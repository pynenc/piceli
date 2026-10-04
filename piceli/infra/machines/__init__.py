"""Machines as typed Python, provisioned with OpenTofu (``piceli infra``).

Declare servers, fixed IPs, firewalls and DNS records like an ``App``;
Piceli renders the OpenTofu configuration, owns its encrypted state and
applies a plan only with the approval of its hash::

    from piceli.infra import (Cluster, DnsRecord, Hetzner, Hook, Infrastructure,
                              PrimaryIp, Rule, Server)

    hcloud = Hetzner(credentials="hcloud", location="fsn1")
    edge_1 = Server(
        "edge-1", provider=hcloud, type="cax11", image="debian-12",
        ipv4=PrimaryIp("edge-1-v4"), firewall=[Rule.tcp(443), Rule.udp(3478)],
        install=Hook(["install-os", "root@{ipv4}"]),
        cluster=Cluster("edge-1", api="https://edge-1.example.net:6443", credentials="edge-1"),
    )
    infra = Infrastructure("edge", servers=[edge_1],
                           records=[DnsRecord("example.com", "www", "A", server=edge_1)])

``piceli infra plan infra.py:infra`` then ``piceli infra apply infra.py:infra
--approve HASH``. See ``docs/infrastructure.md``.

Importing this package is side-effect free.
"""

from __future__ import annotations

from piceli.infra.machines.hetzner import Hetzner
from piceli.infra.machines.model import (
    DnsRecord,
    Firewall,
    Hook,
    InfraError,
    Infrastructure,
    PrimaryIp,
    Rule,
    Server,
    Ssh,
)
from piceli.infra.machines.provider import Pin, Prices, Provider, Resource
from piceli.infra.machines.state import HttpState, LocalState, StateBackend

__all__ = [
    "DnsRecord",
    "Firewall",
    "Hetzner",
    "Hook",
    "HttpState",
    "InfraError",
    "Infrastructure",
    "LocalState",
    "Pin",
    "Prices",
    "PrimaryIp",
    "Provider",
    "Resource",
    "Rule",
    "Server",
    "Ssh",
    "StateBackend",
]
