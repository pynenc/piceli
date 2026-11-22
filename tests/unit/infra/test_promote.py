"""``piceli promote`` (0.25.0): pin the commit an environment verified.

A flake repository with a bare remote and a working clone, its input's
repository and a controller history in a ``--state-dir``, all in
``tmp_path``. Without nix the lock is edited (its narHash computed here);
with nix on the PATH one test also locks with ``nix flake lock`` and checks
that both agree. No network.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.infra import promote
from piceli.infra.promotion import locked_rev
from piceli.k8s.cli import app
from tests.unit.infra import promotion_support as support
from tests.unit.infra.git_support import git
from tests.unit.infra.promotion_support import history, run


@pytest.fixture
def found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    world = support.world(tmp_path, monkeypatch)
    monkeypatch.setattr(promote.shutil, "which", lambda _name: None)  # edit path
    support.publish_history(
        world["state"],
        history(
            run(world["c"], "r3", "2026-10-22T10:00:00Z"),
            run(world["b"], "r2", "2026-10-21T10:00:00Z", passed=False),
            run(world["a"], "r1", "2026-10-20T10:00:00Z"),
        ),
    )
    return world


def _promote(found: dict[str, Any], *extra: str) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(app, [
        "promote", "web-1", "--from", "main", "--composition", str(found["infra"]),
        "--repo", str(found["work"]), "--state-dir", str(found["state"]), "--json",
        *extra,
    ])  # fmt: skip
    body = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, body, result.stderr


def _remote(found: dict[str, Any]) -> dict[str, str]:
    lines = git(found["work"], "ls-remote", found["hosts"].url).splitlines()
    return {ref: sha for sha, ref in (line.split("\t") for line in lines)}


def test_plan_then_approve_pins_commits_tags_and_pushes(found: dict[str, Any]) -> None:
    work, a, c = found["work"], found["a"], found["c"]
    before = _remote(found)
    code, body, said = _promote(found)
    assert code == 3, body
    plan = body["plan"]
    head = git(work, "rev-parse", "HEAD")
    assert body["state"] == "approval-required"
    assert plan["pinned"] == {"input": "app", "commit": c, "environment": "main",
                              "verified_run": "r3",
                              "verified_at": "2026-10-22T10:00:00Z"}  # fmt: skip
    assert plan["change"]["from"] == a and plan["change"]["to"] == c
    assert plan["change"]["method"] == "edit" and plan["change"]["file"] == "flake.lock"
    assert (plan["tag"], plan["previous_tag"], plan["bump"]) == (
        "web-v1.0.1", "web-v1.0.0", "patch")  # fmt: skip
    assert (plan["remote"], plan["branch"], plan["base"]) == ("origin", "main", head)
    assert plan["commit_message"].startswith(f"promote app to {c[:12]} from main")
    assert body["approve_command"].endswith(f"--approve {body['digest']}")
    assert body["plan_hash"] == body["digest"]  # 0.57.0: one name for scripts
    assert f"pin app {a[:12]} -> {c[:12]} (edit)" in said
    assert git(work, "status", "--porcelain") == ""  # the plan changed nothing
    assert _remote(found) == before

    code, done, _ = _promote(found, "--approve", body["digest"])
    assert code == 0, done
    assert done["state"] == "promoted" and done["tag"] == "web-v1.0.1"
    new = git(work, "rev-parse", "HEAD")
    assert done["commit"] == new and git(work, "rev-parse", "HEAD~1") == head
    assert git(work, "show", "--name-only", "--format=", "HEAD") == "flake.lock"
    remote = _remote(found)
    assert remote["refs/heads/main"] == new
    assert remote["refs/tags/web-v1.0.1^{}"] == new
    lock = git(work, "show", "HEAD:flake.lock")
    assert locked_rev(lock, "app") == c
    node = json.loads(lock)["nodes"]["app"]["locked"]
    assert node["revCount"] == 3 and node["narHash"] != "sha256-" + "A" * 43 + "="

    code, again, said = _promote(found)
    assert code == 0 and again["state"] == "current" and again["plan"] is None
    assert again["tag"] == "web-v1.0.1" and "points at it" in said


def test_a_pinned_commit_without_a_tag_is_offered_a_tag(found: dict[str, Any]) -> None:
    support.publish_history(
        found["state"], history(run(found["a"], "r1", "2026-10-20T10:00:00Z"))
    )
    code, body, _ = _promote(found)
    assert code == 0 and body["state"] == "current"
    assert body["plan"] is None and body["tag"] == "web-v1.0.0"  # tagged already

    found["hosts"].commit({"README.md": "hosts\n"})
    git(found["work"], "pull", "-q")
    git(found["work"], "tag", "web-v1.0.2+rollbacktest")  # a test tag is no release
    code, body, said = _promote(found)
    assert code == 0 and body["state"] == "current", body
    assert body["plan"]["change"] is None and body["plan"]["tag"] == "web-v1.0.1"
    assert "approving tags" in said
    code, done, _ = _promote(found, "--approve", body["digest"])
    assert code == 0, done
    head = git(found["work"], "rev-parse", "HEAD")
    assert done["commit"] == head
    assert _remote(found)["refs/tags/web-v1.0.1^{}"] == head


@pytest.mark.parametrize(
    "case", ["dirty", "behind", "ahead", "lock-ahead", "diverged", "detached"]
)
def test_a_working_copy_out_of_sync_is_refused(
    found: dict[str, Any], case: str
) -> None:
    work = found["work"]
    if case == "dirty":
        (work / "flake.nix").write_text("{ }\n")
    if case in ("behind", "diverged"):
        found["hosts"].commit({"README.md": "upstream\n"})
    if case in ("ahead", "diverged"):
        (work / "notes.txt").write_text("local\n")
        git(work, "add", "notes.txt")
        git(work, "commit", "-q", "-m", "local")
    if case == "lock-ahead":  # a lock commit that is not the planned change
        lock = (work / "flake.lock").read_text()
        (work / "flake.lock").write_text(lock.replace('"version": 7', '"version":7'))
        git(work, "commit", "-q", "-am", "edit the lock")
    if case == "detached":
        git(work, "checkout", "-q", "--detach")
    code, body, _ = _promote(found)
    assert code == 2
    assert body["reason"] == (
        "promote-dirty" if case == "dirty" else "promote-not-in-sync"
    )
    words = {"behind": "is behind", "ahead": "is ahead of", "diverged": "diverged from",
             "lock-ahead": "is ahead of",
             "detached": "detached HEAD", "dirty": "uncommitted"}[case]  # fmt: skip
    assert words in body["message"]


def test_other_refusals(found: dict[str, Any]) -> None:
    code, body, _ = _promote(found, "--approve", "sha256:" + "0" * 64)
    assert (code, body["reason"]) == (2, "promote-plan-changed")
    code, body, _ = _promote(found, "--bump", "huge")
    assert (code, body["reason"]) == (2, "promote-invalid")
    result = CliRunner().invoke(app, [
        "promote", "web-1", "--from", "rc", "--composition", str(found["infra"]),
        "--state-dir", str(found["state"]),
    ])  # fmt: skip
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "promote-invalid"
    support.publish_history(
        found["state"],
        history(run(found["c"], "r9", "2026-10-22T10:00:00Z", passed=False)),
    )
    code, body, _ = _promote(found)
    assert (code, body["reason"]) == (2, "promotion-not-verified")
    (found["state"] / "history.json").unlink()
    code, body, _ = _promote(found)
    assert (code, body["reason"]) == (2, "promotion-history-unreachable")


def test_a_refused_push_leaves_the_remote_unchanged_and_the_same_approve_resumes(
    found: dict[str, Any],
) -> None:
    hook = Path(found["hosts"].url) / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    before = _remote(found)
    code, body, _ = _promote(found)
    code, done, _ = _promote(found, "--approve", body["digest"])
    assert (code, done["reason"]) == (1, "promote-push-failed")
    assert done["tag"] == "web-v1.0.1" and "same --approve command" in done["message"]
    assert done["approve_command"] == body["approve_command"]
    assert done["git_retries"] == []  # a refusal is no transport error
    assert _remote(found) == before
    local = git(found["work"], "rev-parse", "HEAD")
    assert done["commit"] == local

    # 0.74.0: the plan of the state the failed push left is the same plan
    code, again, said = _promote(found)
    assert code == 3 and again["digest"] == body["digest"], again
    assert again["resume"] == {"commit": local, "tag": "web-v1.0.1"}
    assert "resumes a push that failed" in said
    hook.unlink()
    code, done, _ = _promote(found, "--approve", body["digest"])
    assert code == 0, done
    assert done["state"] == "promoted" and done["resumed"] is True
    assert done["commit"] == local == git(found["work"], "rev-parse", "HEAD")
    remote = _remote(found)
    assert remote["refs/heads/main"] == local
    assert remote["refs/tags/web-v1.0.1^{}"] == local
    code, current, _ = _promote(found)
    assert code == 0 and current["state"] == "current" and current["plan"] is None


def test_a_refused_tag_push_is_resumed(found: dict[str, Any]) -> None:
    support.publish_history(
        found["state"], history(run(found["a"], "r1", "2026-10-20T10:00:00Z"))
    )
    found["hosts"].commit({"README.md": "hosts\n"})
    git(found["work"], "pull", "-q")
    hook = Path(found["hosts"].url) / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    code, body, _ = _promote(found)
    assert body["plan"]["tag"] == "web-v1.0.1"
    code, done, _ = _promote(found, "--approve", body["digest"])
    assert (code, done["reason"]) == (1, "promote-push-failed")
    code, again, _ = _promote(found)  # the local tag is not taken as released
    assert again["digest"] == body["digest"] and again["resume"]["tag"] == "web-v1.0.1"
    hook.unlink()
    code, done, _ = _promote(found, "--approve", body["digest"])
    assert code == 0 and done["resumed"] is True, done
    head = git(found["work"], "rev-parse", "HEAD")
    assert _remote(found)["refs/tags/web-v1.0.1^{}"] == head


def test_a_second_verified_run_of_the_same_commit_keeps_the_approval(
    found: dict[str, Any],
) -> None:
    c = found["c"]
    code, body, _ = _promote(found)
    assert code == 3 and body["plan"]["pinned"]["verified_run"] == "r3"
    # another source of the environment moved: a new run verified c again
    support.publish_history(
        found["state"],
        history(
            run(c, "r4", "2026-10-22T11:00:00Z"),
            run(c, "r3", "2026-10-22T10:00:00Z"),
            run(found["a"], "r1", "2026-10-20T10:00:00Z"),
        ),
    )
    code, again, _ = _promote(found)
    assert again["plan"]["pinned"]["verified_run"] == "r4"
    assert again["digest"] == body["digest"]
    code, done, _ = _promote(found, "--approve", body["digest"])
    assert code == 0, done
    assert done["state"] == "promoted" and done["verified_run"] == "r4"
    message = git(found["work"], "log", "-1", "--format=%B")
    assert "(run r4, 2026-10-22T11:00:00Z)" in message
    assert _remote(found)["refs/tags/web-v1.0.1^{}"] == done["commit"]


def test_a_newer_verified_commit_still_changes_the_plan(found: dict[str, Any]) -> None:
    code, body, _ = _promote(found)
    support.publish_history(
        found["state"],
        history(
            run(found["b"], "r4", "2026-10-23T10:00:00Z"),
            run(found["c"], "r3", "2026-10-22T10:00:00Z"),
        ),
    )
    code, done, _ = _promote(found, "--approve", body["digest"])
    assert (code, done["reason"]) == (2, "promote-plan-changed")


def test_the_digest_leaves_out_the_verifying_run() -> None:
    body = {"host": "web-1", "tag": "web-v1.0.1", "commit_message": "run r1",
            "pinned": {"input": "app", "commit": "c" * 40, "environment": "main",
                       "verified_run": "r1", "verified_at": "t1"},
            "change": {"to": "c" * 40}}  # fmt: skip
    other = {**body, "commit_message": "run r2",
             "pinned": {**body["pinned"], "verified_run": "r2", "verified_at": "t2"}}  # fmt: skip
    assert promote.digest(body) == promote.digest(other)
    for key, value in (("tag", "web-v1.0.2"), ("change", {"to": "d" * 40}),
                       ("pinned", {**body["pinned"], "commit": "d" * 40})):  # fmt: skip
        assert promote.digest({**body, key: value}) != promote.digest(body)


def _flaky_git(
    monkeypatch: pytest.MonkeyPatch, command: str, stderr: bytes, times: int = 1
) -> list[list[str]]:
    """Make the first ``times`` runs of ``git COMMAND`` fail with ``stderr``."""
    real = subprocess.run
    calls: list[list[str]] = []

    def fake(args: Any, *rest: Any, **kwargs: Any) -> Any:
        if list(args[:2]) == ["git", command]:
            calls.append(list(args))
            if len(calls) <= times:
                return subprocess.CompletedProcess(args, 128, b"", stderr)
        return real(args, *rest, **kwargs)

    monkeypatch.setattr(promote, "RETRY_PAUSE", 0.0)
    monkeypatch.setattr(promote.subprocess, "run", fake)
    return calls


def test_a_fetch_that_times_out_once_is_retried(
    found: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _flaky_git(
        monkeypatch, "fetch",
        b"fatal: unable to access 'https://example.org/hosts.git/': "
        b"Failed to connect to example.org port 443: Connection timed out\n",
    )  # fmt: skip
    code, body, said = _promote(found)
    assert code == 3, body
    assert len(calls) == 2
    assert body["git_retries"] == [{"command": "git fetch", "pause_seconds": 0.0}]
    assert "git fetch failed in transport; retried once" in said


def test_a_fetch_that_fails_otherwise_is_not_retried(
    found: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _flaky_git(monkeypatch, "fetch", b"fatal: couldn't find remote ref main\n")
    code, body, _ = _promote(found)
    assert (code, body["reason"]) == (2, "promote-git-failed")
    assert len(calls) == 1
    calls = _flaky_git(
        monkeypatch, "fetch", b"ssh: connect to host: Connection refused\n", times=2
    )
    code, body, _ = _promote(found)  # one retry only
    assert (code, body["reason"], len(calls)) == (2, "promote-git-failed", 2)


def test_next_tag_bumps_the_highest_release_of_the_pattern() -> None:
    tags = ["web-v1.9.0", "web-v1.10.0", "web-v2.0.0-rc.1", "api-v5.0.0", "v9.0.0"]
    assert promote.next_tag(tags, "web-v*", "patch") == ("web-v1.10.1", "web-v1.10.0")
    assert promote.next_tag(tags, "web-v*", "minor")[0] == "web-v1.11.0"
    assert promote.next_tag(tags, "web-v*", "major")[0] == "web-v2.0.0"
    assert promote.next_tag([], "web-v*", "patch") == ("web-v0.1.0", None)
    assert promote.next_tag(["web-v1.2.3"], "web-*", "patch")[0] == "web-v1.2.4"
    test = ["web-v0.1.1", "web-v0.1.2+rollbacktest"]
    assert promote.next_tag(test, "web-v*", "patch") == ("web-v0.1.2", "web-v0.1.1")


def test_override_urls() -> None:
    rev = "f" * 40
    assert promote.override_url({"type": "github", "owner": "o", "repo": "r",
                                 "ref": "main"}, rev) == f"github:o/r/{rev}"  # fmt: skip
    assert (
        promote.override_url(
            {"type": "git", "url": "https://h/r.git", "ref": "main"}, rev
        )
        == f"git+https://h/r.git?ref=main&rev={rev}"
    )
    with pytest.raises(promote.PromotionError) as refused:
        promote.override_url({"type": "path", "path": "/x"}, rev)
    assert refused.value.code == "promote-lock-failed"


@pytest.mark.skipif(shutil.which("nix") is None, reason="needs nix")
def test_nix_and_the_edit_path_lock_the_same_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = support.world(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    top, c = world["work"], world["c"]
    head = git(top, "rev-parse", "HEAD")
    by_nix, method = promote.pinned_lock(top, head, "app", c)
    assert method == "nix"
    old = git(top, "show", "HEAD:flake.lock").encode()
    by_edit = promote.lock_by_edit(old, "app", c)
    nixed = json.loads(by_nix)["nodes"]["app"]["locked"]
    edited = json.loads(by_edit)["nodes"]["app"]["locked"]
    for key in ("rev", "narHash", "lastModified", "revCount"):
        assert nixed[key] == edited[key], key
    assert git(top, "worktree", "list").count("\n") == 0  # the worktree is gone
