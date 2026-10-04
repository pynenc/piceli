"""The foreign-cluster safety gate: every rule, its opt-out, and the App API."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from piceli import App, Resources, Rule, Security
from piceli.bundle.build import BundleError, build_bundle, default_deny
from piceli.bundle.rules import CODES, RULES
from piceli.bundle.safety import check, exceptions, first_code
from piceli.errors import ERRORS
from tests.unit.bundle.fixtures import app, components

SAFE_CONTEXT = {
    "runAsNonRoot": True,
    "runAsUser": 10001,
    "readOnlyRootFilesystem": True,
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
}
LIMITS = {
    "requests": {"cpu": "10m", "memory": "16Mi"},
    "limits": {"cpu": "100m", "memory": "32Mi"},
}


def deployment(**container: Any) -> dict[str, Any]:
    body = {
        "name": "web",
        "image": "x@sha256:" + "a" * 64,
        "securityContext": dict(SAFE_CONTEXT),
        "resources": copy.deepcopy(LIMITS),
        **container,
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "web"},
        "spec": {"template": {"metadata": {}, "spec": {"containers": [body]}}},
    }


def rules(objects: list[dict[str, Any]]) -> list[str]:
    return [item.rule for item in check(objects)]


def test_every_rule_has_a_registered_code() -> None:
    for rule in RULES:
        assert CODES[rule] in ERRORS
        assert ERRORS[CODES[rule]].area == "bundle"


def test_a_safe_deployment_passes() -> None:
    assert check([deployment(), default_deny("shop")]) == []


@pytest.mark.parametrize(
    ("context", "rule"),
    [
        ({"runAsNonRoot": None, "runAsUser": None}, "non-root"),
        ({"runAsUser": 0, "runAsNonRoot": None}, "non-root"),
        ({"readOnlyRootFilesystem": False}, "read-only-root"),
        ({"readOnlyRootFilesystem": None}, "read-only-root"),
        ({"privileged": True}, "no-privilege"),
        ({"allowPrivilegeEscalation": None}, "no-privilege"),
        ({"capabilities": {"add": ["SYS_ADMIN"]}}, "no-privilege"),
    ],
    ids=[
        "no-user",
        "uid-0",
        "writable",
        "unset-readonly",
        "privileged",
        "escalation",
        "caps",
    ],
)
def test_container_security_rules(context: dict[str, Any], rule: str) -> None:
    settings = {
        key: value
        for key, value in {**SAFE_CONTEXT, **context}.items()
        if value is not None
    }
    assert rules([deployment(securityContext=settings)]) == [rule]


def test_pod_level_non_root_counts_for_every_container() -> None:
    manifest = deployment(
        securityContext={
            k: v
            for k, v in SAFE_CONTEXT.items()
            if k not in {"runAsNonRoot", "runAsUser"}
        }
    )
    manifest["spec"]["template"]["spec"]["securityContext"] = {"runAsNonRoot": True}
    assert rules([manifest]) == []


def test_init_containers_are_checked_too() -> None:
    manifest = deployment()
    manifest["spec"]["template"]["spec"]["initContainers"] = [
        {
            "name": "init",
            "image": "x@sha256:" + "b" * 64,
            "securityContext": dict(SAFE_CONTEXT),
        }
    ]
    found = check([manifest])
    assert [(item.rule, "'init'" in item.detail) for item in found] == [
        ("resources", True)
    ]


@pytest.mark.parametrize(
    "resources",
    [{}, {"requests": LIMITS["requests"]}, {"limits": LIMITS["limits"]},
     {"requests": {"cpu": "1"}, "limits": LIMITS["limits"]}],
    ids=["none", "no-limits", "no-requests", "no-memory-request"],
)  # fmt: skip
def test_requests_and_limits_for_cpu_and_memory(resources: dict[str, Any]) -> None:
    assert rules([deployment(resources=resources)]) == ["resources"]


def test_host_access_and_node_ports() -> None:
    manifest = deployment(ports=[{"containerPort": 80, "hostPort": 80}])
    pod = manifest["spec"]["template"]["spec"]
    pod["hostNetwork"] = True
    pod["volumes"] = [{"name": "root", "hostPath": {"path": "/"}}]
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "web"},
        "spec": {"type": "NodePort", "ports": [{"port": 80}]},
    }
    balancer = copy.deepcopy(service)
    balancer["spec"]["type"] = "LoadBalancer"
    assert rules([manifest, service, balancer]) == [
        "no-privilege", "no-privilege", "node-port", "node-port", "node-port",
    ]  # fmt: skip


def policy(ingress: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "open"},
        "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": ingress},
    }


@pytest.mark.parametrize(
    "ingress",
    [[{}], [{"from": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}],
     [{"from": [{"namespaceSelector": {}}]}], [{"ports": [{"port": 80}]}]],
    ids=["any", "cidr", "namespaces", "ports-only"],
)  # fmt: skip
def test_network_ingress_from_outside_the_namespace(
    ingress: list[dict[str, Any]],
) -> None:
    assert rules([policy(ingress)]) == ["network"]


def test_network_ingress_from_the_namespace_and_egress_pass() -> None:
    inside = policy([{"from": [{"podSelector": {"matchLabels": {"a": "b"}}}]}])
    egress = policy([])
    egress["spec"] = {
        "podSelector": {},
        "policyTypes": ["Egress"],
        "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}],
    }
    assert rules([inside, egress, default_deny("shop")]) == []


def role(kind: str, rules_: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": kind,
        "metadata": {"name": "r"},
        "rules": rules_,
    }


def binding(
    kind: str, role_kind: str, name: str, subject: str = "ServiceAccount"
) -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": kind,
        "metadata": {"name": "b"},
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": role_kind,
            "name": name,
        },
        "subjects": [{"kind": subject, "name": "sa"}],
    }


@pytest.mark.parametrize(
    ("kind", "rule"),
    [
        ("Role", {"apiGroups": [""], "resources": ["*"], "verbs": ["get"]}),
        ("Role", {"apiGroups": [""], "resources": ["pods"], "verbs": ["*"]}),
        ("Role", {"apiGroups": [""], "resources": ["roles"], "verbs": ["escalate"]}),
        ("Role", {"apiGroups": [""], "resources": ["users"], "verbs": ["impersonate"]}),
        ("ClusterRole", {"apiGroups": [""], "resources": ["pods"], "verbs": ["delete"]}),
        ("ClusterRole", {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]}),
        ("ClusterRole", {"nonResourceURLs": ["/metrics"], "verbs": ["get"]}),
    ],
    ids=["wildcard-resource", "wildcard-verb", "escalate", "impersonate",
         "cluster-write", "cluster-secrets", "non-resource"],
)  # fmt: skip
def test_rbac_least_privilege(kind: str, rule: dict[str, Any]) -> None:
    assert "rbac" in rules([role(kind, [rule])])


def test_rbac_read_only_cluster_role_and_own_bindings_pass() -> None:
    reader = role(
        "ClusterRole",
        [
            {
                "apiGroups": [""],
                "resources": ["nodes"],
                "verbs": ["get", "list", "watch"],
            }
        ],
    )
    writer = role(
        "Role",
        [{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "update"]}],
    )
    assert (
        rules(
            [
                reader,
                writer,
                binding("ClusterRoleBinding", "ClusterRole", "r"),
                binding("RoleBinding", "Role", "r"),
            ]
        )
        == []
    )


def test_rbac_bindings_to_foreign_roles_or_users_are_refused() -> None:
    admin = binding("ClusterRoleBinding", "ClusterRole", "cluster-admin")
    user = binding("RoleBinding", "Role", "r", subject="User")
    assert rules([role("Role", []), admin, user]) == ["rbac", "rbac"]


def test_an_annotation_waives_one_rule_of_one_object() -> None:
    manifest = deployment(
        securityContext={**SAFE_CONTEXT, "readOnlyRootFilesystem": False}
    )
    manifest["metadata"]["annotations"] = {
        "safety.piceli.io/allow-read-only-root": "writes its cache at start"
    }
    other = deployment(
        securityContext={**SAFE_CONTEXT, "readOnlyRootFilesystem": False}
    )
    other["metadata"]["name"] = "api"
    found = check([manifest, other])
    assert [(item.object, item.rule) for item in found] == [
        ("Deployment/api", "read-only-root")
    ]
    assert [item.public() for item in exceptions([manifest, other])] == [
        {
            "object": "Deployment/web",
            "rule": "read-only-root",
            "reason": "writes its cache at start",
        }
    ]


def test_the_refusal_code_is_the_first_rule_in_rule_order() -> None:
    found = check(
        [deployment(resources={}, securityContext={**SAFE_CONTEXT, "runAsUser": 0})]
    )
    assert first_code(found) == "bundle-unsafe-non-root"


# ------------------------------------------------------------ the App API


def test_safety_exception_annotates_every_object_it_renders() -> None:
    shop = App("shop")
    account = shop.service_account(
        "watcher", cluster_rules=[Rule(resources=["pods"], verbs=["get", "delete"])]
    )
    shop.safety_exception(account, "rbac", reason="deletes stuck pods")
    composition = shop.render("shop")
    annotated = {
        resource.ref.kind: (resource.manifest["metadata"].get("annotations") or {}).get(
            "safety.piceli.io/allow-rbac"
        )
        for component in composition.components
        for resource in component.resources
    }
    assert annotated == dict.fromkeys(
        ["ServiceAccount", "ClusterRole", "ClusterRoleBinding"], "deletes stuck pods"
    )
    # It survives an environment.
    shop.environment("client", namespace="shop-client")
    derived = shop.for_environment("client").render("shop-client")
    kinds = {
        resource.ref.kind
        for component in derived.components
        for resource in component.resources
        if "safety.piceli.io/allow-rbac"
        in (resource.manifest["metadata"].get("annotations") or {})
    }
    assert kinds == {"ServiceAccount", "ClusterRole", "ClusterRoleBinding"}


@pytest.mark.parametrize(
    ("rule", "reason", "message"),
    [("no-such-rule", "why", "unknown safety rule"), ("rbac", "", "reason"),
     ("rbac", "two\nlines", "reason"), ("rbac", "x" * 201, "reason")],
    ids=["rule", "empty", "multiline", "long"],
)  # fmt: skip
def test_safety_exception_is_validated(rule: str, reason: str, message: str) -> None:
    shop = App("shop")
    account = shop.service_account("watcher")
    with pytest.raises(ValueError, match=message):
        shop.safety_exception(account, rule, reason=reason)


def test_safety_exception_needs_a_declared_object() -> None:
    other = App("other").service_account("watcher")
    with pytest.raises(ValueError, match="not declared"):
        App("shop").safety_exception(other, "rbac", reason="why")


def test_the_gate_refuses_a_bundle_and_lists_every_violation() -> None:
    shop, generators = app()
    shop.deployment(
        "root",
        image="x@sha256:" + "d" * 64,
        security=Security(run_as_user=0, run_as_non_root=False),
    )
    with pytest.raises(BundleError) as caught:
        build_bundle(
            components(shop, generators),
            name="shop", version="1.0.0", namespace="shop", generators=generators.generators,
        )  # fmt: skip
    assert caught.value.code == "bundle-unsafe-non-root"
    assert {(item.object, item.rule) for item in caught.value.violations} == {
        ("Deployment/root", "non-root"),
        ("Deployment/root", "resources"),
    }


def test_a_waived_rule_is_recorded_in_the_bundle() -> None:
    shop, generators = app()
    worker = shop.deployment(
        "worker",
        image="x@sha256:" + "d" * 64,
        security=Security(read_only_root_filesystem=False),
        resources=Resources(cpu="1m", memory="1Mi", cpu_limit="1m", memory_limit="1Mi"),
    )
    shop.safety_exception(worker, "read-only-root", reason="unpacks plugins at start")
    built = build_bundle(
        components(shop, generators),
        name="shop", version="1.0.0", namespace="shop", generators=generators.generators,
    )  # fmt: skip
    assert [item.public() for item in built.exceptions] == [
        {
            "object": "Deployment/worker",
            "rule": "read-only-root",
            "reason": "unpacks plugins at start",
        }
    ]
