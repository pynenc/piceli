"""``piceli registry status|install|uninstall`` take a declared ``Cluster``
(``infra.py:CLUSTER``) or a composition module (``infra.py``) as the registry
reference, reaching the cluster through the Cluster's credential profile."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app
from piceli.profiles import save_profile
from piceli.testing import FakeAPI, fake_cluster, write_kubeconfig


@pytest.fixture
def declared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeAPI]:
    api = FakeAPI(namespace="piceli-system")
    monkeypatch.setenv("PICELI_PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.chdir(tmp_path)
    with fake_cluster(api) as running:
        save_profile(
            "my-cluster", write_kubeconfig(running.url, tmp_path / "kubeconfig"), "fake"
        )
        (tmp_path / "infra.py").write_text(
            f"""
from piceli import Registry
from piceli.infra import Cluster, Node

my_cluster = Cluster(
    "my-cluster",
    api="{running.url}",
    credentials="my-cluster",
    nodes=[Node("node-a", arch="arm64", roles=["registry"])],
    registry=Registry.in_cluster(on="node-a", storage="2Gi"),
)
"""
        )
        (tmp_path / "bare.py").write_text(
            f"""
from piceli.infra import Cluster

bare = Cluster("my-cluster", api="{running.url}", credentials="my-cluster")
"""
        )
        yield api


def _run(*args: str) -> Any:
    return CliRunner().invoke(app, ["registry", *args, "--transport", "loopback-http"])


@pytest.mark.parametrize("ref", ["infra.py:my_cluster", "infra.py"])
def test_status_install_uninstall_accept_a_cluster(declared: FakeAPI, ref: str) -> None:
    status = _run("status", ref, "--json")
    assert status.exit_code == 0, status.output
    body = json.loads(status.stdout)
    assert body["state"] == "not-installed"
    assert body["host"] == "piceli-registry.piceli-system.svc:5000"

    planned = _run("install", ref)
    assert planned.exit_code == 3, planned.output
    plan = json.loads(planned.stdout)
    assert plan["registry"]["node"] == "node-a"
    assert plan["approve_command"].startswith(f"piceli registry install {ref} ")
    done = _run("install", ref, "--approve", plan["plan_hash"])
    assert done.exit_code == 0, done.output
    claim = declared.objects[("PersistentVolumeClaim", "piceli-registry-storage")]
    assert claim["spec"]["resources"]["requests"]["storage"] == "2Gi"

    removal = _run("uninstall", ref)
    assert removal.exit_code == 3, removal.output


def test_a_cluster_without_an_in_cluster_registry_is_refused(declared: FakeAPI) -> None:
    result = _run("status", "bare.py:bare")
    assert json.loads(result.stdout)["reason"] == "cluster-registry-invalid"


def test_a_profile_for_another_api_is_refused(
    declared: FakeAPI, tmp_path: Path
) -> None:
    other = write_kubeconfig("http://127.0.0.1:9", tmp_path / "other")
    save_profile("my-cluster", other, "fake")
    result = _run("status", "infra.py:my_cluster")
    assert json.loads(result.stdout)["reason"] == "cluster-api-mismatch"
    assert not declared.requests
