"""``piceli dev run`` end to end against a local stand-in for the cluster."""

from __future__ import annotations

from pathlib import Path

import pytest

from piceli.dev.client import RunRequest, run
from piceli.dev.model import DevBuilds, DevError, DevProfile
from piceli.dev.pack import SourceRequest
from tests.unit.dev.local_port import LocalPort
from tests.unit.dev.test_pack import commit, git, repo

IMAGE = "ghcr.io/example/builder@sha256:" + "a" * 64
DEV = DevBuilds(
    node="builder-1",
    image=IMAGE,
    profiles=[DevProfile("sh", tools=["sh"], toolchain=None)],
)


def test_a_ref_runs_and_returns_the_result_with_every_phase(tmp_path: Path) -> None:
    app = repo(tmp_path / "shop", {"src/a.txt": "hello\n", "t.sh": "cat src/a.txt"})
    port = LocalPort(tmp_path / "cluster")
    lines: list[str] = []
    result = run(
        port,
        DEV,
        RunRequest(
            SourceRequest("shop", app, "HEAD"), ["sh", "t.sh"], requester="agent-1"
        ),
        log=lines.append,
    )
    assert result["schema"] == "piceli.dev-run.v1"
    assert result["state"] == "passed" and result["exit_code"] == 0
    assert "hello" in lines
    assert {"pack", "queue", "transfer", "sync", "command", "total"} <= set(
        result["durations"]
    )
    assert result["sources"]["shop"]["commit"] == git(app, "rev-parse", "HEAD")
    assert result["requester"] == "agent-1" and result["node"] == "builder-1"
    assert port.deleted == [result["run"]]  # the Job goes on every outcome


def test_a_failing_command_returns_its_code_and_tail(tmp_path: Path) -> None:
    app = repo(tmp_path / "shop", {"t.sh": "echo boom; exit 4"})
    result = run(
        LocalPort(tmp_path / "cluster"),
        DEV,
        RunRequest(SourceRequest("shop", app, "HEAD"), ["sh", "t.sh"]),
    )
    assert result["state"] == "failed" and result["exit_code"] == 4
    assert result["reason"] == "dev-command-failed" and "boom" in result["log_tail"]


def test_a_sibling_branch_reuses_the_warm_lineage(tmp_path: Path) -> None:
    app = repo(tmp_path / "shop", {"a.txt": "1", "b.txt": "2"})
    port = LocalPort(tmp_path / "cluster")
    first = run(port, DEV, RunRequest(SourceRequest("shop", app, "HEAD"), ["true"]))
    git(app, "checkout", "-q", "-b", "wp-b")
    commit(app, {"b.txt": "changed"})
    second = run(port, DEV, RunRequest(SourceRequest("shop", app, "wp-b"), ["true"]))
    assert second["cache"]["lineage"] == first["cache"]["lineage"]
    assert second["cache"]["warm"] is True
    assert second["sync"] == {"written": 1, "removed": 0, "kept": 1}


def test_artifacts_come_back_to_the_client(tmp_path: Path) -> None:
    app = repo(
        tmp_path / "shop",
        {
            "t.sh": "mkdir -p $CARGO_TARGET_DIR/r && echo ok > $CARGO_TARGET_DIR/r/report.json"
        },
    )
    result = run(
        LocalPort(tmp_path / "cluster"),
        DEV,
        RunRequest(
            SourceRequest("shop", app, "HEAD"),
            ["sh", "t.sh"],
            artifacts=["target/r/report.json"],
            artifacts_dir=tmp_path / "artifacts",
        ),
    )
    (local,) = result["artifacts"]
    assert Path(local).read_text() == "ok\n"
    assert Path(local).is_relative_to(tmp_path / "artifacts" / result["run"])


def test_a_run_cancelled_while_queued_reports_it(tmp_path: Path) -> None:
    app = repo(tmp_path / "shop", {"a": "x"})
    port = LocalPort(tmp_path / "cluster")
    port.cancel_before_start.add("fixed-run")
    result = run(
        port,
        DEV,
        RunRequest(SourceRequest("shop", app, "HEAD"), ["true"]),
        run_id="fixed-run",
    )
    assert result["state"] == "cancelled" and result["reason"] == "dev-run-cancelled"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"profile": "python"}, "dev-profile-unknown"),
        ({"command": []}, "dev-run-invalid"),
        ({"priority": "urgent"}, "dev-run-invalid"),
        ({"cwd": "../elsewhere"}, "dev-run-invalid"),
    ],
)
def test_refusals_create_nothing(
    tmp_path: Path, change: dict[str, object], code: str
) -> None:
    app = repo(tmp_path / "shop", {"a": "x"})
    port = LocalPort(tmp_path / "cluster")
    request = RunRequest(SourceRequest("shop", app, "HEAD"), ["true"])
    for key, value in change.items():
        setattr(request, key, value)
    with pytest.raises(DevError) as raised:
        run(port, DEV, request)
    assert raised.value.code == code and port.jobs == {}
