"""``piceli gitops enable infra.py`` and ``gitops sync`` (fake API, local state)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.gitops.install import CONFIG_MAP, NAME
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


def test_enable_installs_a_composition_controller(
    cluster: tuple[FakeAPI, Path],
) -> None:
    api, kubeconfig = cluster
    planned = _enable(kubeconfig)
    assert planned.exit_code == 3, planned.output
    body = json.loads(planned.stdout)
    assert body["controller"]["schema"] == CONFIG_SCHEMA
    composition = body["controller"]["composition"]
    assert [s["name"] for s in composition["sources"]] == ["catalog", "shop"]
    assert [e["name"] for e in composition["environments"]] == [
        "main",
        "rc",
        "branches",
    ]
    assert "environment main (shop-main): follows" in planned.stderr
    done = _enable(kubeconfig, "--approve", body["plan_hash"])
    assert done.exit_code == 0, done.output
    stored = api.objects[("ConfigMap", CONFIG_MAP)]["data"]["config.json"]
    config = CompositionConfig.from_dict(json.loads(stored))
    assert config.model.name == "shop"
    pod = api.objects[("Deployment", NAME)]["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "my-cluster-worker"}
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
        app,
        ["gitops", "sync", "main", "--component", "web", "--state-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["state"] == "requested" and body["component"] == "web"
    requests = json.loads((tmp_path / "requests.json").read_text())
    (entry,) = requests.values()
    assert entry["kind"] == "sync" and entry["env"] == "main"


OLD = "registry.example.com/piceli@sha256:" + "0" * 64


def _with_ui(tmp_path: Path, controller_image: str | None) -> Path:
    """The example composition whose Cluster declares the in-cluster UI."""
    source = (ROOT / "examples" / "composition" / "infra.py").read_text()
    image = "" if controller_image is None else f', image="{controller_image}"'
    text = source.replace(
        "from piceli.infra import Cluster, Component, Controller, Source",
        "from piceli.infra import Cluster, Component, Controller, Source, Ui",
    ).replace(
        "controller=Controller(on=REGISTRY_NODE),",
        f"controller=Controller(on=REGISTRY_NODE{image}),\n    ui=Ui(),",
    )
    assert text.count("Ui()") == 1
    path = tmp_path / "infra.py"
    path.write_text(text)
    return path


def _install_ui(api: FakeAPI, image: str) -> None:
    """What `piceli cluster init` installed earlier: the UI on ``image``."""
    from piceli.infra import Cluster, Controller, Ui
    from piceli.infra.ui_install import render_ui

    cluster = Cluster(
        "my-cluster",
        api="https://127.0.0.1:6443",
        credentials="my-cluster",
        controller=Controller(on="my-cluster-worker", image=image),
        ui=Ui(),
    )
    for item in render_ui(cluster):
        api.put(item)


def _ui_image(api: FakeAPI) -> str:
    pod = api.objects[("Deployment", "piceli-ui")]["spec"]["template"]["spec"]
    return str(pod["containers"][0]["image"])


@pytest.mark.parametrize("declared", [IMAGE, None], ids=["controller-image", "none"])
def test_enable_upgrades_the_in_cluster_ui_with_the_controller(
    cluster: tuple[FakeAPI, Path], tmp_path: Path, declared: str | None
) -> None:
    api, kubeconfig = cluster
    _install_ui(api, OLD)
    module = _with_ui(tmp_path, declared)

    def enable(*extra: str) -> Any:
        return CliRunner().invoke(
            app,
            [
                "gitops", "enable", str(module), "--root", str(tmp_path),
                "--image", IMAGE, "--credentials-secret", "git-token",
                "--kubeconfig", str(kubeconfig), "--context", "fake",
                "--transport", "loopback-http", *extra,
            ],
        )  # fmt: skip

    planned = enable()
    assert planned.exit_code == 3, planned.output
    body = json.loads(planned.stdout)
    assert body["ui"] == "included"
    changes = {(c["kind"], c["name"]): c["operation"] for c in body["changes"]}
    assert changes[("Deployment", "piceli-ui")] == "apply"
    assert changes[("Deployment", NAME)] == "create"
    assert f"piceli-ui in piceli-system runs {IMAGE}" in planned.stderr
    assert _ui_image(api) == OLD  # nothing changes before the approval
    done = enable("--approve", body["plan_hash"])
    assert done.exit_code == 0, done.output
    assert _ui_image(api) == IMAGE
    pod = api.objects[("Deployment", NAME)]["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == IMAGE
    again = enable()
    assert json.loads(again.stdout)["state"] == "unchanged"
    # The stored declaration `cluster init` reads is the composition's (so a
    # later `cluster init` plans no change of it).
    from piceli.infra.cluster_init import cluster_config
    from piceli.infra.composition import load_composition

    stored = api.objects[("ConfigMap", "piceli-cluster")]["data"]
    declared_cluster = load_composition(str(module), tmp_path).cluster
    assert stored == cluster_config(declared_cluster)["data"]


def test_enable_uses_the_controllers_declared_poll(
    cluster: tuple[FakeAPI, Path], tmp_path: Path
) -> None:
    api, kubeconfig = cluster
    module = _with_ui(tmp_path, IMAGE)
    module.write_text(
        module.read_text().replace(
            "Controller(on=REGISTRY_NODE,", 'Controller(on=REGISTRY_NODE, poll="5m",'
        )
    )
    planned = CliRunner().invoke(
        app,
        [
            "gitops", "enable", str(module), "--root", str(tmp_path),
            "--image", IMAGE, "--credentials-secret", "git-token",
            "--kubeconfig", str(kubeconfig), "--context", "fake",
            "--transport", "loopback-http",
        ],
    )  # fmt: skip
    assert planned.exit_code == 3, planned.output
    assert json.loads(planned.stdout)["controller"]["poll_seconds"] == 300


def test_enable_without_a_declared_ui_leaves_the_ui_alone(
    cluster: tuple[FakeAPI, Path],
) -> None:
    api, kubeconfig = cluster
    _install_ui(api, OLD)
    planned = _enable(kubeconfig)
    body = json.loads(planned.stdout)
    assert body["ui"] == "not-declared"
    assert all(c["name"] != "piceli-ui" for c in body["changes"])
    done = _enable(kubeconfig, "--approve", body["plan_hash"])
    assert done.exit_code == 0, done.output
    assert _ui_image(api) == OLD
