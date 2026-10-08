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
    result = invoke("--json", "--", "true")
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
