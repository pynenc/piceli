"""``piceli gitops wait`` and the status summary for coding agents (0.17.0)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.gitops.wait import outcome, summary
from piceli.k8s.cli import app as cli

OLD, NEW, NEWER = "a" * 40, "b" * 40, "c" * 40


def _doc(**record: Any) -> dict[str, Any]:
    base = {
        "branch": "main",
        "state": "pending",
        "commit": NEW,
        "revision": {"infra": "9" * 40, "product": NEW},
        "deployed_commit": OLD,
        "deployed_revision": {"infra": "9" * 40, "product": OLD},
        "attempts": 0,
    }
    return {"schema": "piceli.gitops-status.v1", "envs": {"main": {**base, **record}}}


def test_the_wait_goes_on_until_the_commit_runs() -> None:
    assert outcome(_doc(), "main", NEW[:7], seen=False)[0] is None
    state, body = outcome(
        _doc(
            state="deployed",
            deployed_commit=NEW,
            deployed_revision={"infra": "9" * 40, "product": NEW},
        ),
        "main",
        NEW[:7],
        seen=True,
    )
    assert state == "deployed" and body["deployed_revision"]["product"] == NEW


def test_a_failed_deploy_ends_the_wait_with_its_stage_and_cause() -> None:
    state, body = outcome(
        _doc(
            state="failed",
            reason="restore-point-copy-failed",
            failed_stage="backup",
            failure={"writers_started": ["StatefulSet/db"]},
            attempts=4,
        ),
        "main",
        NEW[:12],
        seen=True,
    )
    assert state == "failed"
    assert body["stage"] == "backup" and body["cause"] == "restore-point-copy-failed"
    assert body["last_error"]["failure"] == {"writers_started": ["StatefulSet/db"]}


def test_retries_are_waited_through() -> None:
    state, body = outcome(
        _doc(
            state="retrying",
            reason="component-build-failed",
            failed_stage="build",
            attempts=1,
            next_attempt_at=1_790_000_060.0,
        ),
        "main",
        NEW[:7],
        seen=True,
    )
    assert state is None
    assert body["attempt"] == 1 and body["next_retry_at"] == "2026-09-21T14:14:20Z"
    assert body["last_error"]["stage"] == "build"


def test_a_newer_revision_supersedes_a_commit_seen_before() -> None:
    newer = _doc(commit=NEWER, revision={"infra": "9" * 40, "product": NEWER})
    assert outcome(newer, "main", NEW[:7], seen=True)[0] == "superseded"
    # Never seen yet (the controller has not polled since the push): wait.
    assert outcome(newer, "main", NEW[:7], seen=False)[0] is None


def test_approval_and_declared_stops_end_the_wait() -> None:
    state, body = outcome(
        _doc(state="approval-required", plan_hash="sha256:" + "d" * 64),
        "main",
        NEW[:7],
        seen=False,
    )
    assert state == "approval-required" and body["plan_hash"].startswith("sha256:")
    assert outcome(_doc(state="stopped"), "main", NEW[:7], seen=False)[0] == "stopped"


def test_the_summary_shows_the_running_stage() -> None:
    view = summary(
        _doc(
            in_progress={
                "action": "deploy",
                "since": "2026-10-07T08:46:00Z",
                "stage": "backup",
                "stage_since": "2026-10-07T08:50:00Z",
            }
        )
    )["main"]
    assert view["stage"] == "backup" and view["since"] == "2026-10-07T08:50:00Z"
    assert view["last_error"] is None


def _write_status(state_dir: Path, document: dict[str, Any]) -> None:
    from piceli.gitops.state import DirectoryChannel

    DirectoryChannel(state_dir).publish(document)


def test_the_wait_command_returns_deployed(tmp_path: Path) -> None:
    document = _doc(
        state="deployed",
        deployed_commit=NEW,
        deployed_revision={"infra": "9" * 40, "product": NEW},
    )
    document["controller"] = {"state": "running"}
    _write_status(tmp_path, document)
    result = CliRunner().invoke(
        cli,
        ["gitops", "wait", "main", NEW[:7], "--state-dir", str(tmp_path), "--json"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    body = json.loads(result.stdout)
    assert body["schema"] == "piceli.gitops-wait.v1" and body["state"] == "deployed"


def test_the_wait_command_times_out(tmp_path: Path) -> None:
    _write_status(tmp_path, _doc())
    result = CliRunner().invoke(
        cli,
        [
            "gitops", "wait", "main", NEW[:7], "--state-dir", str(tmp_path),
            "--timeout", "0", "--json",
        ],
    )  # fmt: skip
    assert result.exit_code == 1
    assert json.loads(result.stdout)["state"] == "timed-out"


@pytest.mark.parametrize("commit", ["abc", "not-a-commit", "A" * 40])
def test_the_wait_refuses_what_is_no_commit(tmp_path: Path, commit: str) -> None:
    result = CliRunner().invoke(
        cli, ["gitops", "wait", "main", commit, "--state-dir", str(tmp_path), "--json"]
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "gitops-request-invalid"


def test_status_json_has_a_summary_per_environment(tmp_path: Path) -> None:
    document = _doc(state="retrying", reason="component-build-failed", attempts=2)
    document["controller"] = {"state": "running"}
    _write_status(tmp_path, document)
    result = CliRunner().invoke(
        cli, ["gitops", "status", "--state-dir", str(tmp_path), "--json"]
    )
    assert result.exit_code == 0, result.stderr
    view = json.loads(result.stdout)["summary"]["main"]
    assert (
        view["attempt"] == 2
        and view["last_error"]["reason"] == "component-build-failed"
    )
