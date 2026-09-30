"""Typed egress, namespace selectors and policy types of ``network_policy``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from piceli import App, NetworkPeer, NetworkPort, NetworkRule


def _app():
    app = App("shop")
    api = app.deployment("api", image="img:1", ports=[8080])
    db = app.deployment("db", image="img:1", ports=[5432])
    return app, api, db


def _spec(policy, namespace="ns"):
    return policy.manifest(namespace, {})["spec"]


def test_ingress_only_policy_is_unchanged():
    app, api, db = _app()
    policy = app.network_policy(api, allow_from=[db], ports=[8080])
    assert policy.manifest("ns", {}) == {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "api-ingress", "namespace": "ns"},
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "api"}},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {
                            "podSelector": {
                                "matchLabels": {"app.kubernetes.io/name": "db"}
                            }
                        }
                    ],
                    "ports": [{"port": 8080, "protocol": "TCP"}],
                }
            ],
        },
    }
    deny = app.network_policy(selector={"a": "b"}, name="deny")
    assert _spec(deny)["ingress"] == [] and _spec(deny)["policyTypes"] == ["Ingress"]


def test_egress_to_workload_dns_and_ports():
    app, api, db = _app()
    policy = app.network_policy(api, egress=[db], allow_dns=True)
    assert policy.name == "api-egress"
    spec = _spec(policy)
    assert spec["policyTypes"] == ["Egress"]
    assert "ingress" not in spec
    assert spec["egress"] == [
        {"to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "db"}}}]},
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                }
            ],
            "ports": [
                {"port": 53, "protocol": "UDP"},
                {"port": 53, "protocol": "TCP"},
            ],
        },
    ]


def test_typed_peers_ports_and_namespaces():
    app, api, _ = _app()
    policy = app.network_policy(
        api,
        allow_from=[],
        ingress=[
            NetworkRule(
                peers=[NetworkPeer.namespace(labels={"team": "edge"})], ports=[8080]
            )
        ],
        egress=[
            NetworkRule(
                peers=[
                    NetworkPeer.cidr("10.0.0.0/8", except_=["10.1.0.0/16"]),
                    NetworkPeer.pods({"role": "cache"}),
                ],
                ports=[NetworkPort(port=6000, end_port=6010), 443],
            ),
            NetworkRule(ports=[NetworkPort(port=53, protocol="UDP")]),
        ],
    )
    spec = _spec(policy)
    assert spec["policyTypes"] == ["Ingress", "Egress"]
    assert spec["ingress"] == [
        {
            "from": [{"namespaceSelector": {"matchLabels": {"team": "edge"}}}],
            "ports": [{"port": 8080, "protocol": "TCP"}],
        }
    ]
    assert spec["egress"][0] == {
        "to": [
            {"ipBlock": {"cidr": "10.0.0.0/8", "except": ["10.1.0.0/16"]}},
            {"podSelector": {"matchLabels": {"role": "cache"}}},
        ],
        "ports": [
            {"port": 6000, "protocol": "TCP", "endPort": 6010},
            {"port": 443, "protocol": "TCP"},
        ],
    }
    assert spec["egress"][1] == {"ports": [{"port": 53, "protocol": "UDP"}]}


def test_explicit_policy_types_and_deny_all_egress():
    app, api, _ = _app()
    deny = app.network_policy(api, policy_types=["Egress"], name="api-no-egress")
    assert _spec(deny) == {
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "api"}},
        "policyTypes": ["Egress"],
        "egress": [],
    }
    both = app.network_policy(
        selector={"a": "b"}, name="both", policy_types=["Ingress", "Egress"]
    )
    assert _spec(both)["policyTypes"] == ["Ingress", "Egress"]
    assert _spec(both)["ingress"] == [] and _spec(both)["egress"] == []


def test_namespace_and_pod_in_namespace():
    peer = NetworkPeer.namespace("prod", pods={"app": "x"})
    assert peer.manifest() == {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "prod"}},
        "podSelector": {"matchLabels": {"app": "x"}},
    }


@pytest.mark.parametrize(
    "build",
    [
        lambda: NetworkPeer.cidr("10.0.0.1/8"),
        lambda: NetworkPeer.cidr("nonsense"),
        lambda: NetworkPeer.cidr("10.0.0.0/8", except_=["192.168.0.0/16"]),
        lambda: NetworkPeer.cidr("10.0.0.0/8", except_=["10.0.0.0/8"]),
        lambda: NetworkPeer.cidr("10.0.0.0/8", except_=["fd00::/8"]),
        lambda: NetworkPeer.pods({}),
        lambda: NetworkPeer.namespace(labels={}),
        lambda: NetworkPeer.namespace(),
        lambda: NetworkPeer(),
        lambda: NetworkPort(port=0),
        lambda: NetworkPort(port=70000),
        lambda: NetworkPort(port=80, protocol="ICMP"),
        lambda: NetworkPort(port=90, end_port=80),
        lambda: NetworkRule(),
    ],
)
def test_invalid_rules_are_refused(build):
    with pytest.raises((ValidationError, ValueError)):
        build()


def test_policy_validation_and_unknown_workload():
    app, api, _ = _app()
    with pytest.raises(ValueError, match="need 'Egress'"):
        app.network_policy(
            api, egress=[NetworkRule(ports=[53])], policy_types=["Ingress"]
        )
    with pytest.raises(ValueError, match="need 'Ingress'"):
        app.network_policy(api, ports=[80], policy_types=["Egress"])
    stranger = App("other").deployment("ghost", image="img:1")
    with pytest.raises(ValueError, match="not declared in this app"):
        app.network_policy(api, egress=[stranger])
