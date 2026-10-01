"""``piceli gitops enable infra.py`` and ``gitops sync`` (fake API, local state)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.gitops.install import CONFIG_MAP
from piceli.infra.controller import CONFIG_SCHEMA, CompositionConfig
from piceli.k8s.cli import app
from piceli.testing import FakeAPI, fake_cluster, write_kubeconfig

IMAGE = "registry.example.com/piceli@sha256:" + "a" * 64
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cluster(tmp_path: Path) -> Iterator[tuple[FakeAPI, Path]]:
    api = FakeAPI(namespace="piceli-system")
    with fake_cluster(api) as running:
        yield api, write_kubeconfig(running.url, tmp_path / "kubeconfig")


def _enable(kubeconfig: Path, *extra: str) -> Any:
    return CliRunner().invoke(
        app,
        [
            "gitops", "enable", "examples/composition/infra.py",
            "--root", str(ROOT), "--image", IMAGE,
            "--credentials-secret", "git-token",
            "--kubeconfig", str(kubeconfig), "--context", "fake",
            "--transport", "loopback-http", *extra,
        ],
    )  # fmt: skip


def test_enable_installs_a_composition_controller(cluster: tuple[FakeAPI, Path]) -> None:
    api, kubeconfig = cluster
    planned = _enable(kubeconfig)
    assert planned.exit_code == 3, planned.output
    body = json.loads(planned.stdout)
    assert body["controller"]["schema"] == CONFIG_SCHEMA
    composition = body["controller"]["composition"]
    assert [s["name"] for s in composition["sources"]] == ["catalog", "shop"]
    assert [e["name"] for e in composition["environments"]] == ["main", "rc", "branches"]
    assert "environment main (shop-main): follows" in planned.stderr
    done = _enable(kubeconfig, "--approve", body["plan_hash"])
    assert done.exit_code == 0, done.output
    stored = api.objects[("ConfigMap", CONFIG_MAP)]["data"]["config.json"]
    config = CompositionConfig.from_dict(json.loads(stored))
    assert config.model.name == "shop"
    again = _enable(kubeconfig)
    assert json.loads(again.stdout)["state"] == "unchanged"


def test_enable_refuses_a_broken_composition(
    cluster: tuple[FakeAPI, Path], tmp_path: Path
) -> None:
    _, kubeconfig = cluster
    module = tmp_path / "infra.py"
    module.write_text("environments = []\n")
    result = CliRunner().invoke(
        app,
        [
            "gitops", "enable", str(module), "--image", IMAGE,
            "--kubeconfig", str(kubeconfig), "--context", "fake",
            "--transport", "loopback-http",
        ],
    )  # fmt: skip
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "composition-invalid"


def test_sync_writes_a_request(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["gitops", "sync", "main", "--component", "web", "--state-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["state"] == "requested" and body["component"] == "web"
    requests = json.loads((tmp_path / "requests.json").read_text())
    (entry,) = requests.values()
    assert entry["kind"] == "sync" and entry["env"] == "main"
