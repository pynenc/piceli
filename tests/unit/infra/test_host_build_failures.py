"""0.41.0: a build host's failure in the run record; identical failures not retried.

A fake build host (the real ``BuilderServe`` with a fake container that
prints what ``piceli build job-run`` prints for a failing command, then
exits 1) answers build-failed. The controller keeps the failing command's
last lines and the host's stage in ``failure`` and ``failed_attempts``;
``piceli gitops show RUN --logs`` prints them (text and ``--json``). The
same failure on the same inputs is not retried a third time; a transient
one is.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.gitops import releases
from piceli.gitops.controller import failure_fingerprint, transient_failure
from piceli.gitops.state import build_failed_request, sync_request
from piceli.infra.builder_serve import BuilderServe, failing_output
from piceli.k8s.cli import app
from tests.unit.infra.test_remote_builds import _next_try, _world

MISSING = "bash: site/scripts/build_site.sh: No such file or directory"
JOB_OUTPUT = [
    "[build] shop for linux/amd64",
    "[piceli] linux/amd64 command 1/1 (bash): running",
    "[piceli] linux/amd64 command 1/1 (bash): failed in 0.1s",
    "[piceli] the failing command's last 1 line(s) of output:",
    f"  | {MISSING}",
    '{"state": "rejected", "reason": "component-build-failed"}',
    *[f"cleanup line {n}" for n in range(150)],  # noise after the block
]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return _world(tmp_path, monkeypatch)


def _host(world: dict[str, Any], tmp_path: Path) -> BuilderServe:
    def container(argv: list[str], sources: Any, out: Path, line: Any) -> int:
        for text in JOB_OUTPUT:
            line(text)
        return 1

    return BuilderServe(
        host="builder-1",
        image="ghcr.io/x/builder@sha256:" + "e" * 64,
        channel=world["channel"],
        container=container,
        push=None,
        index=None,
        root=tmp_path / "builder",
        sleep=lambda _s: world["controller"].poll_once(),
    )


def cli(*argv: str) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(app, ["gitops", *argv])
    out = result.stdout.strip().splitlines()
    body = json.loads(out[-1]) if out and out[-1].startswith("{") else {}
    return result.exit_code, body, result.stderr


def test_the_failing_commands_output_and_stage_reach_gitops_show(
    world: dict[str, Any], tmp_path: Path
) -> None:
    controller = world["controller"]
    controller.poll_once()
    assert _host(world, tmp_path).once() is not None
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "retrying" and main["attempts"] == 1
    failure = main["failure"]
    assert failure["stage"] == "build" and failure["host"] == "builder-1"
    assert failure["log_tail"].endswith(MISSING)  # the block, not the noise
    assert "cleanup line" not in failure["log_tail"]
    assert main["failed_attempts"][0]["log_tail"] == failure["log_tail"]
    assert main["failed_attempts"][0]["stage"] == "build"
    assert main["failed_stage"] == "build"

    status = _next_try(world)  # the same failure on the same inputs
    main = status["envs"]["main"]
    assert main["state"] == "failed" and main["attempts"] == 2
    assert main["reason"] == "component-build-failed"
    assert main["stopped"] == "same-failure"

    state = str(controller.state_dir)
    history = releases.history(controller.channel.read_history(), "main")
    run_id = history["environments"][0]["runs"][0]["id"]
    code, body, _ = cli("show", run_id, "--logs", "--state-dir", state, "--json")
    assert code == 0
    run = body["run"]
    assert run["state"] == "failed" and run["stopped"] == "same-failure"
    assert run["failure"]["stage"] == "build"
    assert run["failure"]["host"] == "builder-1"
    assert MISSING in run["failure"]["log_tail"]
    sources = [(item["source"], item.get("stage")) for item in body["logs"]]
    assert sources == [("failed-attempt", "build"), ("failure", "build")]
    assert all(MISSING in item["text"] for item in body["logs"])

    code, _, text = cli("show", run_id, "--logs", "--state-dir", state)
    assert code == 0
    assert "failure component-build-failed in stage build on build host builder-1" in (
        text
    )
    assert "stage None" not in text and "not retried" in text
    assert "--- failure component-build-failed (stage build)" in text
    assert MISSING in text


def test_a_new_revision_or_a_sync_tries_again(world: dict[str, Any]) -> None:
    controller, channel = world["controller"], world["channel"]
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    channel.add_request(
        *build_failed_request(wanted["id"], "builder-1", "the build exited 1", MISSING)
    )
    controller.poll_once()
    assert _next_try(world)["envs"]["main"]["stopped"] == "same-failure"
    channel.add_request(*sync_request("main"))
    status = _next_try(world)
    main = status["envs"]["main"]
    assert main["state"] == "retrying" and main.get("stopped") is None
    assert status["controller"]["builds_wanted"]  # offered to the hosts again
    world["shop"].commit({"web/index.html": "<h1>v2</h1>\n"})
    main = _next_try(world)["envs"]["main"]
    assert main["attempts"] == 0 and main.get("stopped") is None


def test_a_transient_failure_is_still_retried(world: dict[str, Any]) -> None:
    controller, channel = world["controller"], world["channel"]
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    channel.add_request(
        *build_failed_request(
            wanted["id"],
            "builder-1",
            "the build exited 1",
            "error: failed to download: connection reset by peer",
        )
    )
    controller.poll_once()
    for _ in range(2):
        main = _next_try(world)["envs"]["main"]
        assert main["state"] == "retrying" and main.get("stopped") is None
    assert main["attempts"] == 3


def test_transient_codes_and_outputs_are_never_fingerprinted() -> None:
    tail = {"log_tail": "error[E0308]: mismatched types"}
    for code in (
        "build-host-quarantined",
        "gitops-git-failed",
        "registry-unreachable",
        "build-timed-out",
    ):
        assert transient_failure(code, tail)
    assert not transient_failure("component-build-failed", tail)
    assert transient_failure("component-build-failed", None)  # nothing to compare
    for text in ("No space left on device", "dial tcp: i/o timeout", "OOMKilled"):
        assert transient_failure("component-build-failed", {"log_tail": text})


def test_the_fingerprint_ignores_times_durations_temp_paths_and_ids() -> None:
    def mark(text: str, inputs: str = "shop@abc;key") -> str | None:
        return failure_fingerprint("component-build-failed", {"log_tail": text}, inputs)

    one = mark(
        "2026-10-30T10:00:01Z step failed in 1.5s at /tmp/build-x1/a.sh "
        "(container 0123456789abcdef)"
    )
    two = mark(
        "2026-10-30T11:22:33Z step failed in 12s at /tmp/build-y9/a.sh "
        "(container fedcba9876543210)"
    )
    assert one is not None and one == two
    assert mark("step failed") != mark("other step failed")
    assert mark("step failed") != mark("step failed", "shop@def;key")
    assert mark("") is None


def test_failing_output_keeps_the_block_or_the_whole_tail() -> None:
    lines = [
        "[piceli] x command 1/2 (bash): failed in 1s",
        "[piceli] the failing command's last 2 line(s) of output:",
        "  | first",
        "  | last",
        "[piceli] the first error:",
        "  | error: first",
        "trailing",
    ]
    assert failing_output(lines).splitlines() == [
        lines[0], lines[4], lines[5], lines[1], lines[2], lines[3],
    ]  # fmt: skip
    assert failing_output(["a", "b"]) == "a\nb"
