"""``piceli.infra.Cluster``: validation, role labels, the API check, node plans."""

from __future__ import annotations

from typing import Any

import pytest

from piceli import Registry
from piceli.gitops.config import ControllerConfig
from piceli.gitops.install import InstallSettings, render_controller, render_foundation
from piceli.infra import Cluster, Controller, Node, Ui
from piceli.infra.cluster import ClusterError, api_matches, kubeconfig_server
from piceli.infra.cluster_init import node_runtime, plan_nodes, render_objects

IMAGE = "example.com/piceli@sha256:" + "a" * 64


def _cluster(**overrides: Any) -> Cluster:
    values: dict[str, Any] = {
        "api": "https://10.0.0.1:6443",
        "credentials": "my-cluster",
        "nodes": [
            Node("node-a", arch="amd64", roles=["builder", "registry"]),
            Node("node-b", arch="arm64", roles=["workloads"]),
        ],
        "registry": Registry.in_cluster(on="node-a"),
        "controller": Controller(on="node-a"),
        "ui": Ui(),
    }
    values.update(overrides)
    return Cluster("my-cluster", **values)


def test_lists_become_tuples_and_roles_become_labels() -> None:
    cluster = _cluster()
    assert cluster.nodes[0].roles == ("builder", "registry")
    assert cluster.nodes[0].labels == {
        "piceli.io/role-builder": "true",
        "piceli.io/role-registry": "true",
        "piceli.io/builder": "true",
    }
    assert cluster.nodes[1].labels == {"piceli.io/role-workloads": "true"}
    assert cluster.node("node-b") is cluster.nodes[1]
    described = cluster.describe()
    assert described["controller"]["poll_seconds"] == 60
    assert described["registry"]["host"] == "piceli-registry.piceli-system.svc:5000"


@pytest.mark.parametrize(
    "build",
    [
        lambda: Node("Node_A", arch="amd64"),
        lambda: Node("a", arch="x86"),  # type: ignore[arg-type]
        lambda: Node("a", arch="amd64", roles=["Bad Role"]),
        lambda: Node("a", arch="amd64", roles="builder"),  # type: ignore[arg-type]
        lambda: Node("a", arch="amd64", roles=["x", "x"]),
        lambda: Controller(on="a", poll="5s"),
        lambda: Controller(on="a", poll="soon"),
        lambda: Controller(on="a", image="piceli:latest"),
        lambda: Ui(access="public"),  # type: ignore[arg-type]
        lambda: _cluster(api="10.0.0.1:6443"),
        lambda: _cluster(credentials="../x"),
        lambda: _cluster(registry=Registry("oci://example.com/team")),
        lambda: _cluster(controller=Controller(on="node-z")),
        lambda: _cluster(registry=Registry.in_cluster(on="node-z")),
        lambda: _cluster(nodes=[Node("a", arch="amd64"), Node("a", arch="amd64")]),
    ],
)
def test_invalid_declarations_are_refused(build: Any) -> None:
    with pytest.raises(ClusterError) as error:
        build()
    assert error.value.code == "cluster-invalid"


def test_api_matches_scheme_host_and_port() -> None:
    assert api_matches("https://10.0.0.1:6443", "https://10.0.0.1:6443/")
    assert api_matches("https://API.example.com", "https://api.example.com:443")
    assert not api_matches("https://10.0.0.1:6443", "https://10.0.0.2:6443")
    assert not api_matches("https://10.0.0.1:6443", "http://10.0.0.1:6443")
    assert not api_matches("https://10.0.0.1:6443", "not a url")


def test_kubeconfig_server_reads_the_context_cluster() -> None:
    document = {
        "contexts": [{"name": "c", "context": {"cluster": "k", "user": "u"}}],
        "clusters": [{"name": "k", "cluster": {"server": "https://h:6443"}}],
    }
    assert kubeconfig_server(document, "c") == "https://h:6443"
    assert kubeconfig_server(document, "other") is None


def _node(name: str, arch: str, **info: str) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "uid": f"uid-{name}"},
        "status": {"nodeInfo": {"architecture": arch, **info}},
    }


def test_node_runtime_from_node_info() -> None:
    assert node_runtime(_node("a", "amd64", kubeletVersion="v1.32.5+k3s1")) == "k3s"
    assert (
        node_runtime(_node("a", "amd64", containerRuntimeVersion="containerd://2.1"))
        == "containerd"
    )
    assert (
        node_runtime(_node("a", "amd64", containerRuntimeVersion="cri-o://1")) is None
    )


def test_plan_nodes_labels_runtime_only_for_auto_mirrors() -> None:
    live = [
        _node("node-a", "amd64", containerRuntimeVersion="containerd://2.1"),
        _node("node-b", "arm64", kubeletVersion="v1.32.5+k3s1"),
    ]
    auto = {c.node: c for c in plan_nodes(_cluster(), live)}
    assert auto["node-b"].set["piceli.io/runtime"] == "k3s"
    pinned = _cluster(registry=Registry.in_cluster(on="node-a", node_mirror="k3s"))
    fixed = {c.node: c for c in plan_nodes(pinned, live)}
    assert "piceli.io/runtime" not in fixed["node-b"].set


def test_init_objects_have_one_namespace_and_the_controller_foundation() -> None:
    objects = render_objects(_cluster(), None)
    keys = [(o["kind"], o["metadata"]["name"]) for o in objects]
    assert keys.count(("Namespace", "piceli-system")) == 1
    assert keys[:2] == [("Namespace", "piceli-system"), ("ConfigMap", "piceli-cluster")]
    assert ("PersistentVolumeClaim", "piceli-gitops-state") in keys
    assert ("Deployment", "piceli-gitops") not in keys
    without = render_objects(_cluster(controller=None, ui=None, registry=None), None)
    assert [o["kind"] for o in without] == ["Namespace", "ConfigMap"]


def test_render_controller_is_the_foundation_plus_its_workload() -> None:
    config = ControllerConfig(
        pipeline="deploy/app.py:pipeline",
        repo="https://example.com/app.git",
        branches=("main",),
    )
    objects = render_controller(config, InstallSettings(image=IMAGE))
    foundation = render_foundation("piceli-system")
    assert objects[: len(foundation)] == foundation
    assert [o["kind"] for o in objects[len(foundation) :]] == [
        "ConfigMap",
        "Deployment",
    ]
    pod = objects[-1]["spec"]["template"]["spec"]
    assert "nodeSelector" not in pod
    assert "node" not in InstallSettings(image=IMAGE).to_dict()
    pinned = render_controller(config, InstallSettings(image=IMAGE, node="node-a"))
    pod = pinned[-1]["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "node-a"}
