"""Typed service accounts with RBAC rules, and label-selected network policies."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from piceli import App, PodDefaults, Rule
from piceli.k8s.ops.discovery import RELEASE_NAMESPACE_ANNOTATION, ResourceIdentity

RBAC = "rbac.authorization.k8s.io/v1"
IMAGE = "registry.example/watcher:1"


def _objects(composition) -> dict[tuple[str, str], dict]:
    return {
        (resource.ref.kind, resource.ref.name): resource.manifest
        for component in composition.components
        for resource in component.resources
    }


def _watcher_app(**app) -> tuple[App, object]:
    shop = App("shop", **app)
    watcher = shop.service_account(
        "watcher",
        rules=[Rule(resources=["pods", "pods/log"], verbs=["get", "list", "watch"])],
        cluster_rules=[Rule(resources=["nodes"], verbs=["get", "list"])],
    )
    shop.deployment("watcher", image=IMAGE, service_account=watcher)
    return shop, watcher


def test_service_account_renders_account_role_and_cluster_role():
    shop, _ = _watcher_app()
    composition = shop.render("staging")
    objects = _objects(composition)
    cluster_name = "staging:shop:watcher"
    assert set(objects) == {
        ("ServiceAccount", "watcher"),
        ("Role", "watcher"),
        ("RoleBinding", "watcher"),
        ("ClusterRole", cluster_name),
        ("ClusterRoleBinding", cluster_name),
        ("Deployment", "watcher"),
    }
    account = objects[("ServiceAccount", "watcher")]
    assert account["automountServiceAccountToken"] is False
    assert account["metadata"]["namespace"] == "staging"
    role = objects[("Role", "watcher")]
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["pods", "pods/log"],
            "verbs": ["get", "list", "watch"],
        }
    ]
    subject = {"kind": "ServiceAccount", "name": "watcher", "namespace": "staging"}
    assert objects[("RoleBinding", "watcher")]["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": "watcher",
    }
    assert objects[("RoleBinding", "watcher")]["subjects"] == [subject]
    cluster_role = objects[("ClusterRole", cluster_name)]
    assert "namespace" not in cluster_role["metadata"]
    assert cluster_role["metadata"]["annotations"] == {
        RELEASE_NAMESPACE_ANNOTATION: "staging"
    }
    binding = objects[("ClusterRoleBinding", cluster_name)]
    assert binding["roleRef"]["kind"] == "ClusterRole"
    assert binding["roleRef"]["name"] == cluster_name
    assert binding["subjects"] == [subject]
    # Cluster objects have no namespace in their identity; the name is a valid
    # RBAC path segment even with ':'.
    refs = {
        resource.ref
        for component in composition.components
        for resource in component.resources
        if resource.ref.kind.startswith("Cluster")
    }
    assert {ref.namespace for ref in refs} == {""}
    for ref in refs:
        ResourceIdentity(ref.api_version, ref.kind, "", ref.name)
    pod = objects[("Deployment", "watcher")]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "watcher"
    assert pod["automountServiceAccountToken"] is True


def test_cluster_names_differ_per_namespace():
    shop, _ = _watcher_app()
    staging = {n for k, n in _objects(shop.render("staging")) if k == "ClusterRole"}
    prod = {n for k, n in _objects(shop.render("prod")) if k == "ClusterRole"}
    assert staging.isdisjoint(prod)


def test_workload_depends_on_its_service_account_component():
    shop = App("shop")
    reader = shop.service_account(
        "reader", rules=[Rule(resources=["pods"], verbs=["get"])]
    )
    shop.deployment("api", image=IMAGE, service_account=reader)
    components = {c.name: c.dependencies for c in shop.render("shop").components}
    assert components == {"api": ("reader",), "reader": ()}


def test_account_without_rules_renders_only_the_account():
    shop = App("shop")
    shop.service_account("plain")
    assert set(_objects(shop.render("shop"))) == {("ServiceAccount", "plain")}


def test_token_automounting():
    shop = App("shop", pod_defaults=PodDefaults(automount_token=False))
    bound = shop.service_account("bound")
    shop.deployment("a", image=IMAGE, service_account=bound)
    shop.deployment("b", image=IMAGE)
    shop.deployment("c", image=IMAGE, service_account="external")
    shop.deployment("d", image=IMAGE, service_account=bound, automount_token=False)
    objects = _objects(shop.render("shop"))

    def automount(name: str):
        pod = objects[("Deployment", name)]["spec"]["template"]["spec"]
        return pod.get("automountServiceAccountToken")

    assert [automount(name) for name in "abcd"] == [True, False, False, False]
    # Without pod defaults, unbound pods keep the Kubernetes default.
    plain = App("shop")
    plain.deployment("b", image=IMAGE)
    pod = _objects(plain.render("shop"))[("Deployment", "b")]["spec"]["template"]
    assert "automountServiceAccountToken" not in pod["spec"]


def test_service_account_must_be_declared_on_this_app():
    other = App("other").service_account("watcher")
    with pytest.raises(ValueError, match="not declared on this app"):
        App("shop").deployment("api", image=IMAGE, service_account=other)
    shop = App("shop")
    shop.service_account("watcher")
    with pytest.raises(ValueError, match="already declared"):
        shop.service_account("watcher")


def test_override_patches_the_service_account():
    shop, watcher = _watcher_app()
    shop.override(watcher, {"metadata": {"annotations": {"team": "ops"}}})
    objects = _objects(shop.render("shop"))
    assert objects[("ServiceAccount", "watcher")]["metadata"]["annotations"] == {
        "team": "ops"
    }
    assert "annotations" not in objects[("Role", "watcher")]["metadata"]


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"resources": ["pods"], "verbs": []}, "verbs"),
        ({"resources": [], "verbs": ["get"]}, "resources"),
        ({"resources": ["pods"], "verbs": ["get"], "api_groups": []}, "api_groups"),
        ({"resources": ["pods"], "verbs": ["*"]}, "allow_wildcard"),
        ({"resources": ["*"], "verbs": ["get"]}, "allow_wildcard"),
        ({"resources": ["pods"], "verbs": ["get"], "api_groups": ["*"]}, "wildcard"),
        ({"resources": ["pods"], "verbs": ["get list"]}, "verb"),
        ({"resources": ["Pods"], "verbs": ["get"]}, "resource"),
        ({"resources": ["pods"], "verbs": ["get"], "api_groups": ["Apps"]}, "group"),
        (
            {"resources": ["pods"], "verbs": ["create"], "resource_names": ["a"]},
            "create",
        ),
        ({"resources": ["pods"], "verbs": ["get"], "resource_names": ["a/b"]}, "name"),
    ],
)
def test_rule_validation(fields, message):
    with pytest.raises(ValidationError, match=message):
        Rule(**fields)


def test_rule_wildcard_with_explicit_allow():
    rule = Rule(resources=["*"], verbs=["*"], api_groups=["*"], allow_wildcard=True)
    assert rule.manifest() == {"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}
    named = Rule(
        api_groups=["apps"],
        resources=["deployments"],
        verbs=["get"],
        resource_names=["api"],
    )
    assert named.manifest()["resourceNames"] == ["api"]


# ------------------------------------------------------------ network policies


def test_release_wide_network_policy():
    shop = App("shop")
    api = shop.deployment("api", image=IMAGE, ports=[8080])
    shop.deployment("web", image=IMAGE)
    shop.network_policy(
        selector=shop.release_selector,
        allow_from_selector=shop.release_selector,
        name="shop-internal",
    )
    shop.network_policy(
        api, allow_from_selector=[{"role": "gateway"}], allow_from=[], ports=[8080]
    )
    objects = _objects(shop.render("shop"))
    internal = objects[("NetworkPolicy", "shop-internal")]["spec"]
    assert internal == {
        "podSelector": {"matchLabels": {"app.kubernetes.io/part-of": "shop"}},
        "policyTypes": ["Ingress"],
        "ingress": [
            {
                "from": [
                    {
                        "podSelector": {
                            "matchLabels": {"app.kubernetes.io/part-of": "shop"}
                        }
                    }
                ]
            }
        ],
    }
    # Every pod of the app carries the release selector labels.
    for name in ("api", "web"):
        labels = objects[("Deployment", name)]["spec"]["template"]["metadata"]["labels"]
        assert shop.release_selector.items() <= labels.items()
    ingress = objects[("NetworkPolicy", "api-ingress")]["spec"]["ingress"]
    assert ingress == [
        {
            "from": [{"podSelector": {"matchLabels": {"role": "gateway"}}}],
            "ports": [{"port": 8080, "protocol": "TCP"}],
        }
    ]
    components = {c.name for c in shop.render("shop").components}
    assert "shop-internal" in components


def test_network_policy_argument_errors():
    shop = App("shop")
    api = shop.deployment("api", image=IMAGE)
    with pytest.raises(ValueError, match="either a workload or selector"):
        shop.network_policy(api, selector={"a": "b"})
    with pytest.raises(ValueError, match="either a workload or selector"):
        shop.network_policy()
    with pytest.raises(ValueError, match="needs name"):
        shop.network_policy(selector={"a": "b"})
    with pytest.raises(ValueError, match="every pod"):
        shop.network_policy(api, allow_from_selector={})
    with pytest.raises(ValueError, match="no labels"):
        _ = App("bare", labels={}).release_selector
