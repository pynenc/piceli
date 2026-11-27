"""0.91.0: a tailnet device that is also a node of the cluster reaches the
LoadBalancer companions.

The Tailscale operator marks a companion's tailnet IP ``ipMode: VIP``, so
kube-proxy on every node sends traffic for that IP straight to the pod. From
a node, the packet then comes from the node (on flannel: ``flannel.1``, the
first address of the node's pod range), not from the operator's proxy, and a
policy that admits only the operator's namespace refused it.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from piceli import App, Branch, EnvConfig, Pipeline, Target
from piceli.envs import Access, EnvError, Environment, env_pipeline
from piceli.envs.access import allow_tailnet, node_peers, tailnet_plan
from piceli.envs.ops import _node_addresses
from piceli.pipeline.compose import offline_composition, tailnet_report

WEB = "example/web@sha256:" + "1" * 64
DB = "example/db@sha256:" + "2" * 64
NODES = ("10.0.0.4/32", "10.42.3.0/32", "10.42.3.1/32")


def _node(name: str, ip: str, *ranges: str) -> dict[str, Any]:
    return {
        "metadata": {"name": name},
        "spec": {"podCIDR": ranges[0], "podCIDRs": list(ranges)} if ranges else {},
        "status": {
            "addresses": [
                {"type": "Hostname", "address": name},
                {"type": "InternalIP", "address": ip},
            ]
        },
    }


def test_node_peers_are_the_internal_ips_and_the_pod_range_gateways() -> None:
    nodes = [
        _node("web-1", "10.0.0.5", "10.42.3.0/24", "fd00:42:3::/64"),
        _node("web-2", "10.0.0.4", "10.42.0.0/24"),
        _node("web-3", "not-an-ip"),  # no pod range, a bad address: skipped
        {"spec": {"podCIDR": "garbage"}},
    ]
    assert node_peers(nodes) == (
        "10.0.0.4/32",
        "10.0.0.5/32",
        "10.42.0.0/32",
        "10.42.0.1/32",
        "10.42.3.0/32",
        "10.42.3.1/32",
        "fd00:42:3::/128",
        "fd00:42:3::1/128",
    )
    assert node_peers([]) == ()


def _pipeline(access: Access) -> Pipeline:
    app = App("shop")
    web = app.deployment("web", image=WEB, ports=[3000])
    db = app.deployment("db", image=DB, ports=[5432])
    app.service(web, port=3000, access=app.access.forward(local=18080, path="/login"))
    app.service(db, port=5432)
    app.network_policy(web, allow_from=[db], ports=[3000])
    return Pipeline(
        app,
        Target(Path("/nonexistent/kc"), context="c", namespace="shop"),
        state_dir=Path("/nonexistent/state"),
        envs=EnvConfig(
            prefix="shop-",
            environments=[
                Environment(
                    "main",
                    namespace="shop-main",
                    follow=Branch("main"),
                    access=access,
                )
            ],
        ),
    )


def test_the_policy_admits_the_nodes_on_the_loadbalancer_ports() -> None:
    derived = env_pipeline(
        _pipeline(Access(tailnet=True)), "main", node_addresses=NODES
    )
    _, composition = offline_composition(derived)
    (policy,) = [
        dict(r.manifest)
        for c in composition.components
        for r in c.resources
        if r.ref.kind == "NetworkPolicy"
    ]
    rules = policy["spec"]["ingress"]
    assert len(rules) == 3  # the app's own, the operator's, the nodes'
    assert rules[-1] == {
        "from": [{"ipBlock": {"cidr": cidr}} for cidr in NODES],
        "ports": [{"port": 3000, "protocol": "TCP"}],
    }
    report = tailnet_report(derived)
    assert report is not None and report["nodes"] == list(NODES)


def test_without_node_addresses_only_the_operator_is_admitted() -> None:
    _, composition = offline_composition(
        env_pipeline(_pipeline(Access(tailnet=True)), "main")
    )
    (policy,) = [
        dict(r.manifest)
        for c in composition.components
        for r in c.resources
        if r.ref.kind == "NetworkPolicy"
    ]
    assert not any(
        "ipBlock" in peer
        for rule in policy["spec"]["ingress"]
        for peer in rule.get("from") or ()
    )


def _manifests() -> list[dict[str, Any]]:
    def service(name: str, port: int) -> dict[str, Any]:
        return {
            "kind": "Service",
            "metadata": {"name": name, "namespace": "shop-main"},
            "spec": {"selector": {"role": name}, "ports": [{"port": port}]},
        }

    def deployment(name: str) -> dict[str, Any]:
        return {
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": "shop-main"},
            "spec": {"template": {"metadata": {"labels": {"role": name}}}},
        }

    return [
        deployment("web"),
        deployment("turn"),
        service("web", 3000),
        service("turn", 3478),
        {
            "kind": "NetworkPolicy",
            "metadata": {"name": "shop-ingress", "namespace": "shop-main"},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []},
        },
    ]


def test_ports_served_by_a_tailnet_ingress_are_not_opened_to_the_nodes() -> None:
    """An Ingress is not short-circuited: its proxy reaches the pod."""
    manifests = _manifests()
    access = Access(tailnet=True, https=True)
    exposed = {"web": (3000,), "turn": (3478,)}
    plan = tailnet_plan(
        manifests, access, exposed, "main", "shop-main", {"web": (3000,)}
    )
    assert {b.service: b.plain for b in plan.backends} == {
        "turn": True,
        "web": False,
    }
    policies = [m for m in manifests if m["kind"] == "NetworkPolicy"]
    changed, notes = allow_tailnet(policies, plan.backends, manifests, access, NODES)
    assert notes == [] and len(changed) == 1
    operator, nodes = policies[0]["spec"]["ingress"]
    assert [p["port"] for p in operator["ports"]] == [3478, 3000]
    assert nodes["ports"] == [{"port": 3478, "protocol": "TCP"}]
    again = copy.deepcopy(policies)
    assert allow_tailnet(again, plan.backends, manifests, access, NODES)[0] == []
    assert again == policies


class _Nodes:
    def __init__(self, result: Any) -> None:
        self.result = result

    def node_addresses(self) -> tuple[str, ...]:
        if isinstance(self.result, Exception):
            raise self.result
        return tuple(self.result)


def test_env_up_reads_the_nodes_and_carries_on_when_it_cannot() -> None:
    said: list[str] = []
    assert _node_addresses(_Nodes(NODES), said.append) == NODES
    assert _node_addresses(object(), said.append) == ()  # a port without nodes
    assert said == []
    denied = EnvError(
        "env-cluster-unavailable", "the Kubernetes API refused list nodes"
    )
    assert _node_addresses(_Nodes(denied), said.append) == ()
    assert len(said) == 1 and "cannot list the nodes" in said[0]
