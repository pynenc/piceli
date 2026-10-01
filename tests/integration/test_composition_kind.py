"""Opt-in: a composition end to end on a disposable MULTI-NODE kind cluster.

Two local Git source repositories (bare remotes in ``tmp_path``, the
contents of ``examples/composition/shop`` and ``catalog``), the composition
``examples/composition/infra.py`` and its three environments:

1. ``piceli registry install`` puts the in-cluster registry on one worker.
2. ``piceli gitops run --once --local-build`` (the composition controller,
   building on this machine and pushing through a port-forward) deploys
   ``main``: every component built or mirrored once, all ``synced``.
3. A change in ``web``'s files rebuilds and rolls only ``web``: ``api``,
   ``catalog`` and ``cache`` keep their image digest (``unchanged``) and
   their workloads are not touched (same generation).
4. A ``wp-*`` branch of ``shop`` gets its environment on the chosen node,
   whose pods pull from the in-cluster registry (not the node it runs on).
5. A new ``v*`` tag of ``shop`` deploys ``rc``.

Needs at least two nodes (skipped otherwise), ``docker``, ``kubectl``,
``git`` and network access to pull the pinned ``busybox`` base. The cluster's
containerd must read ``/etc/containerd/certs.d`` (``config_path``), for
example::

    cat > kind.yaml <<EOF
    kind: Cluster
    apiVersion: kind.x-k8s.io/v1alpha4
    nodes: [{role: control-plane}, {role: worker}, {role: worker}]
    containerdConfigPatches:
    - |-
      [plugins."io.containerd.grpc.v1.cri".registry]
        config_path = "/etc/containerd/certs.d"
    EOF
    kind create cluster --name piceli-wpd --config kind.yaml --kubeconfig wpd.kubeconfig
    PICELI_KIND_KUBECONFIG=wpd.kubeconfig PICELI_KIND_CONTEXT=kind-piceli-wpd \\
      uv run pytest tests/integration/test_composition_kind.py

It never reads the ambient kubeconfig and removes what it creates.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from kind_support import kubectl
from typer.testing import CliRunner

from piceli.infra.composition import load_composition
from piceli.infra.controller import CompositionConfig
from piceli.k8s.cli import app
from tests.unit.infra.git_support import Repo, example_files

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "composition"
HOST = "piceli-registry.piceli-system.svc:5000"
NAMESPACES = ("shop-main", "shop-rc", "shop-wp-demo")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(1800),
    pytest.mark.skipif(
        not (
            KUBECONFIG
            and CONTEXT
            and shutil.which("docker")
            and shutil.which("kubectl")
            and shutil.which("git")
        ),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT; needs docker, "
        "kubectl and git",
    ),
]


def _nodes() -> list[dict[str, Any]]:
    found = json.loads(kubectl("get", "nodes", "-o", "json"))
    return sorted(found["items"], key=lambda item: item["metadata"]["name"])


@pytest.fixture(scope="module")
def placement() -> dict[str, str]:
    nodes = _nodes()
    if len(nodes) < 2:
        pytest.skip("the composition test needs a cluster with >= 2 nodes")
    workers = [
        n
        for n in nodes
        if "node-role.kubernetes.io/control-plane" not in n["metadata"]["labels"]
    ]
    chosen = workers if len(workers) >= 2 else nodes
    return {
        "registry": chosen[0]["metadata"]["name"],
        "branch": chosen[-1]["metadata"]["name"],
        "platform": "linux/" + chosen[0]["metadata"]["labels"]["kubernetes.io/arch"],
    }


def _cli(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def _registry(command: str, *extra: str) -> dict[str, Any]:
    base = ["registry", command, "--kubeconfig", KUBECONFIG, "--context", CONTEXT]
    planned = _cli(*base, *extra)
    assert planned.exit_code in (0, 3), planned.output
    body = json.loads(planned.stdout)
    if body["state"] == "unchanged":
        return dict(body)
    done = _cli(*base, *extra, "--approve", body["plan_hash"])
    assert done.exit_code == 0, done.output
    return dict(json.loads(done.stdout))


def _wait_registry() -> None:
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        status = _cli(
            "registry", "status", "--kubeconfig", KUBECONFIG, "--context", CONTEXT, "--json"
        )
        if status.exit_code == 0 and json.loads(status.stdout)["state"] == "ready":
            return
        time.sleep(3)
    raise AssertionError("the in-cluster registry did not become ready")


def _poll(config: Path, state: Path) -> dict[str, Any]:
    result = _cli(
        "gitops", "run", "--config", str(config), "--state-dir", str(state),
        "--kubeconfig", KUBECONFIG, "--context", CONTEXT, "--once", "--local-build",
    )  # fmt: skip
    assert result.exit_code == 0, result.output[-4000:]
    return dict(json.loads(result.stdout))


def _workload(namespace: str, kind: str, name: str) -> dict[str, Any]:
    return dict(json.loads(kubectl("get", kind, name, "-o", "json", namespace=namespace)))


def _states(status: dict[str, Any], env: str) -> dict[str, str]:
    return {k: v["state"] for k, v in status["envs"][env]["components"].items()}


def _digests(status: dict[str, Any], env: str) -> dict[str, str]:
    return {k: v["digest"] for k, v in status["envs"][env]["components"].items()}


def test_a_change_rolls_only_its_component_and_branches_pull_from_the_registry(
    placement: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = Repo(tmp_path / "remotes", "shop", example_files(EXAMPLE / "shop"))
    catalog = Repo(tmp_path / "remotes", "catalog", example_files(EXAMPLE / "catalog"))
    catalog.tag("v0.1.0")
    monkeypatch.setenv("COMPOSITION_SHOP_URL", shop.url)
    monkeypatch.setenv("COMPOSITION_CATALOG_URL", catalog.url)
    monkeypatch.setenv("COMPOSITION_REGISTRY_NODE", placement["registry"])
    monkeypatch.setenv("COMPOSITION_BRANCH_NODE", placement["branch"])
    composition = load_composition(EXAMPLE / "infra.py")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            CompositionConfig(
                composition=composition.to_dict(),
                poll_seconds=10,
                platforms=(placement["platform"],),
            ).to_dict()
        )
    )
    state = tmp_path / "state"
    try:
        _registry("install", "--on", placement["registry"], "--storage", "1Gi")
        _wait_registry()

        # 1. main: every component built (or mirrored) once and synced.
        status = _poll(config, state)
        main = status["envs"]["main"]
        assert main["state"] == "deployed", main
        assert set(_states(status, "main").values()) == {"synced"}
        assert set(main["revision"]) == {"shop", "catalog"}
        assert "rc" not in status["envs"]  # existing tags are the baseline
        for component in ("web", "api", "catalog", "cache"):
            assert main["components"][component]["image"].startswith(f"{HOST}/shop/")
        before = {
            ("deployment", "api"): _workload("shop-main", "deployment", "api"),
            ("deployment", "cache"): _workload("shop-main", "deployment", "cache"),
            ("statefulset", "catalog"): _workload("shop-main", "statefulset", "catalog"),
        }
        web_before = _workload("shop-main", "deployment", "web")
        first = _digests(status, "main")

        # 2. A change in web's files: only web rebuilds and rolls.
        shop.commit({"web/index.html": "<h1>shop, second edition</h1>\n"})
        status = _poll(config, state)
        assert status["envs"]["main"]["state"] == "deployed"
        assert _states(status, "main") == {
            "web": "synced",
            "api": "unchanged",
            "catalog": "unchanged",
            "cache": "unchanged",
        }
        after = _digests(status, "main")
        assert after["web"] != first["web"]
        assert {k: v for k, v in after.items() if k != "web"} == {
            k: v for k, v in first.items() if k != "web"
        }
        for (kind, name), old in before.items():
            new = _workload("shop-main", kind, name)
            assert new["metadata"]["generation"] == old["metadata"]["generation"], name
        web = _workload("shop-main", "deployment", "web")
        assert web["metadata"]["generation"] > web_before["metadata"]["generation"]
        image = web["spec"]["template"]["spec"]["containers"][0]["image"]
        assert image == status["envs"]["main"]["components"]["web"]["image"]

        # 3. A branch environment on the chosen node, pulling from the registry.
        shop.commit({"web/index.html": "<h1>demo</h1>\n"}, branch="wp-demo")
        status = _poll(config, state)
        branch = status["envs"]["wp-demo"]
        assert branch["state"] == "deployed", branch
        assert branch["namespace"] == "shop-wp-demo"
        assert set(branch["components"]) == {"web", "catalog"}
        pods = json.loads(
            kubectl("get", "pods", "-o", "json", namespace="shop-wp-demo")
        )["items"]
        running = [p for p in pods if p["status"].get("phase") == "Running"]
        assert {p["spec"]["nodeName"] for p in running} == {placement["branch"]}
        for pod in running:
            for container in pod["status"]["containerStatuses"]:
                assert container["image"].startswith(HOST) or HOST in container[
                    "imageID"
                ] or container["ready"], container
            assert pod["spec"]["containers"][0]["image"].startswith(f"{HOST}/shop/")
        assert placement["branch"] != placement["registry"]

        # 4. A new tag deploys rc (three environments now).
        shop.tag("v1.0.0")
        status = _poll(config, state)
        rc = status["envs"]["rc"]
        assert rc["state"] == "deployed", rc
        assert rc["refs"] == {"catalog": "refs/tags/v0.1.0", "shop": "refs/tags/v1.0.0"}
        assert {"main", "rc", "wp-demo"} <= set(status["envs"])
        assert status["sources"]["shop"]["refs"]["refs/tags/v1.0.0"]
    finally:
        for namespace in NAMESPACES:
            kubectl("delete", "namespace", namespace, "--wait=false", check=False)
        _registry("uninstall", "--delete-storage")
