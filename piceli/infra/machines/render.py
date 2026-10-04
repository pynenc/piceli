"""Render an :class:`~piceli.infra.Infrastructure` to OpenTofu's JSON configuration.

:func:`render` returns ``main.tf.json`` (sorted keys, no paths, no time, no
secrets) and ``.terraform.lock.hcl`` (the pinned providers' hashes, so
``tofu init -lockfile=readonly`` verifies every download). Same declaration,
same bytes: the plan hash starts from them.

Every resource is labelled ``piceli.io/managed-by=piceli``,
``piceli.io/infra=<name>`` and ``piceli.io/resource=<kind>-<name>``; destroy
only touches resources that carry them (:mod:`piceli.infra.machines.plan`).

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from piceli.infra.machines.model import (
    INFRA_LABEL,
    MANAGED_BY,
    RESOURCE_LABEL,
    Firewall,
    Infrastructure,
)
from piceli.infra.machines.provider import Resource

__all__ = ["MIN_TOFU", "Rendered", "labels", "render"]

#: The oldest OpenTofu Piceli runs (state encryption, early evaluation).
MIN_TOFU = (1, 8, 0)
LOCK_HEADER = (
    "# This file is maintained by Piceli (piceli infra): the pinned providers.\n"
    "# Manual edits are replaced on the next plan.\n"
)


def labels(
    infra: Infrastructure, kind: str, name: str, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """The ownership labels of one resource (plus the declaration's own)."""
    return {
        **(extra or {}),
        MANAGED_BY: "piceli",
        INFRA_LABEL: infra.name,
        RESOURCE_LABEL: f"{kind}-{name}"[:63].rstrip("-."),
    }


@dataclass(frozen=True)
class Rendered:
    """The configuration and lock file of one declaration."""

    config: dict[str, Any]
    lock: str
    #: OpenTofu address -> (provider name, kind, declared name) of every resource.
    resources: dict[str, tuple[str, str, str]]

    @property
    def config_text(self) -> str:
        return json.dumps(self.config, sort_keys=True, indent=2) + "\n"

    @property
    def digest(self) -> str:
        """``sha256:`` of the configuration and the lock file."""
        hasher = hashlib.sha256()
        hasher.update(self.config_text.encode())
        hasher.update(b"\0")
        hasher.update(self.lock.encode())
        return "sha256:" + hasher.hexdigest()


def render(infra: Infrastructure) -> Rendered:
    """The OpenTofu configuration (JSON syntax) of ``infra``."""
    resources: list[Resource] = []
    owners: dict[str, tuple[str, str, str]] = {}

    def add(resource: Resource, provider: str, kind: str, name: str) -> Resource:
        if resource.address in owners:
            raise ValueError(f"two resources render as {resource.address}")
        owners[resource.address] = (provider, kind, name)
        resources.append(resource)
        return resource

    firewalls: dict[str, Resource] = {}
    for firewall in infra.firewalls():
        users = [s for s in infra.servers if s.firewall == firewall]
        provider = users[0].provider
        for item in provider.render_firewall(
            firewall, labels(infra, "firewall", firewall.name)
        ):
            add(item, provider.name, "firewall", firewall.name)
            firewalls.setdefault(firewall.name, item)
    keys: dict[tuple[str, str], Resource] = {}
    outputs: dict[str, Any] = {}
    for server in infra.servers:
        provider = server.provider
        ips: dict[str, Resource] = {}
        for ip in server.primary_ips():
            ips[ip.name] = add(
                provider.render_primary_ip(ip, server, labels(infra, "ip", ip.name)),
                provider.name,
                "ip",
                ip.name,
            )
        server_keys: list[Resource] = []
        for public in server.ssh_keys:
            digest = hashlib.sha256(public.split()[1].encode()).hexdigest()[:12]
            found = keys.get((provider.name, digest))
            if found is None:
                name = f"{infra.name}-{digest}"
                made = provider.render_ssh_key(
                    name, digest, public, labels(infra, "key", digest)
                )
                if made is not None:
                    found = keys[(provider.name, digest)] = add(
                        made, provider.name, "key", digest
                    )
            if found is not None:
                server_keys.append(found)
        resource = add(
            provider.render_server(
                server,
                ips=ips,
                firewall=(
                    firewalls.get(server.firewall.name)
                    if isinstance(server.firewall, Firewall)
                    else None
                ),
                labels=labels(infra, "server", server.name, dict(server.labels)),
                ssh_keys=server_keys,
            ),
            provider.name,
            "server",
            server.name,
        )
        value: dict[str, Any] = {"id": provider.server_id(resource)}
        for family in ("ipv4", "ipv6"):
            if getattr(server, family) is not False:
                value[family] = provider.address(resource, family)
        status = provider.server_status(resource)
        if status is not None:
            value["status"] = status
        outputs[f"server-{server.name}"] = {"value": value}
    servers = {
        address: resource
        for resource in resources
        for address in [resource.address]
        if owners[address][1] == "server"
    }
    for record in infra.records:
        assert record.provider is not None
        if record.servers:
            family = "ipv4" if record.type == "A" else "ipv6"
            values = sorted(
                record.provider.address(
                    next(r for a, r in servers.items() if owners[a][2] == item.name),
                    family,
                )
                for item in record.servers
            )
        else:
            assert isinstance(record.value, tuple)
            values = list(record.value)
        add(
            record.provider.render_record(
                record,
                values,
                labels(infra, "record", record.resource_name.removeprefix("record-")),
            ),
            record.provider.name,
            "record",
            record.key,
        )
    body: dict[str, dict[str, Any]] = {}
    for resource in resources:
        body.setdefault(resource.type, {})[resource.name] = dict(resource.body)
    terraform: dict[str, Any] = {
        "required_version": ">= " + ".".join(map(str, MIN_TOFU))
    }
    pins = [
        p for p in (provider.pin() for provider in infra.providers()) if p is not None
    ]
    if pins:
        terraform["required_providers"] = {
            pin.local_name: {"source": pin.source, "version": pin.version}
            for pin in pins
        }
    assert infra.backend is not None
    backend = infra.backend.render()
    if backend is not None:
        terraform["backend"] = backend
    config: dict[str, Any] = {"terraform": terraform}
    providers = {
        pin.local_name: block
        for provider in infra.providers()
        for pin in [provider.pin()]
        for block in [provider.provider_block()]
        if pin is not None and block is not None
    }
    if providers:
        config["provider"] = providers
    if body:
        config["resource"] = body
    if outputs:
        config["output"] = outputs
    lock = LOCK_HEADER + "".join(
        "\n" + pin.lock_block() for pin in sorted(pins, key=lambda p: p.source)
    )
    return Rendered(config, lock, owners)
