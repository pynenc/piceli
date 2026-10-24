"""0.19: a failing Git remote is never a silent outage.

On 2026-10-09 the GitOps controller deployed nothing for 30 h: an invalid
token made every ``git ls-remote`` fail, ``gitops wait`` kept reporting the
old revision, and nothing said why. Git's error (masked) is now kept in the
status and the log (once per change), emitted as telemetry, and a wait for a
commit the controller cannot fetch fails at once with that cause.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from piceli.gitops import GitOpsError
from piceli.gitops.repo import GitRemote, mask_git_output
from piceli.gitops.wait import outcome
from tests.unit.infra.test_controller_otel import world  # noqa: F401 (fixture)

SECRET = "ghp_" + "Q" * 36
STDERR = (
    "remote: Invalid username or token. Password authentication is not "
    "supported for Git operations.\n"
    f"fatal: Authentication failed for 'https://bot:{SECRET}@github.com/o/r.git/'\n"
)


def _remote(tmp_path: Path, stderr: str) -> GitRemote:
    credentials = tmp_path / "git"
    credentials.mkdir()
    (credentials / "username").write_text("bot")
    (credentials / "password").write_text(SECRET)

    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 128, b"", stderr.encode())

    return GitRemote(
        "https://github.com/o/r.git",
        tmp_path / "mirror",
        credentials_dir=credentials,
        runner=runner,
    )


def test_git_errors_keep_gits_message_with_the_secret_masked(tmp_path: Path) -> None:
    with pytest.raises(GitOpsError) as error:
        _remote(tmp_path, STDERR).ls_remote()
    message = str(error.value)
    assert error.value.code == "gitops-git-failed"
    assert message.startswith("git ls-remote failed (exit 128): ")
    assert "Invalid username or token" in message
    assert SECRET not in message and "bot:" not in message
    assert error.value.details == {"exit": 128, "stderr": message.split(": ", 1)[1]}


def test_masking_drops_known_values_userinfo_and_tokens() -> None:
    text = f"https://bot:{SECRET}@host/x\nAuthorization: Bearer abc.def\npw hunter22"
    masked = mask_git_output(text, ["hunter22"])
    assert SECRET not in masked and "hunter22" not in masked
    assert "bot" not in masked and "abc.def" not in masked
    assert len(mask_git_output("x\n" * 50 + "last line", [])) < 1000
    assert mask_git_output("x\n" * 50 + "last line", []).endswith("last line")


def _break(state: dict[str, Any]) -> Path:
    """Move the shop remote away: ls-remote fails as with a revoked token."""
    path = Path(state["shop"].remote)
    gone = path.with_name("shop-moved.git")
    shutil.move(path, gone)
    return gone


def test_a_failing_source_is_in_the_status_the_log_and_telemetry_once(
    world: dict[str, Any],  # noqa: F811
) -> None:
    controller, logs, signals = world["controller"], world["logs"], world["signals"]
    controller.poll_once()
    gone = _break(world)
    for _ in range(3):
        status = controller.poll_once()
    shop = status["sources"]["shop"]
    assert shop["error"] == "gitops-git-failed"
    assert shop["error_detail"].startswith("git ls-remote failed (exit ")
    head = status["controller"]
    assert head["state"] == "degraded" and head["last_error"] == "gitops-git-failed"
    assert head["last_error_detail"].startswith("shop: git ls-remote failed")
    assert head["poll_failures"] == 3
    failures = [line for line in logs if line.startswith("source shop:")]
    assert len(failures) == 1, logs  # once per change, not every poll
    assert "gitops-git-failed" in failures[0]
    events = signals.events("piceli.controller.git.failed")
    assert len(events) == 1
    assert events[0].attributes["piceli.source"] == "shop"
    assert events[0].attributes["error.type"] == "gitops-git-failed"
    errors = signals.metrics()["cicd.pipeline.run.errors"]
    assert any(p.attributes.get("error.type") == "gitops-git-failed" for p in errors)
    shutil.move(gone, Path(world["shop"].remote))
    status = controller.poll_once()
    assert "error_detail" not in status["sources"]["shop"]
    assert status["controller"]["last_error_detail"] is None
    assert any(line == "source shop: git works again" for line in logs)


def _document(error: str | None) -> dict[str, Any]:
    return {
        "controller": {
            "last_error": "gitops-git-failed" if error else None,
            "last_error_detail": error,
            "poll_failures": 4 if error else 0,
        },
        "envs": {
            "main": {
                "state": "deployed",
                "commit": "a" * 40,
                "revision": {"shop": "a" * 40},
                "deployed_commit": "a" * 40,
                "deployed_revision": {"shop": "a" * 40},
            }
        },
    }


def test_wait_fails_fast_when_the_controller_cannot_fetch() -> None:
    detail = "shop: git ls-remote failed (exit 128): remote: Invalid username or token."
    state, body = outcome(_document(detail), "main", "b" * 12, seen=False)
    assert state == "failed"
    assert body["stage"] == "fetch" and body["cause"] == "gitops-git-failed"
    assert body["controller_error"] == {"reason": "gitops-git-failed", "detail": detail}
    # The commit already running is still deployed, with the error attached.
    state, body = outcome(_document(detail), "main", "a" * 12, seen=False)
    assert state == "deployed" and body["controller_error"]["detail"] == detail
    # One failed poll may be a blip: the wait goes on.
    blip = _document(detail)
    blip["controller"]["poll_failures"] = 1
    assert outcome(blip, "main", "b" * 12, seen=False)[0] is None
    # Without a Git error a commit the controller has not seen yet waits.
    assert outcome(_document(None), "main", "b" * 12, seen=False)[0] is None


def test_wait_and_summary_name_the_failing_checks_with_their_detail() -> None:
    """0.19."""
    document = _document(None)
    document["envs"]["main"].update(
        state="failed",
        commit="b" * 40,
        revision={"shop": "b" * 40},
        reason="checks-failed-rolled-back",
        checks={
            "state": "failed",
            "results": [
                {"name": "home", "passed": True},
                {
                    "name": "dashboards",
                    "passed": False,
                    "code": "check-failed",
                    "detail": "stale source for k8s.cluster",
                },
            ],
        },
    )
    state, body = outcome(document, "main", "b" * 12, seen=True)
    assert state == "failed" and body["cause"] == "checks-failed-rolled-back"
    assert body["failed_checks"] == [
        {
            "name": "dashboards",
            "code": "check-failed",
            "detail": "stale source for k8s.cluster",
        }
    ]


def test_a_github_token_close_to_expiry_is_warned(
    world: dict[str, Any],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.19: GitHub reports a token's expiry; warn 7 days ahead."""
    from datetime import UTC, datetime

    from piceli.gitops.token_expiry import github_token_expiry, is_github, parse_expiry

    assert parse_expiry("2026-11-01 12:00:00 UTC") == datetime(
        2026, 11, 1, 12, tzinfo=UTC
    )
    assert parse_expiry(None) is None and parse_expiry("never") is None
    assert is_github("https://github.com/o/r.git") and not is_github(
        "git@github.com:o/r"
    )
    credentials = tmp_path / "creds"
    credentials.mkdir()
    (credentials / "username").write_text("bot")
    (credentials / "password").write_text(SECRET)
    asked: list[dict[str, str]] = []

    def fetch(url: str, headers: dict[str, str]) -> dict[str, str]:
        asked.append(headers)
        return {"github-authentication-token-expiration": "2026-09-20 00:00:00 UTC"}

    expires = github_token_expiry(credentials, fetch)
    assert expires == datetime(2026, 9, 20, tzinfo=UTC)
    assert asked[0]["Authorization"].startswith("Basic ")
    assert SECRET not in json.dumps(asked[0])  # base64 in the header only

    controller, logs, clock = world["controller"], world["logs"], world["clock"]
    from piceli.gitops import token_expiry

    # The local shop remote stands for a github.com one (no network).
    monkeypatch.setattr(token_expiry, "is_github", lambda url: url.endswith("shop.git"))
    controller.token_expiry = lambda _directory: datetime.fromtimestamp(
        clock["now"] + 3 * 86400, UTC
    )
    status = controller.poll_once()
    warnings = status["controller"]["warnings"]
    assert [item["code"] for item in warnings] == ["gitops-token-expiring"]
    assert warnings[0]["source"] == "shop" and warnings[0]["expires_at"]
    assert any("gitops-token-expiring" in line for line in logs)
    controller.token_expiry = lambda _directory: datetime.fromtimestamp(
        clock["now"] + 90 * 86400, UTC
    )
    clock["now"] += 7 * 3600  # the next check
    assert controller.poll_once()["controller"]["warnings"] == []


def test_gitops_cache_clear_runs_between_steps_and_is_recorded(
    world: dict[str, Any],  # noqa: F811
) -> None:
    """0.19: ``piceli gitops cache clear [--crate]``."""
    from piceli.gitops.state import cache_clear_request

    controller, channel = world["controller"], world["channel"]
    controller.poll_once()
    cleared: list[list[str]] = []

    def clear(crates: list[str]) -> dict[str, Any]:
        cleared.append(crates)
        return {"state": "passed", "job": "piceli-build-cache-clear-x"}

    controller.ports.builder.clear_cache = clear
    channel.add_request(*cache_clear_request(["geometry", "serde_derive"]))
    status = controller.poll_once()
    assert cleared == [["geometry", "serde_derive"]]
    record = status["controller"]["cache_clear"]
    assert (
        record["crates"] == ["geometry", "serde_derive"] and record["state"] == "passed"
    )
    with pytest.raises(GitOpsError):
        cache_clear_request(["../etc"])


def test_the_build_cache_clear_job_empties_only_targets(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    from piceli.infra.builders import JobSettings, cache_clear_job

    settings = JobSettings(
        image="ghcr.io/x/builder@sha256:" + "a" * 64,
        namespace="piceli-system",
        registry_url="oci://registry:5000/x",
        node_registry="registry:5000",
    )
    job = cache_clear_job(settings, ["geometry"], "abcd")
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["volumes"] == [
        {
            "name": "cache",
            "persistentVolumeClaim": {"claimName": "piceli-component-cache"},
        }
    ]
    container = pod["containers"][0]
    assert "env" not in container  # no Git or registry Secret
    cache = tmp_path / "cache"
    for name in (
        "amd64-4096/release/deps/libgeometry-1.rlib",
        "amd64-4096/release/deps/libapp-1.rlib",
    ):
        (cache / "target" / name).parent.mkdir(parents=True, exist_ok=True)
        (cache / "target" / name).write_text("x")
    (cache / "stage").mkdir()
    script = container["command"][3]
    env = {**os.environ, "PICELI_CACHE": str(cache)}
    run = subprocess.run(
        [sys.executable, "-c", script, "geometry"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    assert '"amd64-4096": {"entries": 1}' in run.stdout
    assert (cache / "target/amd64-4096/release/deps/libapp-1.rlib").exists()
    everything = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True
    )
    assert everything.returncode == 0 and not (cache / "target" / "amd64-4096").exists()
    assert (cache / "stage").is_dir()
