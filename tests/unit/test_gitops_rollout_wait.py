"""0.28.0: ``gitops enable --wait`` and ``promote HOST --from ENV --wait``.

The rollout decision is pure (a status document in); the commands run
against a local status directory, a fake install API and a fake host
status (no cluster, no SSH).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.gitops.state import DirectoryChannel
from piceli.gitops.wait import progress, rollout
from piceli.k8s.cli import app as cli

A, B = "a" * 40, "b" * 40
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "composition"


def _doc(poll: str = "2033-05-18T03:40:00Z", **envs: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "piceli.gitops-status.v1",
        "controller": {"last_poll": poll, "heartbeat_at": poll},
        "envs": envs,
    }


def _running(commit: str = B, **extra: Any) -> dict[str, Any]:
    return {
        "state": "deployed",
        "commit": commit,
        "revision": {"shop": commit},
        "deployed_commit": commit,
        "deployed_revision": {"shop": commit},
        "checks": {"state": "passed", "total": 2, "passed": 2},
        **extra,
    }


WAITING_BUILD = {
    "state": "retrying",
    "reason": "build-waiting",
    "commit": B,
    "revision": {"shop": B},
    "deployed_revision": {"shop": A},
    "waiting_since": "2033-05-18T03:37:00Z",
}


def test_every_environment_must_run_its_revision_with_checks_passed() -> None:
    state, body = rollout(_doc(main=_running(), staging=WAITING_BUILD))
    assert state is None
    assert body["envs"]["staging"]["text"] == "building (waiting for a build host) 3m"
    assert body["envs"]["main"]["text"] == "running bbbbbbbbbbbb, checks passed"
    state, _ = rollout(_doc(main=_running(), staging=_running()))
    assert state == "deployed"
    # Only a poll after the install counts: an old status never ends the wait.
    old = "2033-05-18T03:40:00Z"
    assert rollout(_doc(main=_running()), since_poll=old)[0] is None
    assert (
        rollout(_doc(main=_running(), poll="2033-05-18T03:41:00Z"), since_poll=old)[0]
        == "deployed"
    )


def test_progress_lines_name_the_stage_and_the_approval() -> None:
    now = 2_000_000_400.0  # 2033-05-18T03:40:00Z
    step = {"state": "pending", "in_progress": {"action": "deploy", "stage": "build",
            "stage_since": "2033-05-18T03:38:00Z"}}  # fmt: skip
    assert progress("main", step, now)["text"] == "deploying, stage build 2m"
    approval = {"state": "approval-required", "plan_hash": "sha256:" + "d" * 64}
    assert progress("rc", approval, now)["text"] == (
        "waiting for approval: piceli gitops approve rc sha256:" + "d" * 64
    )


def test_the_first_failure_ends_the_wait_with_its_cause_and_tail() -> None:
    failed = {
        **WAITING_BUILD,
        "state": "failed",
        "reason": "component-build-failed",
        "failed_stage": "build",
        "failure": {"log_tail": "\n".join(f"line {n}" for n in range(30))},
    }
    state, body = rollout(_doc(main=_running(), staging=failed))
    assert state == "failed"
    failure = body["failure"]
    assert (failure["env"], failure["reason"], failure["stage"]) == (
        "staging", "component-build-failed", "build")  # fmt: skip
    assert failure["log_tail"].splitlines() == [f"line {n}" for n in range(10, 30)]
    degraded = _running(
        health="degraded",
        reason="pipeline-checks-failed",
        verification={"failed": [{"check": "home", "code": "check-failed",
                                  "detail": "503"}]},
    )  # fmt: skip
    state, body = rollout(_doc(main=degraded))
    assert (
        state == "failed" and body["failure"]["detail"] == "failing checks: home (503)"
    )


def test_a_composition_that_does_not_import_fails_at_once() -> None:
    document = _doc(main=_running())
    document["controller"]["composition_repo"] = {
        "failed": B,
        "error": "composition-invalid",
        "error_detail": "importing infra.py failed: SyntaxError",
    }
    state, body = rollout(document)
    assert state == "failed"
    assert body["failure"] == {
        "env": None, "reason": "composition-invalid", "stage": "import",
        "commit": B, "detail": "importing infra.py failed: SyntaxError",
        "log_tail": None,
    }  # fmt: skip


# ------------------------------------------------------------- gitops enable --wait


class FakePlan:
    changes = True
    plan_hash = "sha256:" + "e" * 64
    action = "enable"
    objects: list[Any] = []

    def to_dict(self) -> dict[str, Any]:
        return {"plan_hash": self.plan_hash}


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """``gitops enable`` against a fake API whose status is a directory."""
    import piceli.gitops.install as install
    import piceli.gitops.state as state
    import piceli.k8s.cli.gitops as gitops

    class Api:
        client = object()

    @contextmanager
    def api(*_args: Any, **_kw: Any) -> Iterator[Any]:
        yield Api()

    monkeypatch.setattr(gitops, "_api", api)
    monkeypatch.setattr(install, "plan_objects", lambda *a, **k: FakePlan())
    monkeypatch.setattr(install, "execute", lambda *a, **k: [])
    channel = DirectoryChannel(tmp_path / "status")
    monkeypatch.setattr(state, "ConfigMapChannel", lambda _client, _ns: channel)
    monkeypatch.setattr("piceli.k8s.cli.gitops_watch.time.sleep", lambda _s: None)
    yield channel


def _enable(*extra: str) -> Any:
    return CliRunner().invoke(cli, [
        "gitops", "enable", str(EXAMPLE / "infra.py"),
        "--image", "ghcr.io/example/piceli@sha256:" + "0" * 64,
        "--kubeconfig", "kc", "--context", "c",
        "--approve", FakePlan.plan_hash, "--wait", "--interval", "1", *extra,
    ])  # fmt: skip


def test_enable_wait_returns_when_every_environment_runs(
    installed: DirectoryChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _doc(poll="2033-05-18T03:39:00Z", main=_running(A))
    reads = iter([before, before, _doc(main=WAITING_BUILD), _doc(main=_running())])
    monkeypatch.setattr(installed, "read_status", lambda: next(reads))
    result = _enable()
    assert result.exit_code == 0, result.output
    assert "main: building (waiting for a build host) 3m" in result.stderr
    body = json.loads(result.stdout)
    assert body["state"] == "enabled" and body["wait"]["state"] == "deployed"


def test_enable_wait_reports_a_composition_import_failure(
    installed: DirectoryChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _doc(poll="2033-05-18T03:39:00Z", main=_running(A))
    document = _doc(main=_running(A))  # the new controller's first poll
    document["controller"]["composition_repo"] = {
        "failed": B, "error": "composition-invalid",
        "error_detail": "importing infra.py failed: NameError",
    }  # fmt: skip
    reads = iter([before, before, document])
    monkeypatch.setattr(installed, "read_status", lambda: next(reads))
    result = _enable()
    assert result.exit_code == 1, result.output
    assert "the composition: failed in stage import (composition-invalid)" in (
        result.stderr
    )
    assert json.loads(result.stdout)["wait"]["failure"]["reason"] == (
        "composition-invalid"
    )


def test_enable_wait_times_out_with_exit_2(installed: DirectoryChannel) -> None:
    installed.publish(_doc(main=WAITING_BUILD))
    result = _enable("--timeout", "0")
    assert result.exit_code == 2, result.output
    body = json.loads(result.stdout)["wait"]
    assert body["state"] == "timed-out" and body["reason"] == "gitops-wait-timeout"
    assert "still waiting: main" in result.stderr


# ------------------------------------------------------------- promote --wait


def test_promote_wait_follows_the_publish_to_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.k8s.cli import promote

    controller = [
        {"tag": "web-v1.0.1", "state": "waiting-build"},
        {"tag": "web-v1.0.1", "state": "publishing", "claimed": {"host": "builder-1"}},
        {"tag": "web-v1.0.1", "state": "published"},
        {"tag": "web-v1.0.1", "state": "published"},
    ]
    hosts = iter([
        {"host": "web-1", "current": {"tag": "web-v1.0.0"}},
        {"host": "web-1", "current": {"tag": "web-v1.0.0"},
         "in_progress": {"tag": "web-v1.0.1"}},
        {"host": "web-1", "current": {"tag": "web-v1.0.1"}},
    ])  # fmt: skip
    records = iter(controller)
    monkeypatch.setattr("piceli.hosts.remote.read_status", lambda *a, **k: next(hosts))
    monkeypatch.setattr("piceli.k8s.cli.host._target", lambda *a: (None, _Ssh(), None))
    code, body = promote.wait_host_release(
        "web-1", "web-v1.0.1", composition=tmp_path / "infra.py",
        controller=lambda: next(records), timeout=60, interval=1,
        sleep=lambda _s: None,
    )  # fmt: skip
    assert code == 0 and body["state"] == "deployed"


class _Ssh:
    address = "root@web-1.example.org"


def test_promote_wait_reports_a_failed_publish_or_a_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from piceli.k8s.cli import promote

    monkeypatch.setattr("piceli.k8s.cli.host._target", lambda *a: (None, _Ssh(), None))
    failed = {
        "tag": "web-v1.0.1", "state": "failed", "reason": "gitops-nix-release-failed",
        "commit": B, "failure": {"host": "builder-1", "reason": "build-failed",
                                 "log_tail": "error: builder failed\n"},
    }  # fmt: skip
    code, body = promote.wait_host_release(
        "web-1", "web-v1.0.1", composition=tmp_path / "infra.py",
        controller=lambda: failed, timeout=60, interval=1, sleep=lambda _s: None,
    )  # fmt: skip
    assert code == 1
    assert body["failure"]["reason"] == "gitops-nix-release-failed"
    assert body["failure"]["detail"] == "build host builder-1; build-failed"
    assert "error: builder failed" in capsys.readouterr().err

    rolled = {
        "host": "web-1",
        "current": {"tag": "web-v1.0.0"},
        "last_attempt": {
            "tag": "web-v1.0.1", "state": "rolled-back", "commit": B,
            "stages": [{"name": "checks", "state": "failed"}],
            "checks": [{"name": "http", "state": "failed"}],
        },
    }  # fmt: skip
    monkeypatch.setattr("piceli.hosts.remote.read_status", lambda *a, **k: rolled)
    code, body = promote.wait_host_release(
        "web-1", "web-v1.0.1", composition=tmp_path / "infra.py",
        controller=lambda: None, timeout=60, interval=1, sleep=lambda _s: None,
    )  # fmt: skip
    assert code == 1
    assert (body["failure"]["reason"], body["failure"]["stage"]) == (
        "host-rolled-back", "checks")  # fmt: skip
    assert body["failure"]["detail"] == "failing checks: http"

    monkeypatch.setattr(
        "piceli.hosts.remote.read_status", lambda *a, **k: {"current": {}}
    )
    code, body = promote.wait_host_release(
        "web-1", "web-v1.0.1", composition=tmp_path / "infra.py",
        controller=lambda: None, timeout=0, interval=1, sleep=lambda _s: None,
    )  # fmt: skip
    assert code == 2 and body["reason"] == "gitops-wait-timeout"


def test_promote_wait_needs_from(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli, ["promote", "wp-1@abcdef1", "--wait", "--state-dir", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "promote-invalid"
