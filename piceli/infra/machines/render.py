"""Render an :class:`~piceli.infra.Infrastructure` to OpenTofu's JSON configuration.

:func:`render` returns ``main.tf.json`` (sorted keys, no paths, no time, no
secrets) and ``.terraform.lock.hcl`` (the pinned providers' hashes, so
``tofu init -lockfile=readonly`` verifies every download). Same declaration,
same bytes: the plan hash starts from them.

A server's :class:`~piceli.infra.machines.model.UserData` is a sensitive
variable ``user_data_<server>`` (default ``null``): the value reaches
OpenTofu only through ``TF_VAR_user_data_<server>`` in its environment, when
a plan creates the server (:mod:`piceli.infra.machines.plan`).

Every resource is labelled ``piceli.io/managed-by=piceli``,
``piceli.io/infra=<name>`` and ``piceli.io/resource=<kind>-<name>``; destroy
only touches resources that carry them (:mod:`piceli.infra.machines.plan`).

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from piceli.infra.machines.model import (
    INFRA_LABEL,
    MANAGED_BY,
    RESOURCE_LABEL,
    Firewall,
    Infrastructure,
)
from piceli.infra.machines.provider import Resource

__all__ = ["MIN_TOFU", "Rendered", "labels", "render", "user_data_variable"]

#: The oldest OpenTofu Piceli runs (state encryption, early evaluation).
MIN_TOFU = (1, 8, 0)
LOCK_HEADER = (
    "# This file is maintained by Piceli (piceli infra): the pinned providers.\n"
    "# Manual edits are replaced on the next plan.\n"
)


def user_data_variable(server: str) -> str:
    """The OpenTofu variable holding a server's user data (``user_data_web_1``)."""
    return "user_data_" + server.replace("-", "_")


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
    #: Server address -> (its server name, its user data's secret name).
    user_data: dict[str, tuple[str, str]] = field(default_factory=dict)

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

    for image, provider in infra.images():
        add(
            provider.render_image(image, labels(infra, "image", image.name)),
            provider.name,
            "image",
            image.name,
        )
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
    #: Server name -> (its resource, its rendered PrimaryIps).
    hosts: dict[str, tuple[Resource, dict[str, Resource]]] = {}
    variables: dict[str, Any] = {}
    user_data: dict[str, tuple[str, str]] = {}
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
        made = provider.render_server(
            server,
            ips=ips,
            firewall=(
                firewalls.get(server.firewall.name)
                if isinstance(server.firewall, Firewall)
                else None
            ),
            labels=labels(infra, "server", server.name, dict(server.labels)),
            ssh_keys=server_keys,
        )
        if server.user_data is not None:
            variable = user_data_variable(server.name)
            variables[variable] = {
                "type": "string",
                "default": None,
                "sensitive": True,
                "description": f"user data of {server.name} (creation only)",
            }
            made = provider.render_user_data(made, f"${{var.{variable}}}")
            user_data[made.address] = (server.name, server.user_data.secret)
        resource = add(made, provider.name, "server", server.name)
        hosts[server.name] = (resource, ips)
        value: dict[str, Any] = {"id": provider.server_id(resource)}
        for family in ("ipv4", "ipv6"):
            if getattr(server, family) is not False:
                value[family] = provider.address(resource, family)
        status = provider.server_status(resource)
        if status is not None:
            value["status"] = status
        outputs[f"server-{server.name}"] = {"value": value}
    for record in infra.records:
        assert record.provider is not None
        if record.servers:
            # Through each server's own provider: a reference into this
            # configuration, also when the record is at another provider.
            family = "ipv4" if record.type == "A" else "ipv6"
            values = sorted(
                item.provider.record_address(
                    item, hosts[item.name][0], family, hosts[item.name][1]
                )
                for item in record.servers
            )
        else:
            assert isinstance(record.value, tuple)
            values = list(record.value)
        for made in record.provider.render_records(
            record,
            values,
            labels(infra, "record", record.resource_name.removeprefix("record-")),
        ):
            add(made, record.provider.name, "record", record.key)
    for bucket in infra.buckets:
        add(
            bucket.provider.render_bucket(bucket, labels(infra, "bucket", bucket.name)),
            bucket.provider.name,
            "bucket",
            bucket.name,
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
    if variables:
        config["variable"] = variables
    providers = {
        pin.local_name: block
        for provider in infra.providers()
        for pin in [provider.pin()]
        for block in [provider.provider_block()]
        if pin is not None and block is not None
    }
    if providers:
        config["provider"] = providers
    data: dict[str, dict[str, Any]] = {}
    for provider in infra.providers():
        for lookup in provider.data_sources():
            data.setdefault(lookup.type, {})[lookup.name] = dict(lookup.body)
    if data:
        config["data"] = data
    if body:
        config["resource"] = body
    if outputs:
        config["output"] = outputs
    lock = LOCK_HEADER + "".join(
        "\n" + pin.lock_block() for pin in sorted(pins, key=lambda p: p.source)
    )
    return Rendered(config, lock, owners, user_data)
