"""``Registry.in_cluster``: the model, its objects, the node mirror and pull refs."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from piceli import App, Pipeline, Registry, Target
from piceli.artifacts import cluster_registry as cr
from piceli.artifacts.delivery_inputs import DeliveryInputError
from piceli.artifacts.registry import (
    RegistryEndpoint,
    RegistryTarget,
    is_cluster_service,
)
from piceli.k8s.templates.deployable.node_local_registry import DEFAULT_REGISTRY_IMAGE
from piceli.pipeline import ClusterRegistry, PipelineError

HOST = "piceli-registry.piceli-system.svc:5000"
DIGEST = "sha256:" + "a" * 64


def _pipeline(tmp_path: Path, deliver: Any) -> Pipeline:
    (tmp_path / "build.toml").write_text("")
    target = Target.kubeconfig(tmp_path / "kubeconfig", context="ctx", namespace="shop")
    return Pipeline(App("shop"), target, deliver=deliver, state_dir=tmp_path / "s")


def _by_kind(objects: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["kind"]: item for item in objects}


# ------------------------------------------------------------------ model


def test_in_cluster_is_a_registry_with_a_stable_service_name() -> None:
    registry = Registry.in_cluster(on="node-a")
    assert isinstance(registry, ClusterRegistry) and isinstance(registry, Registry)
    assert registry.host == HOST
    assert registry.url == f"oci://{HOST}"
    assert (registry.storage, registry.port) == ("20Gi", 5000)
    assert registry.describe() == {
        "strategy": "cluster-registry",
        "host": HOST,
        "repository": None,
        "namespace": "piceli-system",
        "name": "piceli-registry",
        "node": "node-a",
        "storage": "20Gi",
    }
    custom = Registry.in_cluster(
        on="node-a", port=5001, namespace="infra", name="images", node_port=30500
    )
    assert custom.host == "images.infra.svc:5001"
    assert custom.describe()["node_port"] == 30500


@pytest.mark.parametrize(
    "kwargs",
    [
        {"on": ""},
        {"on": "Node_A"},
        {"on": "a", "storage": "20G"},
        {"on": "a", "port": 0},
        {"on": "a", "namespace": "Bad"},
        {"on": "a", "name": "x" * 60},
        {"on": "a", "image": "registry:3"},
        {"on": "a", "node_port": 8080},
        {"on": "a", "mirror_dir": "relative/certs.d"},
        {"on": "a", "mirror_dir": "/etc"},
        {"on": "a", "repository": "Team/App"},
    ],
)
def test_in_cluster_refuses_invalid_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(PipelineError) as raised:
        Registry.in_cluster(**kwargs)
    assert raised.value.code == "pipeline-invalid"


def test_the_pipeline_prefixes_images_with_the_app_name(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, Registry.in_cluster(on="node-a"))
    assert isinstance(pipeline.deliver, ClusterRegistry)
    assert pipeline.deliver.repository == "shop"
    assert pipeline.deliver.url == f"oci://{HOST}/shop"
    explicit = _pipeline(
        tmp_path, Registry.in_cluster(on="node-a", repository="team/shop")
    )
    assert explicit.deliver.url == f"oci://{HOST}/team/shop"  # type: ignore[union-attr]


def test_a_plain_registry_describes_itself_as_in_0_13() -> None:
    # The plan hash of a 0.13 pipeline hashes describe(): it must not change.
    assert Registry("oci://registry.example:5000/shop").describe() == {
        "strategy": "registry",
        "url": "oci://registry.example:5000/shop",
        "node_registry": None,
    }


# ------------------------------------------------------- plain HTTP rule


def test_plain_http_is_allowed_to_a_cluster_service_name_only() -> None:
    assert is_cluster_service("piceli-registry.piceli-system.svc")
    assert is_cluster_service("piceli-registry.piceli-system.svc.cluster.local")
    for host in ("registry.example", "a.svc", "a.b.svc.example.com", "a.b.c.svc"):
        assert not is_cluster_service(host)
    target = RegistryTarget.parse(f"oci://{HOST}/shop/web")
    assert target.tls is False and target.registry == HOST
    RegistryEndpoint("piceli-registry.piceli-system.svc", 5000, use_tls=False)
    with pytest.raises(DeliveryInputError) as raised:
        RegistryTarget.parse("oci://registry.example/a?tls=false")
    assert raised.value.code == "plain-http-not-loopback"
    assert RegistryTarget.parse("oci://registry.example/a").tls is True


# ------------------------------------------------------------ pull refs


def test_every_delivery_path_pulls_by_the_stable_name(tmp_path: Path) -> None:
    from piceli.artifacts.cluster_build import ClusterBuildConfig, _registry
    from piceli.envs.ops import pull_reference
    from piceli.pipeline.compose import mirror_node_registry, mirror_repository

    pipeline = _pipeline(
        tmp_path,
        Registry.in_cluster(on="node-a", mirror=["docker.io/library/redis@" + DIGEST]),
    )
    assert pull_reference(pipeline, "web", DIGEST) == f"{HOST}/shop/web@{DIGEST}"
    assert mirror_node_registry(pipeline) == HOST
    key = "docker.io/library/redis@" + DIGEST
    assert mirror_repository(pipeline, key) == "shop/mirror/docker.io/library/redis"
    # A cluster build Job pushes to the Service by name, on the pod network.
    config = ClusterBuildConfig(
        image="builder@sha256:" + "b" * 64, repo="https://git.example/app.git"
    )
    assert _registry(pipeline, config) == (f"oci://{HOST}/shop", HOST, False)


def test_a_laptop_pushes_through_a_forward_to_the_service(tmp_path: Path) -> None:
    from piceli.pipeline.runner import PipelineRunner

    pipeline = _pipeline(tmp_path, Registry.in_cluster(on="node-a"))
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.pipeline = pipeline
    route = runner._route()
    assert route.push is None
    assert route.forward == "service/piceli-registry"
    assert (route.namespace, route.remote_port) == ("piceli-system", 5000)
    assert route.node_registry == HOST
    assert route.context == "ctx"
    assert runner._repository("web") == "shop/web"


# ------------------------------------------------------------- objects


def test_render_installs_registry_service_and_node_agent() -> None:
    registry = Registry.in_cluster(on="node-a", storage="5Gi")
    objects = cr.render(registry)
    assert [o["kind"] for o in objects] == [
        "Namespace",
        "ConfigMap",
        "PersistentVolumeClaim",
        "Service",
        "Deployment",
        "DaemonSet",
    ]  # the Service exists before the agent starts (its address variable)
    kinds = _by_kind(objects)
    assert all(
        o["metadata"].get("namespace") == "piceli-system"
        for o in objects
        if o["kind"] != "Namespace"
    )
    claim = kinds["PersistentVolumeClaim"]
    assert claim["metadata"]["name"] == "piceli-registry-storage"
    assert claim["metadata"]["annotations"] == {"piceli.io/retained": "true"}
    assert claim["spec"]["resources"]["requests"]["storage"] == "5Gi"
    service = kinds["Service"]["spec"]
    assert service["type"] == "ClusterIP"
    assert service["ports"][0]["port"] == 5000
    pod = kinds["Deployment"]["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "node-a"}
    assert "hostNetwork" not in pod
    container = pod["containers"][0]
    assert container["image"] == DEFAULT_REGISTRY_IMAGE
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert '"addr": ":5000"' in kinds["ConfigMap"]["data"]["config.yml"]
    assert "debug" not in kinds["ConfigMap"]["data"]["config.yml"]
    agent = kinds["DaemonSet"]["spec"]["template"]["spec"]
    assert agent["tolerations"] == [{"operator": "Exists"}]
    assert agent["enableServiceLinks"] is True
    assert agent["volumes"][0]["hostPath"] == {
        "path": "/etc/containerd/certs.d",
        "type": "DirectoryOrCreate",
    }
    agent_container = agent["containers"][0]
    assert agent_container["image"] == DEFAULT_REGISTRY_IMAGE  # one pinned image
    assert agent_container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert agent_container["readinessProbe"]["exec"]["command"] == [
        "test",
        "-s",
        f"/host/certs.d/{HOST}/hosts.toml",
    ]
    assert "PICELI_REGISTRY_SERVICE_HOST" in agent_container["command"][2]


def test_render_options_node_port_storage_class_and_k3s_dir() -> None:
    registry = Registry.in_cluster(
        on="node-a",
        node_port=30500,
        storage_class="local-path",
        mirror_dir="/var/lib/rancher/k3s/agent/etc/containerd/certs.d",
    )
    kinds = _by_kind(cr.render(registry))
    assert kinds["Service"]["spec"]["type"] == "NodePort"
    assert kinds["Service"]["spec"]["ports"][0]["nodePort"] == 30500
    assert kinds["PersistentVolumeClaim"]["spec"]["storageClassName"] == "local-path"
    path = kinds["DaemonSet"]["spec"]["template"]["spec"]["volumes"][0]["hostPath"]
    assert path["path"] == "/var/lib/rancher/k3s/agent/etc/containerd/certs.d"
    assert cr.mirror_path(registry).startswith("/var/lib/rancher/k3s/")


def test_uninstall_keeps_the_namespace_and_by_default_the_data() -> None:
    objects = cr.render(Registry.in_cluster(on="node-a"))
    kept = {o["kind"] for o in cr.removable(objects, delete_storage=False)}
    assert kept == {"ConfigMap", "Service", "Deployment", "DaemonSet"}
    gone = {o["kind"] for o in cr.removable(objects, delete_storage=True)}
    assert "PersistentVolumeClaim" in gone and "Namespace" not in gone


def test_hosts_toml_points_the_stable_name_at_the_service() -> None:
    assert cr.hosts_toml("10.96.12.34", 5000) == (
        'server = "http://10.96.12.34:5000"\n'
        "\n"
        '[host."http://10.96.12.34:5000"]\n'
        '  capabilities = ["pull", "resolve"]\n'
    )
    assert 'server = "http://[fd00::10]:5000"' in cr.hosts_toml("fd00::10", 5000)


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_the_node_agent_writes_the_mirror_and_removes_it_on_stop(
    tmp_path: Path,
) -> None:
    registry = Registry.in_cluster(on="node-a")
    script = cr.agent_script(registry).replace(cr.HOST_MIRROR_DIR, str(tmp_path))
    written = tmp_path / HOST / "hosts.toml"
    missing = subprocess.run(
        ["sh", "-c", script], env={"PATH": os.environ["PATH"]}, timeout=10,
        capture_output=True, text=True,
    )  # fmt: skip
    assert missing.returncode == 1 and not written.exists()
    agent = subprocess.Popen(
        ["sh", "-c", script],
        env={"PATH": os.environ["PATH"], "PICELI_REGISTRY_SERVICE_HOST": "10.96.0.7"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not written.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert written.read_text() == cr.hosts_toml("10.96.0.7", 5000)
    finally:
        agent.send_signal(signal.SIGTERM)
        agent.wait(timeout=10)
    assert not written.exists() and not written.parent.exists()


# -------------------------------------------------------------- status


def _pod(name: str, node: str, component: str, ready: bool) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "labels": {"app.kubernetes.io/component": component},
        },
        "spec": {"nodeName": node},
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": str(ready)}],
        },
    }


def test_status_reports_the_mirror_of_every_node() -> None:
    registry = Registry.in_cluster(on="node-a")
    nodes = [{"metadata": {"name": n}} for n in ("cp", "node-a", "node-b")]
    pods = [
        _pod("reg", "node-a", "registry", True),
        _pod("m1", "cp", "mirror", True),
        _pod("m2", "node-a", "mirror", True),
        _pod("m3", "node-b", "mirror", False),
    ]
    body = cr.summarize(
        registry,
        deployment={"status": {"readyReplicas": 1}},
        service={"spec": {"clusterIP": "10.96.0.7", "ports": [{"port": 5000}]}},
        claim={"status": {"phase": "Bound", "capacity": {"storage": "20Gi"}}},
        nodes=nodes,
        pods=pods,
        used_bytes=1234,
    )
    assert body["state"] == "degraded"
    assert body["mirrors"] == [
        {"node": "cp", "mirror": "ready"},
        {"node": "node-a", "mirror": "ready"},
        {"node": "node-b", "mirror": "pending"},
    ]
    assert body["registry"]["pods"][0]["node"] == "node-a"
    assert body["service"] == {"cluster_ip": "10.96.0.7", "node_port": None}
    assert body["storage"]["used_bytes"] == 1234
    pods[-1] = _pod("m3", "node-b", "mirror", True)
    ready = cr.summarize(
        registry,
        deployment={"status": {"readyReplicas": 1}},
        service=None,
        claim=None,
        nodes=nodes,
        pods=pods,
    )
    assert ready["state"] == "ready"
    absent = cr.summarize(
        registry, deployment=None, service=None, claim=None, nodes=nodes, pods=[]
    )
    assert absent["state"] == "not-installed"


def test_used_bytes_reads_the_claim_from_kubelet_stats() -> None:
    summary = {
        "pods": [
            {
                "podRef": {"namespace": "piceli-system"},
                "volume": [
                    {"name": "config"},
                    {"pvcRef": {"name": "piceli-registry-storage"}, "usedBytes": 42},
                ],
            }
        ]
    }
    assert cr.used_bytes(summary, "piceli-system", "piceli-registry-storage") == 42
    assert cr.used_bytes(summary, "other", "piceli-registry-storage") is None


# ---------------------------------------------------------- placement


@pytest.mark.parametrize("in_cluster", [True, False])
def test_env_placement_is_free_with_the_in_cluster_registry(
    tmp_path: Path, in_cluster: bool
) -> None:
    """Every node pulls through the mirror: ``on_nodes`` may name any node.

    A node-loopback registry pins workloads to its node, so the same
    placement elsewhere is ``env-nodes-conflict``.
    """
    from piceli import EnvConfig, NodeLoopbackRegistry
    from piceli.envs import EnvError, env_pipeline
    from piceli.pipeline.compose import offline_composition

    cache = "docker.io/library/redis@" + DIGEST
    app = App("shop")
    app.deployment("cache", image=cache)
    deliver = (
        Registry.in_cluster(on="node-a", mirror=[cache])
        if in_cluster
        else NodeLoopbackRegistry(node="registry", mirror=[cache])
    )
    pipeline = Pipeline(
        app,
        Target(
            tmp_path / "kc",
            context="c",
            namespace="shop",
            nodes={"registry": "node-a"},
        ),
        deliver=deliver,
        state_dir=tmp_path / "state",
        envs=EnvConfig(prefix="shop-", branch_nodes=["node-b"]),
    )
    branch = env_pipeline(pipeline, "wp-1")
    if not in_cluster:
        with pytest.raises(EnvError) as error:
            offline_composition(branch)
        assert error.value.code == "env-nodes-conflict"
        return
    _, composition = offline_composition(branch)
    (manifest,) = [
        r.manifest
        for c in composition.components
        for r in c.resources
        if r.ref.kind == "Deployment" and r.ref.name == "cache"
    ]
    pod = manifest["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == (
        f"{HOST}/shop/mirror/docker.io/library/redis@{DIGEST}"
    )
    terms = pod["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"]
    assert terms[0]["matchExpressions"][-1]["values"] == ["node-b"]
