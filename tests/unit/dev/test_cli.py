"""``piceli dev run``: the command's contract, against a local stand-in."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.dev.model import DevBuilds, DevProfile
from piceli.infra import Cluster, Node
from piceli.k8s.cli import app as cli
from tests.unit.dev.local_port import LocalPort
from tests.unit.dev.test_pack import repo

IMAGE = "ghcr.io/example/builder@sha256:" + "a" * 64
DEV = DevBuilds(
    node="builder-1",
    image=IMAGE,
    profiles=[DevProfile("sh", tools=["sh"], toolchain=None)],
)


@pytest.fixture
def stand_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import piceli.dev.cluster
    import piceli.k8s.cli.dev as dev_cli

    installed: dict[str, Any] = {"dev": DEV}
    ports: list[LocalPort] = []

    class Port(LocalPort):
        def __init__(self, api: Any) -> None:
            super().__init__(tmp_path / "cluster")
            ports.append(self)

        def config(self) -> DevBuilds | None:
            return installed["dev"]

    @contextmanager
    def connect(*_args: Any) -> Iterator[object]:
        yield object()

    cluster = Cluster(
        "c", api="https://10.0.0.1:6443", credentials="c",
        nodes=[Node("builder-1", arch="amd64", roles=["builder"])],
    )  # fmt: skip
    monkeypatch.setattr(dev_cli, "load_cluster", lambda ref: cluster)
    monkeypatch.setattr(dev_cli, "connect", connect)
    monkeypatch.setattr(piceli.dev.cluster, "DevCluster", Port)
    app = repo(
        tmp_path / "shop", {"ok.sh": "echo fine", "bad.sh": "echo broken; exit 2"}
    )
    monkeypatch.chdir(app)
    return {"installed": installed, "ports": ports}


def invoke(*args: str) -> Any:
    return CliRunner().invoke(cli, ["dev", "run", "--cluster", "infra.py:c", *args])


def test_a_passing_run_exits_0_with_the_json_result(stand_in: dict[str, Any]) -> None:
    result = invoke(
        "--ref", "HEAD", "--json", "--requester", "Agent 7", "--", "sh", "ok.sh"
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    body = json.loads(result.stdout)
    assert body["state"] == "passed" and body["requester"] == "agent-7"
    assert "fine" in result.stderr  # the log is streamed to stderr
    assert "run " in result.stderr and ": passed" in result.stderr


def test_a_failing_command_exits_1(stand_in: dict[str, Any]) -> None:
    result = invoke("--ref", "HEAD", "--json", "--quiet", "--", "sh", "bad.sh")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["reason"] == "dev-command-failed"
    assert "  | broken" in result.stderr  # quiet: only the tail


def test_refusals_exit_2_with_a_code(stand_in: dict[str, Any]) -> None:
    result = invoke("--ref", "HEAD", "--worktree", ".", "--json", "--", "true")
    assert (
        result.exit_code == 2
        and json.loads(result.stdout)["reason"] == "dev-run-invalid"
    )
    result = invoke("--ref", "nope", "--json", "--", "true")
    assert json.loads(result.stdout)["reason"] == "dev-ref-unknown"
    result = invoke("--ref", "HEAD", "--node", "elsewhere", "--json", "--", "true")
    assert json.loads(result.stdout)["reason"] == "dev-run-invalid"
    stand_in["installed"]["dev"] = None
    result = invoke("--ref", "HEAD", "--json", "--", "true")
    assert (
        result.exit_code == 2
        and json.loads(result.stdout)["reason"] == "dev-not-enabled"
    )


def test_without_ref_the_working_tree_runs_as_it_is(stand_in: dict[str, Any]) -> None:
    Path("ok.sh").write_text("echo edited, not committed")
    result = invoke("--json", "--", "sh", "ok.sh")
    assert result.exit_code == 0, result.stderr
    body = json.loads(result.stdout)
    assert "edited, not committed" in result.stderr
    assert body["sources"]["shop"]["dirty"] is True


class QueuePort:
    """Jobs and a scheduler status, for dev status, logs and cancel."""

    def __init__(self) -> None:
        from tests.unit.dev.test_scheduler import job

        self.items = [
            job("r-running", requester="alice", suspend=False),
            job("r-agent", requester="bob", created="2026-10-08T07:00:01Z"),
            job(
                "r-round",
                requester="coordinator",
                priority="round",
                created="2026-10-08T07:00:09Z",
            ),
        ]
        self.deleted: list[str] = []

    def jobs(self, selector: str | None = None) -> list[dict[str, Any]]:
        return self.items

    def job(self, run: str) -> dict[str, Any] | None:
        return next(
            (
                j
                for j in self.items
                if j["metadata"]["labels"]["piceli.io/dev-run"] == run
            ),
            None,
        )

    def pod(self, run: str) -> dict[str, Any] | None:
        return {"metadata": {"name": f"{run}-pod"}} if run == "r-running" else None

    def logs(self, run: str, tail: int | None = None) -> str:
        return "compiling\nPICELI-DEV-RESULT {}\n"

    def delete(self, run: str) -> None:
        self.deleted.append(run)

    def node(self, name: str) -> dict[str, Any]:
        return {"status": {"allocatable": {"cpu": "8", "memory": "16Gi"}}}

    def status(self) -> dict[str, Any]:
        return {
            "scheduler_at": "2000-01-01T00:00:00Z",
            "slots": 1,
            "recent": [
                {
                    "run": "r-old",
                    "state": "failed",
                    "requester": "bob",
                    "log_tail": ["boom"],
                }
            ],
            "cache": {"used_bytes": 3 * 2**30, "max_bytes": 100 * 2**30},
        }


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> QueuePort:
    import piceli.k8s.cli.dev as dev_cli

    port = QueuePort()

    @contextmanager
    def opened(*_args: Any) -> Iterator[tuple[QueuePort, DevBuilds]]:
        yield port, DEV

    monkeypatch.setattr(dev_cli, "_port", opened)
    return port


def dev(*args: str) -> Any:
    return CliRunner().invoke(cli, ["dev", *args, "--cluster", "infra.py:c"])


def test_status_shows_slots_queue_order_recent_and_cache(queue: QueuePort) -> None:
    result = dev("status", "--json")
    assert result.exit_code == 0, result.stderr
    view = json.loads(result.stdout)
    assert view["slots"] == 1 and view["scheduler"]["alive"] is False
    assert [r["run"] for r in view["running"]] == ["r-running"]
    assert [(q["run"], q["position"]) for q in view["queued"]] == [
        ("r-round", 1),
        ("r-agent", 2),
    ]
    assert (
        view["recent"][0]["run"] == "r-old" and view["cache"]["used_bytes"] == 3 * 2**30
    )
    assert "1/1 slots busy, 2 queued" in result.stderr


def test_logs_of_a_live_and_a_finished_run(queue: QueuePort) -> None:
    live = dev("logs", "r-running")
    assert live.exit_code == 0 and live.stdout == "compiling\n"
    finished = dev("logs", "r-old")
    assert finished.exit_code == 0 and finished.stdout == "boom\n"
    unknown = dev("logs", "nope")
    assert (
        unknown.exit_code == 2 and "dev-run-unknown" in unknown.stdout + unknown.stderr
    )


def test_cancel_deletes_a_queued_run_only_once(queue: QueuePort) -> None:
    result = dev("cancel", "r-agent", "--json")
    assert result.exit_code == 0 and json.loads(result.stdout)["state"] == "cancelled"
    assert queue.deleted == ["r-agent"]
    assert dev("cancel", "nope").exit_code == 2
