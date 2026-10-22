"""0.19 (P0): which ingress policies admit the operator is decided by the pods.

A namespace's ``shop-ingress`` policy selects the app's pods through
``shop.network`` and ``shop.release``, labels no Service selector uses.
Matching policies against Service selectors skipped it, and the web, worker
and monitor pods stayed unreachable from the operator's proxies;
``shop-api-ingress`` uses labels the api Service has, so a one-Service test
passed.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from piceli.envs.access import (
    SKIPPED,
    UNCHANGED,
    Access,
    allow_tailnet,
    selects,
    tailnet_plan,
    tailnet_report,
)

BASE = {
    "app.kubernetes.io/name": "shop",
    "app.kubernetes.io/part-of": "shop-p2",
}
POD = {
    **BASE,
    "shop.network": "primary",
    "shop.release": "r05",
    "piceli.io/managed": "true",
}
OPERATOR = {
    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "tailscale"}}
}


def _deployment(
    name: str, role: str, port: int, kind: str = "Deployment"
) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": "shop-main"},
        "spec": {
            "selector": {"matchLabels": {**BASE, "shop.role": role}},
            "template": {
                "metadata": {"labels": {**POD, "shop.role": role}},
                "spec": {
                    "containers": [{"name": role, "ports": [{"containerPort": port}]}]
                },
            },
        },
    }


def _service(name: str, role: str, *ports: int) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": "shop-main"},
        "spec": {
            "selector": {**BASE, "shop.role": role, "piceli.io/managed": "true"},
            "ports": [
                {"name": f"p{port}", "port": port, "targetPort": port} for port in ports
            ],
        },
    }


SHOP_INGRESS = {
    "apiVersion": "networking.k8s.io/v1",
    "kind": "NetworkPolicy",
    "metadata": {"name": "shop-ingress", "namespace": "shop-main"},
    "spec": {
        "podSelector": {
            "matchLabels": {
                **BASE,
                "shop.network": "primary",
                "shop.release": "r05",
            }
        },
        "policyTypes": ["Ingress"],
        "ingress": [{"from": [{"podSelector": {"matchLabels": BASE}}]}],
    },
}
API_INGRESS = {
    "apiVersion": "networking.k8s.io/v1",
    "kind": "NetworkPolicy",
    "metadata": {"name": "shop-api-ingress", "namespace": "shop-main"},
    "spec": {
        "podSelector": {
            "matchLabels": {"shop.role": "api", "piceli.io/managed": "true"}
        },
        "policyTypes": ["Ingress"],
        "ingress": [
            {
                "from": [{"podSelector": {"matchLabels": BASE}}],
                "ports": [{"port": 4318}],
            }
        ],
    },
}
EGRESS_ONLY = {
    "apiVersion": "networking.k8s.io/v1",
    "kind": "NetworkPolicy",
    "metadata": {"name": "shop-egress", "namespace": "shop-main"},
    "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]},
}


def _shop() -> list[dict[str, Any]]:
    return [
        _deployment("shop-web", "web", 3000),
        _deployment("shop-worker", "worker", 18083),
        _deployment("shop-rustvello-monitor", "monitor", 18084),
        _deployment("shop-api", "api", 8080, kind="StatefulSet"),
        _deployment("shop-redis", "redis", 6379, kind="StatefulSet"),
        _service("shop-web", "web", 3000),
        _service("shop-worker", "worker", 18083),
        _service("shop-rustvello-monitor", "monitor", 18084),
        _service("shop-api", "api", 8080, 4318),
        _service("shop-redis", "redis", 6379),
        copy.deepcopy(SHOP_INGRESS),
        copy.deepcopy(API_INGRESS),
        copy.deepcopy(EGRESS_ONLY),
    ]


EXPOSED = {
    "shop-web": (3000,),
    "shop-worker": (18083,),
    "shop-rustvello-monitor": (18084,),
    "shop-api": (8080,),
}


def _amend(
    manifests: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], Any]:
    access = Access(tailnet=True)
    plan = tailnet_plan(manifests, access, EXPOSED, "main", "shop-main")
    policies = [m for m in manifests if m["kind"] == "NetworkPolicy"]
    changed, notes = allow_tailnet(policies, plan.backends, manifests, access)
    return {m["metadata"]["name"]: m for m in policies}, notes, changed


def test_a_policy_selecting_the_pods_by_other_labels_admits_the_operator() -> None:
    by_name, notes, changed = _amend(_shop())
    rule = by_name["shop-ingress"]["spec"]["ingress"][-1]
    assert rule["from"] == [OPERATOR]
    # The web, worker and monitor pods (and the api: shop-ingress selects its pod
    # too); never Redis, which is not exposed.
    assert rule["ports"] == [  # by Service name
        {"port": 8080, "protocol": "TCP"},
        {"port": 18084, "protocol": "TCP"},
        {"port": 3000, "protocol": "TCP"},
        {"port": 18083, "protocol": "TCP"},
    ]
    api = by_name["shop-api-ingress"]["spec"]["ingress"]
    assert api[-1] == {
        "from": [OPERATOR],
        "ports": [{"port": 8080, "protocol": "TCP"}],
    }
    assert len(api) == 2  # its own rule is kept as declared
    assert (
        by_name["shop-egress"]["spec"] == EGRESS_ONLY["spec"]
    )  # not an ingress policy
    assert {p["metadata"]["name"] for p in changed} == {
        "shop-ingress",
        "shop-api-ingress",
    }
    assert notes == []


def test_amending_twice_changes_nothing_more() -> None:
    manifests = _shop()
    first, _, _ = _amend(manifests)
    again, _, changed = _amend(manifests)
    assert changed == [] and again == first


def test_selectors_are_evaluated_like_kubernetes() -> None:
    labels = {"a": "1", "b": "2"}
    assert selects({}, labels) is True and selects(None, labels) is True
    assert selects({"matchLabels": {"a": "1"}}, labels) is True
    assert selects({"matchLabels": {"a": "2"}}, labels) is False
    expressions = [
        ({"key": "a", "operator": "In", "values": ["1", "3"]}, True),
        ({"key": "a", "operator": "NotIn", "values": ["1"]}, False),
        ({"key": "c", "operator": "NotIn", "values": ["1"]}, True),
        ({"key": "b", "operator": "Exists"}, True),
        ({"key": "b", "operator": "DoesNotExist"}, False),
    ]
    for expression, expected in expressions:
        assert selects({"matchExpressions": [expression]}, labels) is expected
    assert (
        selects({"matchExpressions": [{"key": "a", "operator": "Gt"}]}, labels) is None
    )
    assert selects({"matchLabels": ["a"]}, labels) is None


def test_match_expressions_select_the_pods_and_unknown_ones_are_reported() -> None:
    manifests = _shop()
    expressions = copy.deepcopy(SHOP_INGRESS)
    expressions["metadata"]["name"] = "by-expression"
    expressions["spec"]["podSelector"] = {
        "matchExpressions": [{"key": "shop.role", "operator": "In", "values": ["web"]}]
    }
    odd = copy.deepcopy(SHOP_INGRESS)
    odd["metadata"]["name"] = "odd"
    odd["spec"]["podSelector"] = {"matchExpressions": [{"key": "x", "operator": "Gt"}]}
    manifests += [expressions, odd]
    by_name, notes, _ = _amend(manifests)
    assert by_name["by-expression"]["spec"]["ingress"][-1]["ports"] == [
        {"port": 3000, "protocol": "TCP"}
    ]
    assert by_name["odd"]["spec"] == odd["spec"]
    assert [(n["code"], n["policy"], n["why"]) for n in notes] == [
        (UNCHANGED, "odd", "selector-unevaluable")
    ]


def test_skipped_services_and_unknown_pods_are_notes() -> None:
    manifests = _shop() + [
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "external"},
         "spec": {"ports": [{"port": 80}]}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "elsewhere"},
         "spec": {"selector": {"app": "not-here"}, "ports": [{"port": 80}]}},
    ]  # fmt: skip
    access = Access(tailnet=True)
    exposed = {
        **EXPOSED,
        "external": (),
        "elsewhere": (),
        "gone": (),
        "shop-redis": (9999,),
    }
    report = tailnet_report(manifests, access, exposed, "main", "shop-main")
    reasons = {(n["code"], n.get("service"), n["why"]) for n in report["notes"]}
    assert reasons == {
        (SKIPPED, "external", "no-selector"),
        (SKIPPED, "gone", "not-found"),
        (SKIPPED, "shop-redis", "no-matching-port"),
        (UNCHANGED, "elsewhere", "pods-not-in-release"),
    }
    assert {item["host"] for item in report["tailnet"]} >= {
        "main-shop-web",
        "main-shop-api",
    }
    assert report["policies"] == []  # nothing amended in these manifests yet


@pytest.mark.parametrize("tags", [["k8s"], ["tag:"], ["tag:a b"], "tag:k8s"])
def test_tags_are_tailscale_tags(tags: Any) -> None:
    from piceli.envs import EnvError

    with pytest.raises(EnvError):
        Access(tailnet=True, tags=tags)


def test_tags_proxy_class_and_https_render_for_the_operator() -> None:
    access = Access(
        tailnet=True, tags=["tag:k8s", "tag:family"], proxy_class="small", https=True
    )
    plan = tailnet_plan(
        _shop(), access, EXPOSED, "main", "shop-main",
        http={"shop-web": (3000,), "shop-api": (8080,)},
    )  # fmt: skip
    by = {(o["kind"], o["metadata"]["name"]): o for o in plan.objects}
    web = by[("Ingress", "shop-web-tailnet")]
    assert web["spec"]["ingressClassName"] == "tailscale"
    assert web["spec"]["tls"] == [{"hosts": ["main-shop-web"]}]
    assert web["spec"]["defaultBackend"]["service"] == {
        "name": "shop-web", "port": {"number": 3000},
    }  # fmt: skip
    assert web["metadata"]["annotations"]["tailscale.com/tags"] == "tag:k8s,tag:family"
    assert web["metadata"]["labels"]["tailscale.com/proxy-class"] == "small"
    assert ("Service", "shop-web-tailnet") not in by
    # The worker's forward is not HTTP: a plain companion, tags too.
    worker = by[("Service", "shop-worker-tailnet")]
    assert (
        worker["metadata"]["annotations"]["tailscale.com/tags"] == "tag:k8s,tag:family"
    )
    # The api: exposed 8080 only, and it is HTTP.
    assert ("Ingress", "shop-api-tailnet") in by
    mixed = tailnet_plan(
        _shop(),
        access,
        {"shop-api": ()},
        "main",
        "shop-main",
        http={"shop-api": (8080,)},
    )
    assert [o["kind"] for o in mixed.objects] == ["Service"]
    assert mixed.notes[0]["why"] == "https-mixed-ports"
