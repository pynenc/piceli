"""The machine declaration: validation, normalisation, deterministic rendering."""

from __future__ import annotations

import json
from typing import Any

import pytest

from piceli.infra import (
    Cluster,
    DnsRecord,
    Firewall,
    Hetzner,
    Hook,
    HttpState,
    Infrastructure,
    PrimaryIp,
    Rule,
    Server,
    Ssh,
)
from piceli.infra.machines.model import InfraError
from piceli.infra.machines.render import render
from piceli.testing.infra import FakeProvider

HCLOUD = Hetzner(credentials="hcloud", location="fsn1")
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTests owner@example"


def _server(**overrides: Any) -> Server:
    values: dict[str, Any] = {
        "name": "edge-1",
        "provider": HCLOUD,
        "type": "cax11",
        "image": "debian-12",
    }
    values.update(overrides)
    return Server(**values)


def _invalid(match: str) -> Any:
    return pytest.raises(InfraError, match=match)


def test_rules_normalise_and_validate() -> None:
    rule = Rule.tcp(443, sources=["10.0.0.0/8", "10.0.0.0/8", "::/0"], name="https")
    assert rule.sources == ("10.0.0.0/8", "::/0")
    assert rule.port_text == "443"
    assert Rule.udp((3478, 3480)).port_text == "3478-3480"
    assert Rule.icmp().port_text is None
    with _invalid("CIDR"):
        Rule.tcp(22, sources=["10.0.0.1/8"])  # host bits set
    with _invalid("port"):
        Rule.tcp(0)
    with _invalid("low < high"):
        Rule.tcp((10, 10))
    with _invalid("no port"):
        Rule("icmp", 1)


def test_a_rule_list_becomes_a_firewall_named_after_the_server() -> None:
    server = _server(firewall=[Rule.tcp(443), Rule.udp(3478)])
    assert isinstance(server.firewall, Firewall)
    assert server.firewall.name == "edge-1"
    with _invalid("listed twice"):
        Firewall("edge", rules=[Rule.tcp(443), Rule.tcp(443)])


def test_server_checks_names_types_addresses_and_keys() -> None:
    with _invalid("DNS label"):
        _server(name="Edge_1")
    with _invalid("not one of cax11"):
        _server(type="cx22")
    assert _server(
        type="cx22",
        provider=Hetzner(credentials="h", location="fsn1", server_types=("cx22",)),
    )
    with _invalid("location is required"):
        _server(provider=Hetzner(credentials="h"))
    with _invalid("PrimaryIp\\(kind='ipv4'\\)"):
        _server(ipv4=PrimaryIp("v6", kind="ipv6"))
    with _invalid("no public address"):
        _server(ipv4=False, ipv6=False)
    assert _server(ipv4=False, ipv6=False, ssh=Ssh(address="edge-1.example.net"))
    with _invalid("public key line"):
        _server(ssh_keys=["not a key"])
    with _invalid("piceli.io/ labels"):
        _server(labels={"piceli.io/infra": "x"})
    assert _server(ssh_keys=[KEY, KEY]).ssh_keys == (KEY,)


def test_hook_placeholders_are_checked_and_rendered() -> None:
    hook = Hook(["install-os", "--flake", ".#{name}", "root@{ipv4}"])
    assert hook.render({"name": "edge-1", "ipv4": "203.0.113.7"}) == [
        "install-os", "--flake", ".#edge-1", "root@203.0.113.7",
    ]  # fmt: skip
    with _invalid("placeholder"):
        Hook(["x", "{password}"])
    with _invalid("unbalanced"):
        Hook(["x", "{ipv4"])
    with pytest.raises(InfraError) as caught:
        Hook(["x", "{ipv6}"]).render({"name": "edge-1"})
    assert caught.value.code == "infra-install-unavailable"


def test_dns_records() -> None:
    server = _server()
    record = DnsRecord("example.com", "www", "A", server=server)
    assert record.servers == (server,) and record.provider == HCLOUD
    assert DnsRecord(
        "example.com", "@", "TXT", value=["b", "a"], provider=HCLOUD
    ).value == ("a", "b")
    with _invalid("not both"):
        DnsRecord("example.com", "www", "A", value="203.0.113.1", server=server)
    with _invalid("provider= is required"):
        DnsRecord("example.com", "www", "A", value="203.0.113.1")
    with _invalid("does not fit"):
        DnsRecord("example.com", "www", "AAAA", value="203.0.113.1", provider=HCLOUD)
    with _invalid("no ipv6"):
        DnsRecord("example.com", "www", "AAAA", server=_server(ipv6=False))
    with _invalid("needs type A or AAAA"):
        DnsRecord("example.com", "www", "TXT", server=server)


def test_infrastructure_cross_checks() -> None:
    one, two = _server(), _server(name="edge-2")
    with _invalid("declared twice"):
        Infrastructure("edge", servers=[one, _server()])
    with _invalid("not in servers"):
        Infrastructure(
            "edge",
            servers=[one],
            records=[DnsRecord("example.com", "w", "A", server=two)],
        )
    ip = PrimaryIp("shared")
    with _invalid("two servers"):
        Infrastructure(
            "edge", servers=[_server(ipv4=ip), _server(name="edge-2", ipv4=ip)]
        )
    with _invalid("two different firewalls"):
        Infrastructure(
            "edge",
            servers=[
                _server(firewall=Firewall("fw", [Rule.tcp(1)])),
                _server(name="edge-2", firewall=Firewall("fw", [Rule.tcp(2)])),
            ],
        )
    with _invalid("two different hetzner"):
        Infrastructure(
            "edge",
            servers=[
                one,
                _server(
                    name="edge-2",
                    provider=Hetzner(credentials="other", location="fsn1"),
                ),
            ],
        )
    assert Infrastructure("edge", servers=[one]).state_key == "edge-state"
    with _invalid("credential name"):
        Infrastructure("edge", state_key="Not A Name")


def test_rendering_is_deterministic_and_labelled() -> None:
    cluster = Cluster(
        "edge-1", api="https://edge-1.example.net:6443", credentials="edge-1"
    )

    def build() -> Infrastructure:
        a = _server(
            ipv4=PrimaryIp("edge-1-v4"),
            firewall=Firewall("edge", [Rule.udp(3478), Rule.tcp(443)]),
            ssh_keys=[KEY],
            cluster=cluster,
            labels={"team": "web"},
        )
        b = _server(
            name="edge-2",
            firewall=Firewall("edge", [Rule.udp(3478), Rule.tcp(443)]),
            ssh_keys=[KEY],
        )
        return Infrastructure(
            "edge",
            servers=[a, b],
            records=[
                DnsRecord("example.com", "www", "A", servers=[b, a]),
                DnsRecord(
                    "example.com", "@", "TXT", value="v=spf1 -all", provider=HCLOUD
                ),
            ],
        )

    first, second = render(build()), render(build())
    assert first.config_text == second.config_text and first.digest == second.digest
    config = first.config
    for kind, items in config["resource"].items():
        for name, body in items.items():
            labels = body["labels"]
            assert labels["piceli.io/managed-by"] == "piceli", (kind, name)
            assert labels["piceli.io/infra"] == "edge"
    resources = config["resource"]
    assert set(resources) == {
        "hcloud_firewall",
        "hcloud_primary_ip",
        "hcloud_server",
        "hcloud_ssh_key",
        "hcloud_zone_rrset",
    }
    assert len(resources["hcloud_ssh_key"]) == 1  # one key, shared
    server = resources["hcloud_server"]["server-edge-1"]
    assert server["public_net"] == [
        {
            "ipv4": "${hcloud_primary_ip.ip-edge-1-v4.id}",
            "ipv4_enabled": True,
            "ipv6_enabled": True,
        }
    ]
    assert server["firewall_ids"] == ["${hcloud_firewall.firewall-edge.id}"]
    assert server["labels"]["team"] == "web"
    www = resources["hcloud_zone_rrset"]["record-example-com-www-A".lower()]
    assert www["records"] == [
        {"value": "${hcloud_server.server-edge-1.ipv4_address}"},
        {"value": "${hcloud_server.server-edge-2.ipv4_address}"},
    ]
    assert config["terraform"]["required_providers"]["hcloud"] == {
        "source": "registry.opentofu.org/hetznercloud/hcloud",
        "version": "1.69.0",
    }
    assert 'provider "registry.opentofu.org/hetznercloud/hcloud"' in first.lock
    assert "zh:" in first.lock and "h1:" in first.lock
    # No token, no credential name value, no path in the configuration.
    text = first.config_text
    assert "HCLOUD_TOKEN" not in text and "token" not in json.dumps(config["provider"])


def test_a_different_declaration_changes_the_digest() -> None:
    base = render(Infrastructure("edge", servers=[_server()]))
    other = render(Infrastructure("edge", servers=[_server(image="debian-13")]))
    assert base.digest != other.digest


def test_mock_endpoint_goes_into_the_provider_block_only() -> None:
    provider = Hetzner(
        credentials="hcloud",
        location="fsn1",
        endpoint="http://127.0.0.1:9/v1",
        poll_interval="200ms",
    )
    rendered = render(Infrastructure("edge", servers=[_server(provider=provider)]))
    assert rendered.config["provider"]["hcloud"] == {
        "endpoint": "http://127.0.0.1:9/v1",
        "poll_interval": "200ms",
    }
    with _invalid("endpoint"):
        Hetzner(credentials="h", endpoint="ftp://x")


def test_fake_provider_renders_builtin_resources_only() -> None:
    fake = FakeProvider(addresses={"edge-1": "127.0.0.1"})
    server = _server(provider=fake, type="t1", image="img", ipv4=PrimaryIp("v4"))
    rendered = render(Infrastructure("edge", servers=[server]))
    assert set(rendered.config["resource"]) == {"terraform_data"}
    assert "required_providers" not in rendered.config["terraform"]
    assert rendered.resources["terraform_data.server-edge-1"] == (
        "fake",
        "server",
        "edge-1",
    )


def test_http_state_backend_renders_without_credentials() -> None:
    backend = HttpState(
        "https://state.example.net/edge",
        lock_address="https://state.example.net/edge/lock",
        credentials="state-store",
    )
    rendered = render(Infrastructure("edge", servers=[_server()], backend=backend))
    assert rendered.config["terraform"]["backend"] == {
        "http": {
            "address": "https://state.example.net/edge",
            "lock_address": "https://state.example.net/edge/lock",
            "unlock_address": "https://state.example.net/edge/lock",
        }
    }
    with _invalid("without credentials"):
        HttpState("https://user:pw@state.example.net/edge")


def test_describe_has_no_credentials() -> None:
    body = Infrastructure("edge", servers=[_server()]).describe()
    assert body["schema"] == "piceli.infra.v1"
    assert body["providers"][0]["credentials"] == "hcloud"  # the name, never a value
    assert body["servers"][0]["ipv4"] is True
