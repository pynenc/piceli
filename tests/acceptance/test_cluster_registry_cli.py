"""``piceli registry install|status|uninstall`` and a deploy to ``Registry.in_cluster``.

The CLI runs against the in-process fake API: the install is planned with a
hash (exit 3), applied only with that hash and idempotent; the uninstall keeps
the namespace and the data. A pipeline delivering to the in-cluster registry
is refused until the registry is installed, pushes through a port-forward to
its Service and releases workloads that pull by the stable name.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app
from piceli.testing import FakeAPI, fake_cluster, write_kubeconfig
from tests.acceptance.test_deploy_pipeline import FakeBackend, deploy, image

HOST = "piceli-registry.piceli-system.svc:5000"


@pytest.fixture
def cluster(tmp_path: Path) -> Iterator[tuple[FakeAPI, Path]]:
    api = FakeAPI(namespace="piceli-system")
    with fake_cluster(api) as running:
        yield api, write_kubeconfig(running.url, tmp_path / "kubeconfig")


def _registry(kubeconfig: Path, command: str, *extra: str) -> Any:
    return CliRunner().invoke(
        app,
        [
            "registry",
            command,
            "--kubeconfig",
            str(kubeconfig),
            "--context",
            "fake",
            "--transport",
            "loopback-http",
            *extra,
        ],
    )


def test_install_plans_then_installs_with_the_hash(
    cluster: tuple[FakeAPI, Path],
) -> None:
    api, kubeconfig = cluster
    planned = _registry(kubeconfig, "install", "--on", "node-a", "--storage", "5Gi")
    assert planned.exit_code == 3, planned.output
    body = json.loads(planned.stdout)
    assert body["state"] == "approval-required"
    assert body["schema"] == "piceli.cluster-registry-plan.v1"
    assert body["registry"]["host"] == HOST
    changes = {(c["kind"], c["name"]): c["operation"] for c in body["changes"]}
    assert changes[("DaemonSet", "piceli-registry-mirror")] == "create"
    assert changes[("Deployment", "piceli-registry")] == "create"
    assert ("Deployment", "piceli-registry") not in api.objects  # nothing ran
    assert "--on node-a --storage 5Gi" in body["approve_command"]

    wrong = _registry(kubeconfig, "install", "--on", "node-a", "--storage", "5Gi",
                      "--approve", "sha256:" + "0" * 64)  # fmt: skip
    assert wrong.exit_code == 2
    assert json.loads(wrong.stdout)["reason"] == "cluster-registry-plan-changed"

    done = _registry(kubeconfig, "install", "--on", "node-a", "--storage", "5Gi",
                     "--approve", body["plan_hash"])  # fmt: skip
    assert done.exit_code == 0, done.output
    assert json.loads(done.stdout)["state"] == "installed"
    pod = api.objects[("Deployment", "piceli-registry")]["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "node-a"}
    claim = api.objects[("PersistentVolumeClaim", "piceli-registry-storage")]
    assert claim["spec"]["resources"]["requests"]["storage"] == "5Gi"
    assert ("DaemonSet", "piceli-registry-mirror") in api.objects

    again = _registry(kubeconfig, "install", "--on", "node-a", "--storage", "5Gi")
    assert again.exit_code == 0, again.output
    assert json.loads(again.stdout)["state"] == "unchanged"

    status = _registry(kubeconfig, "status", "--json")
    assert status.exit_code == 0, status.output
    report = json.loads(status.stdout)
    assert report["schema"] == "piceli.cluster-registry-status.v1"
    assert report["state"] != "not-installed"
    assert report["node"] == "node-a"  # read from the live Deployment
    assert report["storage"]["claim"] == "piceli-registry-storage"

    removal = _registry(kubeconfig, "uninstall")
    assert removal.exit_code == 3, removal.output
    plan = json.loads(removal.stdout)
    assert plan["kept"] == [
        "Namespace/piceli-system",
        "PersistentVolumeClaim/piceli-registry-storage",
    ]
    removed = _registry(kubeconfig, "uninstall", "--approve", plan["plan_hash"])
    assert removed.exit_code == 0, removed.output
    assert ("Deployment", "piceli-registry") not in api.objects
    assert ("DaemonSet", "piceli-registry-mirror") not in api.objects
    assert ("PersistentVolumeClaim", "piceli-registry-storage") in api.objects

    gone = _registry(kubeconfig, "status", "--json")
    assert json.loads(gone.stdout)["state"] == "not-installed"


def test_install_refusals(cluster: tuple[FakeAPI, Path], tmp_path: Path) -> None:
    _, kubeconfig = cluster
    result = _registry(kubeconfig, "install")
    assert json.loads(result.stdout)["reason"] == "cluster-registry-invalid"
    result = _registry(kubeconfig, "install", "--on", "node-a", "--image", "registry:3")
    assert json.loads(result.stdout)["reason"] == "cluster-registry-invalid"
    result = CliRunner().invoke(app, ["registry", "install", "--on", "node-a"])
    assert json.loads(result.stdout)["reason"] == "cluster-registry-target-required"
    module = tmp_path / "infra.py"
    module.write_text(
        "from piceli import Registry\nregistry = Registry.in_cluster(on='node-a')\n"
    )
    both = _registry(kubeconfig, "install", f"{module}:registry", "--on", "node-b")
    assert json.loads(both.stdout)["reason"] == "cluster-registry-invalid"
    declared = _registry(kubeconfig, "install", f"{module}:registry")
    assert declared.exit_code == 3, declared.output
    assert json.loads(declared.stdout)["registry"]["node"] == "node-a"


# ------------------------------------------------------------ deploy


class ClusterRegistryBackend(FakeBackend):
    state = "ready"
    routes: list[Any] = []

    def cluster_registry_state(self, target: Any, strategy: Any) -> str:
        return self.state

    def registry_deliver(self, route, image_id, repository):  # type: ignore[no-untyped-def]
        self.routes.append(route)
        self.calls.append("deliver")
        manifest = "sha256:" + hashlib.sha256(image_id.encode()).hexdigest()
        self.registry.setdefault(repository, set()).add(manifest)
        return {
            "schema": "piceli.registry-delivery.v1",
            "result": "pushed",
            "state": "succeeded",
            "approved_digest": image_id,
            "image": {"config_digest": image_id, "manifest_digest": manifest},
            "target": {"registry": None, "repository": repository},
            "node_registry": route.node_registry,
            "pull_ref": f"{route.node_registry}/{repository}@{manifest}",
        }


@pytest.fixture
def in_cluster(shop: tuple[Any, Path], monkeypatch: pytest.MonkeyPatch) -> Any:
    api, tmp_path = shop
    monkeypatch.setattr("piceli.pipeline.runner.Backend", ClusterRegistryBackend)
    ClusterRegistryBackend.state = "ready"
    ClusterRegistryBackend.routes = []
    text = (tmp_path / "app.py").read_text()
    (tmp_path / "app.py").write_text(
        text.replace(
            'Registry("oci://registry.example:5000/shop")',
            'Registry.in_cluster(on="node-a")',
        )
    )
    return api, tmp_path


def test_deploy_pulls_from_the_stable_name(in_cluster: Any) -> None:
    api, tmp_path = in_cluster
    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 0, result.stdout + result.stderr
    deliver = events[-1]["stages"]["deliver"]
    assert deliver["strategy"]["strategy"] == "cluster-registry"
    assert deliver["registry"] == {
        "action": "unchanged",
        "host": HOST,
        "namespace": "piceli-system",
        "node": "node-a",
        "state": "ready",
    }
    assert f"registry {HOST}: ready" in result.stderr
    code, events, result = deploy(
        tmp_path, "--approve", events[-1]["combined_hash"], "--json"
    )
    assert code == 0, result.stdout + result.stderr
    (route,) = ClusterRegistryBackend.routes
    assert route.forward == "service/piceli-registry"
    assert route.namespace == "piceli-system" and route.push is None
    assert image(api).startswith(f"{HOST}/shop/web@sha256:")


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("not-installed", "cluster-registry-not-installed"),
        ("starting", "cluster-registry-not-ready"),
    ],
)
def test_deploy_refuses_until_the_registry_serves(
    in_cluster: Any, state: str, reason: str
) -> None:
    _, tmp_path = in_cluster
    ClusterRegistryBackend.state = state
    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 2, result.stdout + result.stderr
    assert events[-1]["reason"] == reason
