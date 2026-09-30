"""Branch isolation at render time (piceli.envs.isolation) and the namespace mapping."""

from __future__ import annotations

from typing import Any

import pytest

from piceli.envs import BranchEnv, EnvConfig, EnvError
from piceli.envs.isolation import COMPONENT, isolate
from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    ResourceIntent,
)
from piceli.pipeline.model import HANDLE_HOST

NS = "shop-wp-login"
REF = "127.0.0.1:5000/shop/api@sha256:" + "4" * 64


def _env(**config: Any) -> BranchEnv:
    return BranchEnv(
        branch="wp-login",
        namespace=NS,
        config=EnvConfig(prefix="shop-", **config),
        main_namespace="shop",
        images={"api": REF},
        pin_node="node-a",
    )


def _deployment(name: str = "api", **pod: Any) -> dict[str, Any]:
    container = {"name": name, "image": f"{HANDLE_HOST}/api:unresolved"}
    container.update(pod.pop("container", {}))
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": NS},
        "spec": {"template": {"spec": {"containers": [container], **pod}}},
    }


def _composition(*manifests: dict[str, Any]) -> DeploymentComposition:
    return DeploymentComposition(
        (
            DeploymentComponent(
                "app", tuple(ResourceIntent.from_manifest(m) for m in manifests)
            ),
        )
    )


def _objects(
    composition: DeploymentComposition,
) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (r.ref.kind, r.ref.name): r.manifest
        for c in composition.components
        for r in c.resources
    }


# ------------------------------------------------------------------ mapping


def test_namespace_for_maps_branches_and_main() -> None:
    config = EnvConfig(prefix="shop-", main_namespace="shop-prod")
    assert config.namespace_for("wp/Login_Page") == "shop-wp-login-page"
    assert config.namespace_for("main") == "shop-prod"
    long = config.namespace_for("feature/" + "x" * 80)
    assert len(long) == 63 and long.startswith("shop-feature-xxx")
    assert long != config.namespace_for("feature/" + "x" * 81)
    with pytest.raises(EnvError) as error:
        config.namespace_for("///")
    assert error.value.code == "env-branch-invalid"


def test_branch_globs_and_config_validation() -> None:
    config = EnvConfig(prefix="shop-", branches=["wp-*"])
    assert config.allows("wp-login") and config.allows("main")
    assert not config.allows("feature-x")
    for bad in (
        {"prefix": "Shop-"},
        {"prefix": "shop-", "max_envs": 0},
        {"prefix": "shop-", "claim_sizes": {"db": "lots"}},
        {"prefix": "shop-", "branches": "wp-*"},
    ):
        with pytest.raises(EnvError) as error:
            EnvConfig(**bad)
        assert error.value.code == "env-config-invalid"


# ---------------------------------------------------------------- isolation


def test_images_pins_isolation_objects_and_quota() -> None:
    result = isolate(_composition(_deployment()), _env(quota={"pods": "5"}))
    objects = _objects(result)
    pod = objects[("Deployment", "api")]["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == REF
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "node-a"}
    policy = objects[("NetworkPolicy", "piceli-env-isolation")]["spec"]
    assert policy["policyTypes"] == ["Ingress", "Egress"]
    assert policy["ingress"] == [{"from": [{"podSelector": {}}]}]
    assert {"protocol": "UDP", "port": 53} in policy["egress"][1]["ports"]
    quota = objects[("ResourceQuota", "piceli-env-isolation")]
    assert quota["spec"]["hard"] == {"pods": "5"}
    assert COMPONENT in {c.name for c in result.components}


def test_main_gets_images_only() -> None:
    env = BranchEnv(
        "main",
        "shop",
        EnvConfig(prefix="shop-"),
        "shop",
        isolated=False,
        images={"api": REF},
    )
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "api", "namespace": "shop"},
        "spec": {"type": "NodePort", "ports": [{"port": 80}]},
    }
    objects = _objects(isolate(_composition(_deployment(), service), env))
    assert set(objects) == {("Deployment", "api"), ("Service", "api")}


def test_missing_image_is_refused() -> None:
    env = BranchEnv("wp-x", NS, EnvConfig(prefix="shop-"), "shop")
    with pytest.raises(EnvError) as error:
        isolate(_composition(_deployment()), env)
    assert error.value.code == "env-image-missing"


@pytest.mark.parametrize(
    ("manifest", "code"),
    [
        (
            _deployment(
                container={
                    "env": [{"name": "DB", "value": "db.shop.svc.cluster.local"}]
                }
            ),
            "env-isolation-absolute-service",
        ),
        (
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "web", "namespace": NS},
                "spec": {"type": "NodePort", "ports": [{"port": 80}]},
            },
            "env-isolation-node-port",
        ),
        (
            _deployment(container={"ports": [{"containerPort": 80, "hostPort": 80}]}),
            "env-isolation-host-port",
        ),
        (_deployment(hostNetwork=True), "env-isolation-host-port"),
        (
            _deployment(volumes=[{"name": "d", "hostPath": {"path": "/srv"}}]),
            "env-isolation-host-path",
        ),
        (
            {
                "apiVersion": "apiextensions.k8s.io/v1",
                "kind": "CustomResourceDefinition",
                "metadata": {"name": "widgets.example.test"},
                "spec": {},
            },
            "env-isolation-cluster-scoped",
        ),
        (
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": "data", "namespace": NS},
                "spec": {"volumeName": "main-data"},
            },
            "env-isolation-shared-volume",
        ),
        (
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": "rb", "namespace": NS},
                "roleRef": {
                    "kind": "Role",
                    "name": "r",
                    "apiGroup": "rbac.authorization.k8s.io",
                },
                "subjects": [
                    {"kind": "ServiceAccount", "name": "x", "namespace": "shop"}
                ],
            },
            "env-isolation-cross-namespace",
        ),
        (
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "open", "namespace": NS},
                "spec": {
                    "podSelector": {},
                    "ingress": [{"from": [{"namespaceSelector": {}}]}],
                },
            },
            "env-isolation-cross-namespace",
        ),
    ],
)
def test_isolation_refusals(manifest: dict[str, Any], code: str) -> None:
    with pytest.raises(EnvError) as error:
        isolate(_composition(_deployment("ok"), manifest), _env())
    assert error.value.code == code


def test_relative_and_shared_names_are_allowed() -> None:
    manifest = _deployment(
        container={
            "env": [
                {"name": "A", "value": "db"},
                {"name": "B", "value": f"db.{NS}.svc.cluster.local"},
                {"name": "C", "value": "kubernetes.default.svc"},
            ]
        }
    )
    isolate(_composition(manifest), _env())


def test_cluster_scoped_objects_get_namespace_qualified_names() -> None:
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": {"name": "viewer"},
        "rules": [],
    }
    binding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": "viewer"},
        "roleRef": {
            "kind": "ClusterRole",
            "name": "viewer",
            "apiGroup": "rbac.authorization.k8s.io",
        },
        "subjects": [{"kind": "ServiceAccount", "name": "api", "namespace": NS}],
    }
    storage = {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "StorageClass",
        "metadata": {"name": "fast"},
        "provisioner": "example.test/fast",
    }
    claim = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": "data", "namespace": NS},
        "spec": {
            "storageClassName": "fast",
            "resources": {"requests": {"storage": "10Gi"}},
        },
    }
    objects = _objects(isolate(_composition(role, binding, storage, claim), _env()))
    assert ("ClusterRole", f"viewer-{NS}") in objects
    assert (
        objects[("ClusterRoleBinding", f"viewer-{NS}")]["roleRef"]["name"]
        == f"viewer-{NS}"
    )
    assert ("StorageClass", f"fast-{NS}") in objects
    assert (
        objects[("PersistentVolumeClaim", "data")]["spec"]["storageClassName"]
        == f"fast-{NS}"
    )


def test_claim_sizes_apply_to_templates_and_claims() -> None:
    stateful = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": "db", "namespace": NS},
        "spec": {
            "template": {"spec": {"containers": [{"name": "db", "image": REF}]}},
            "volumeClaimTemplates": [
                {
                    "metadata": {"name": "data"},
                    "spec": {"resources": {"requests": {"storage": "50Gi"}}},
                }
            ],
        },
    }
    claim = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": "cache-state", "namespace": NS},
        "spec": {"resources": {"requests": {"storage": "20Gi"}}},
    }
    cache = _deployment(
        "cache",
        volumes=[{"name": "s", "persistentVolumeClaim": {"claimName": "cache-state"}}],
    )
    env = _env(claim_sizes={"db/data": "1Gi", "cache": "2Gi"})
    objects = _objects(isolate(_composition(stateful, claim, cache), env))
    template = objects[("StatefulSet", "db")]["spec"]["volumeClaimTemplates"][0]
    assert template["spec"]["resources"]["requests"]["storage"] == "1Gi"
    assert objects[("PersistentVolumeClaim", "cache-state")]["spec"]["resources"] == {
        "requests": {"storage": "2Gi"}
    }
    with pytest.raises(EnvError) as error:
        isolate(_composition(stateful), _env(claim_sizes={"nothing": "1Gi"}))
    assert error.value.code == "env-config-invalid"
