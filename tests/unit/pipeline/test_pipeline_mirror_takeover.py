"""Unit tests of ``mirror=`` and registry takeover declarations (no cluster)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from piceli import App
from piceli.k8s.templates.deployable.node_local_registry import NodeLocalRegistry
from piceli.pipeline import (
    NodeLoopbackRegistry,
    Pipeline,
    PipelineError,
    Registry,
    Target,
)
from piceli.pipeline.compose import (
    mirror_references,
    pinned_images,
    registry_release_spec,
    resolve,
)
from piceli.pipeline.registry_takeover import decide, live_registry

DIGEST = "sha256:" + "d" * 64
REDIS = f"docker.io/library/redis:7.2@{DIGEST}"
NODE = "shop-node"


def _target() -> Target:
    return Target.kubeconfig(
        "cluster.kubeconfig",
        context="kind-shop",
        namespace="shop",
        nodes={"primary": NODE},
    )


def _pipeline(deliver: Any) -> Pipeline:
    app = App("shop")
    app.deployment("cache", image=REDIS)
    app.deployment("web", image=f"example.invalid/web@{DIGEST}")
    return Pipeline(app, _target(), deliver=deliver)


# ---------------------------------------------------------------- declarations


def test_mirror_entries_are_canonical_and_pinned() -> None:
    strategy = NodeLoopbackRegistry(mirror=[f"redis@{DIGEST}", REDIS])
    assert strategy.mirror == (f"docker.io/library/redis@{DIGEST}",)
    with pytest.raises(PipelineError) as raised:
        NodeLoopbackRegistry(mirror=["redis:7.2"])
    assert raised.value.code == "pipeline-mirror-not-pinned"
    with pytest.raises(PipelineError) as raised:
        Registry("oci://registry.example/shop", mirror=["redis:7.2"])
    assert raised.value.code == "pipeline-mirror-not-pinned"
    with pytest.raises(PipelineError, match="list"):
        NodeLoopbackRegistry(mirror=REDIS)  # type: ignore[arg-type]


def test_mirror_credentials_are_files_by_registry_never_described() -> None:
    strategy = NodeLoopbackRegistry(
        mirror=[REDIS], mirror_credentials={"docker.io": "hub.json"}
    )
    assert strategy.mirror_credentials == {
        "docker.io": Path(__file__).parent / "hub.json"
    }
    described = strategy.describe()
    assert described["mirror_credentials"] == ["docker.io"]
    assert "hub.json" not in repr(strategy) + str(described)
    with pytest.raises(PipelineError):
        NodeLoopbackRegistry(mirror_credentials={"bad host/": "x.json"})


def test_describe_is_unchanged_without_the_new_options() -> None:
    # Earlier plans keep their combined hashes.
    assert NodeLoopbackRegistry().describe() == {
        "strategy": "node-loopback-registry",
        "port": 5000,
        "node": None,
        "name": "registry",
        "storage": "10Gi",
        "image": None,
        "repository": None,
    }
    assert Registry("oci://registry.example/shop").describe() == {
        "strategy": "registry",
        "url": "oci://registry.example/shop",
        "node_registry": None,
    }


def test_adopt_and_replace_name_the_registry_and_exclude_each_other() -> None:
    assert NodeLoopbackRegistry(adopt="old").registry_name == "old"
    assert NodeLoopbackRegistry(replace="old", name="old").registry_name == "old"
    for kwargs in (
        {"adopt": "a", "replace": "a"},
        {"adopt": "a", "name": "b"},
        {"adopt": "Not_Valid"},
        {"host_path": "relative"},
        {"host_path": "/data", "existing_claim": "claim"},
        {"inherited_owners": "ops"},
    ):
        with pytest.raises(PipelineError) as raised:
            NodeLoopbackRegistry(**kwargs)  # type: ignore[arg-type]
        assert raised.value.code == "pipeline-invalid"


# ------------------------------------------------------------------- rewrite


def test_mirrored_images_are_rewritten_with_the_same_digest_and_pinned() -> None:
    pipeline = _pipeline(NodeLoopbackRegistry(port=5001, mirror=[f"redis@{DIGEST}"]))
    copy = f"127.0.0.1:5001/mirror/docker.io/library/redis@{DIGEST}"
    assert mirror_references(pipeline) == {f"docker.io/library/redis@{DIGEST}": copy}
    images = pinned_images(pipeline, {})
    assert images["cache"].reference == copy
    assert images["cache"].identity == DIGEST  # the release records the digest
    assert images["web"].reference == f"example.invalid/web@{DIGEST}"
    from piceli.pipeline.compose import preview_context, render_app

    composition = resolve(
        render_app(pipeline, preview_context(pipeline)),
        images={},
        secrets={},
        pin_node=NODE,
        mirrors=mirror_references(pipeline),
    )
    pods = {
        resource.ref.name: resource.manifest["spec"]["template"]["spec"]
        for component in composition.components
        for resource in component.resources
        if resource.ref.kind == "Deployment"
    }
    assert pods["cache"]["containers"][0]["image"] == copy
    assert pods["cache"]["nodeSelector"] == {"kubernetes.io/hostname": NODE}
    assert "nodeSelector" not in pods["web"]  # not mirrored, not pinned


def test_registry_strategy_mirrors_under_its_prefix() -> None:
    pipeline = _pipeline(
        Registry(
            "oci://registry.example:5000/team",
            node_registry="10.0.0.5:5000",
            mirror=[REDIS],
        )
    )
    assert mirror_references(pipeline) == {
        f"docker.io/library/redis@{DIGEST}": (
            f"10.0.0.5:5000/team/mirror/docker.io/library/redis@{DIGEST}"
        )
    }


def test_registry_spec_carries_platforms_and_takeover() -> None:
    from piceli.k8s.release_spec import NodeRef, ReleaseContext
    from piceli.pipeline.compose import RegistryTakeover

    pipeline = _pipeline(
        NodeLoopbackRegistry(adopt="old", host_path="/srv/registry", mirror=[REDIS])
    )
    spec = registry_release_spec(
        pipeline,
        platforms=("linux/arm64",),
        takeover=RegistryTakeover(adopt=("Deployment/old",), selector={"app": "old"}),
    )
    assert spec.model.release.adopt == ("Deployment/old",)
    ctx = ReleaseContext(
        namespace="shop",
        images=spec.images(),
        secrets={},
        nodes={"primary": NodeRef(NODE, "uid")},
    )
    (component,) = spec.load_composition()(ctx).components
    manifests = {r.ref.kind: r.manifest for r in component.resources}
    assert "PersistentVolumeClaim" not in manifests
    assert manifests["Deployment"]["spec"]["selector"] == {
        "matchLabels": {"app": "old"}
    }
    labels = manifests["Deployment"]["spec"]["template"]["metadata"]["labels"]
    assert labels["app"] == "old"
    config = yaml.safe_load(manifests["ConfigMap"]["data"]["config.yml"])
    assert config["validation"]["manifests"]["indexes"]["platformlist"] == [
        {"os": "linux", "architecture": "arm64"}
    ]


def test_template_storage_and_validation_options() -> None:
    registry = NodeLocalRegistry(node_name=NODE, existing_claim="data")
    assert registry.get_claim() is None
    assert registry.claim_name == "data"
    assert "validation" not in registry.registry_config()
    assert registry.get_deployment().spec.strategy.type == "Recreate"
    rolling = NodeLocalRegistry(node_name=NODE, rolling_update=True).get_deployment()
    assert rolling.spec.strategy.rolling_update.max_surge == 0
    with pytest.raises(ValueError):
        NodeLocalRegistry(node_name=NODE, existing_claim="data", host_path="/x")
    with pytest.raises(ValueError):
        NodeLocalRegistry(node_name=NODE, index_platforms=["arm64"])


# ------------------------------------------------------------------ takeover


def _deployment(
    name: str,
    *,
    port: int = 5000,
    host_network: bool = True,
    node: str | None = NODE,
    volume: dict[str, Any] | None = None,
    owner: str | None = None,
    replicas: int = 1,
    selector: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pod: dict[str, Any] = {
        "hostNetwork": host_network,
        "containers": [
            {
                "name": "registry",
                "ports": [{"containerPort": port}],
                "volumeMounts": [{"name": "data", "mountPath": "/var/lib/registry"}],
            }
        ],
        "volumes": [{"name": "data", **(volume or {"hostPath": {"path": "/srv/r"}})}],
    }
    if node:
        pod["nodeSelector"] = {"kubernetes.io/hostname": node}
    return {
        "metadata": {
            "name": name,
            "annotations": {"piceli.io/owner": owner} if owner else {},
        },
        "spec": {
            "replicas": replicas,
            "selector": selector or {"matchLabels": {"app": name}},
            "template": {"spec": pod},
        },
    }


def _decide(strategy: NodeLoopbackRegistry, *deployments: dict[str, Any]) -> Any:
    return decide(strategy, node=NODE, owner="shop-registry", deployments=deployments)


def test_nothing_live_creates_and_standing_adopt_is_harmless() -> None:
    assert _decide(NodeLoopbackRegistry()).existing is None
    decision = _decide(NodeLoopbackRegistry(adopt="old"))
    assert decision.existing is None
    assert decision.takeover.adopt == (
        "Deployment/old",
        "ConfigMap/old-config",
        "PersistentVolumeClaim/old-storage",
    )


def test_port_holders_on_the_node_are_detected() -> None:
    with pytest.raises(PipelineError) as raised:
        _decide(NodeLoopbackRegistry(), _deployment("old"))
    assert raised.value.code == "pipeline-registry-takeover-required"
    assert "adopt='old'" in str(raised.value)
    # Other node, other port, scaled to zero, or not on the host network: free.
    _decide(NodeLoopbackRegistry(), _deployment("a", node="other-node"))
    _decide(NodeLoopbackRegistry(), _deployment("b", port=5001))
    _decide(NodeLoopbackRegistry(), _deployment("c", replicas=0))
    _decide(NodeLoopbackRegistry(), _deployment("d", host_network=False))
    hostport = _deployment("e", host_network=False)
    hostport["spec"]["template"]["spec"]["containers"][0]["ports"] = [
        {"containerPort": 80, "hostPort": 5000}
    ]
    with pytest.raises(PipelineError):
        _decide(NodeLoopbackRegistry(), hostport)


def test_unmanaged_registry_with_the_same_name_needs_a_flag() -> None:
    with pytest.raises(PipelineError) as raised:
        _decide(NodeLoopbackRegistry(), _deployment("registry"))
    assert raised.value.code == "pipeline-registry-takeover-required"


def test_managed_registry_keeps_its_selector() -> None:
    live = _deployment(
        "registry",
        owner="shop-registry",
        volume={"persistentVolumeClaim": {"claimName": "registry-storage"}},
    )
    live["spec"]["strategy"] = {"type": "Recreate"}
    decision = _decide(NodeLoopbackRegistry(), live)
    assert decision.existing["action"] == "managed"
    assert decision.takeover.selector == {"app": "registry"}
    assert decision.takeover.rolling_update is False
    assert decision.takeover.adopt == decision.takeover.replace == ()


def test_adopt_checks_compatibility() -> None:
    ok = _decide(
        NodeLoopbackRegistry(adopt="old", host_path="/srv/r"), _deployment("old")
    )
    assert ok.existing["action"] == "adopt" and ok.existing["data"] == "kept"
    assert ok.takeover.adopt == ("Deployment/old", "ConfigMap/old-config")
    assert ok.takeover.rolling_update is True  # the live default strategy
    claim = _deployment("old", volume={"persistentVolumeClaim": {"claimName": "data"}})
    assert _decide(
        NodeLoopbackRegistry(adopt="old", existing_claim="data"), claim
    ).existing["storage"] == {"claim": "data"}
    own_claim = _deployment(
        "old", volume={"persistentVolumeClaim": {"claimName": "old-storage"}}
    )
    assert (
        "PersistentVolumeClaim/old-storage"
        in _decide(NodeLoopbackRegistry(adopt="old"), own_claim).takeover.adopt
    )
    for strategy, live, words in (
        (NodeLoopbackRegistry(adopt="old"), _deployment("old"), "/srv/r"),
        (
            NodeLoopbackRegistry(adopt="old", host_path="/srv/r"),
            _deployment("old", port=5001),
            "5001",
        ),
        (
            NodeLoopbackRegistry(adopt="old", host_path="/srv/r"),
            _deployment("old", host_network=False),
            "host network",
        ),
        (
            NodeLoopbackRegistry(adopt="old", host_path="/srv/r"),
            _deployment("old", node="other-node"),
            "other-node",
        ),
        (
            NodeLoopbackRegistry(adopt="old", host_path="/srv/r"),
            _deployment(
                "old",
                selector={"matchExpressions": [{"key": "app", "operator": "Exists"}]},
            ),
            "match expressions",
        ),
    ):
        with pytest.raises(PipelineError) as raised:
            _decide(strategy, live)
        assert raised.value.code == "pipeline-registry-incompatible"
        assert words in str(raised.value)


def test_replace_allows_differences_and_says_whether_data_carries_over() -> None:
    kept = _decide(
        NodeLoopbackRegistry(replace="old", host_path="/srv/r"),
        _deployment("old", port=5001),
    )
    assert kept.takeover.replace == ("Deployment/old",)
    assert kept.takeover.adopt == ("ConfigMap/old-config",)
    assert kept.takeover.selector is None  # the new Deployment gets the template's
    assert kept.existing["data"] == "kept"
    lost = _decide(NodeLoopbackRegistry(replace="old"), _deployment("old"))
    assert lost.existing["data"] == "not-carried-over"
    assert "PersistentVolumeClaim/old-storage" in lost.takeover.adopt


def test_live_registry_reads_env_port_and_storage_root() -> None:
    live = _deployment("old", port=1)
    container = live["spec"]["template"]["spec"]["containers"][0]
    container["ports"] = []
    container["env"] = [
        {"name": "REGISTRY_HTTP_ADDR", "value": "127.0.0.1:5000"},
        {"name": "REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY", "value": "/data"},
    ]
    container["volumeMounts"] = [{"name": "data", "mountPath": "/data"}]
    summary = live_registry(live, ["x"])
    assert summary.ports == (5000,)
    assert summary.storage == {"host_path": "/srv/r"}
    assert not summary.managed
