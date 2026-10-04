"""Hetzner Cloud: servers, primary IPs, firewalls and DNS record sets.

Rendered for the ``hetznercloud/hcloud`` OpenTofu provider, pinned to one
version and its lock-file hashes. The API token never appears in the
configuration: OpenTofu reads it from ``HCLOUD_TOKEN`` in its environment,
which Piceli fills from the local credential ``Hetzner(credentials=)``
(``piceli secrets provider NAME --prompt``).

DNS records go into zones the owner already has in the Hetzner Console
(``hcloud_zone_rrset``); Piceli never creates or deletes a zone.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from piceli.infra.machines.model import (
    DnsRecord,
    Firewall,
    InfraError,
    PrimaryIp,
    Server,
)
from piceli.infra.machines.provider import Pin, Prices, Provider, Resource

__all__ = ["DEFAULT_ENDPOINT", "HCLOUD_PIN", "Hetzner"]

DEFAULT_ENDPOINT = "https://api.hetzner.cloud/v1"
#: hetznercloud/hcloud, as ``tofu init`` locked it (every platform's hashes).
HCLOUD_PIN = Pin(
    local_name="hcloud",
    source="registry.opentofu.org/hetznercloud/hcloud",
    version="1.69.0",
    hashes=(
        "h1:/pWxd5Osefkg7cjjoocgUGsgV+ogMNR5Bj8NSUDLXUU=",
        "h1:3LoUpRjqu3O/vSVTdHPQSMXvGSAA9g4O3emY8qhQPpw=",
        "h1:3eLWWMWbF/q/UYAvhFbRfL6RwAs9cACPvi0N4W+vi14=",
        "h1:H9WTbWGVkHW9oixJWfNOD9E6TdDVRwmVjFiHDqfe6hk=",
        "h1:HGJANuVqrAGQiSN/m6DODhf05D7rSugUpriG2bPhBuM=",
        "h1:MLxpeLlRL97wDLqL+WNpb7L9Vk+g8aUHLjXspFPR+tg=",
        "h1:PWhmd5g+OPBPdwQoFkG+KFmppYSR6ujPof+zwCjfMPI=",
        "h1:XuuF9gGyXrmeINI2/SdgfoaJntzP231hsM+MSbaQ5QM=",
        "h1:e4gDEHjE7p660CTFwnkWS3ibVNV54nT7e1TliOJ0cqw=",
        "h1:owcgdkfmWRbaGgiIl2HY5AFQdFENckbPcn91NJpr1r4=",
        "h1:sRkgVfXIDHNIyGR/kOVjIa45/sxH2JzCHNzDQlKgdF4=",
        "h1:xxSPOhL3DApttfBxP8GeH91tf1ywGw56NeNqpkD2P2k=",
        "h1:z7i3QamSF7nU+9dj+5+g7JTRwtos+kAfkwQpZjbx+/Q=",
        "zh:023c0580c46b48a1dce8aeb5832ec9896ce53ee019a2b296d854873f92aad7d9",
        "zh:02fa14eebb1c48b52b9acb33fb987425513537b70a50dac5639aecfe62c4b66f",
        "zh:0f534ee078e5f0f3c21e99cc020c3a9be565fceed4936d88b978f7b7d4b981fc",
        "zh:42dcf6859d097179c30cbe346931be731548a51ef7c73d08cd4af69a044bd60f",
        "zh:6d319ee6c9e64437634f3e682196dab24025ea91a7332b40972279e9c6e67f64",
        "zh:71a1dc59a47ab1b4127696e8047b54bb4bc8f92b8994ecc13509eaeb7e13a729",
        "zh:781310b68c74dede2e225096d7ada9a0b4b9252b6cafa4dc1ff064dd9ea4c817",
        "zh:7c4e0411e6e10ca6bd49c8e699c7c61cd9750d32d46ee5472d01faa30fbd222f",
        "zh:c4c70587a7632aea65d6b1bb0bbe8da2145a7acb6d16a7dd0e0dbbde6195c8ca",
        "zh:caaab2e9bb7cf8bb478b143bc0156c40729d720d3e2c77cf021e515fbeca3a6a",
        "zh:cbd1c2040111b595cfa5daa201166560b4988eb0227dd6be17e8ae72a5ff1df0",
        "zh:eba595371b98a66c1bf64dfb9f9256e6dc365e990e7202c50b6977fb3d24f822",
        "zh:fd477dca1d5b0bc7f41cce6b9f3e3250b58597cc178a70fa3136717343ffeede",
    ),
)
_ENDPOINT = re.compile(r"https?://[^\s?#@]+")
#: Server types Piceli has been tested with (one is enough to start with).
#: Another type is refused unless ``Hetzner(server_types=...)`` lists it.
SERVER_TYPES = ("cax11",)


@dataclass(frozen=True)
class Hetzner(Provider):
    """Hetzner Cloud.

    :param credentials: The local credential holding the API token (``piceli
        secrets provider NAME --prompt``).
    :param location: Default location of servers (``fsn1``, ``nbg1``, ``hel1``...).
    :param endpoint: The API (a mock API in tests).
    :param server_types: Server types accepted (default: ``cax11``).
    :param poll_interval: How often the OpenTofu provider polls actions
        (``"500ms"``; default the provider's).
    """

    credentials: str | None = None
    location: str | None = None
    endpoint: str = DEFAULT_ENDPOINT
    server_types: tuple[str, ...] = SERVER_TYPES
    poll_interval: str | None = None

    name = "hetzner"

    def __post_init__(self) -> None:
        from piceli.profiles import ProfileError, check_name

        if self.credentials is None:
            raise InfraError(
                "infra-invalid", "Hetzner(credentials=) names the token's credential"
            )
        try:
            check_name(self.credentials)
        except ProfileError:
            raise InfraError(
                "infra-invalid", "Hetzner(credentials=) must be a credential name"
            ) from None
        if not isinstance(self.endpoint, str) or not _ENDPOINT.fullmatch(self.endpoint):
            raise InfraError("infra-invalid", "Hetzner(endpoint=) is an http(s) URL")
        object.__setattr__(self, "endpoint", self.endpoint.rstrip("/"))
        if isinstance(self.server_types, str):
            raise InfraError("infra-invalid", "Hetzner(server_types=) is a list")
        object.__setattr__(self, "server_types", tuple(sorted(set(self.server_types))))
        if self.poll_interval is not None and not re.fullmatch(
            r"\d+(ms|s)", self.poll_interval
        ):
            raise InfraError(
                "infra-invalid", "Hetzner(poll_interval=) is like 500ms or 1s"
            )

    # ------------------------------------------------------------ pinning
    def pin(self) -> Pin:
        return HCLOUD_PIN

    def token_env(self) -> str:
        return "HCLOUD_TOKEN"

    def provider_block(self) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if self.endpoint != DEFAULT_ENDPOINT:
            body["endpoint"] = self.endpoint
        if self.poll_interval is not None:
            body["poll_interval"] = self.poll_interval
        return body

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "endpoint": self.endpoint}

    def check_server(self, server: Server) -> None:
        if server.type not in self.server_types:
            raise InfraError(
                "infra-invalid",
                f"Server {server.name}: type {server.type!r} is not one of "
                f"{', '.join(self.server_types)} (Hetzner(server_types=...) adds one)",
            )

    # ------------------------------------------------------------ rendering
    def render_primary_ip(
        self, ip: PrimaryIp, server: Server, labels: Mapping[str, str]
    ) -> Resource:
        return Resource(
            "hcloud_primary_ip",
            f"ip-{ip.name}",
            {
                "name": ip.name,
                "type": ip.kind,
                "location": server.location,
                "auto_delete": False,
                "delete_protection": ip.protect,
                "labels": dict(labels),
            },
        )

    def render_firewall(
        self, firewall: Firewall, labels: Mapping[str, str]
    ) -> list[Resource]:
        rules = []
        for rule in firewall.rules:
            body: dict[str, Any] = {
                "direction": "in",
                "protocol": rule.protocol,
                "source_ips": list(rule.sources),
            }
            if rule.port_text is not None:
                body["port"] = rule.port_text
            if rule.name is not None:
                body["description"] = rule.name
            rules.append(body)
        rules.sort(key=lambda item: json.dumps(item, sort_keys=True))
        return [
            Resource(
                "hcloud_firewall",
                f"firewall-{firewall.name}",
                {"name": firewall.name, "labels": dict(labels), "rule": rules},
            )
        ]

    def render_ssh_key(
        self, name: str, digest: str, public_key: str, labels: Mapping[str, str]
    ) -> Resource:
        return Resource(
            "hcloud_ssh_key",
            f"key-{digest}",
            {"name": name, "public_key": public_key, "labels": dict(labels)},
        )

    def render_server(
        self,
        server: Server,
        *,
        ips: Mapping[str, Resource],
        firewall: Resource | None,
        labels: Mapping[str, str],
        ssh_keys: list[Resource],
    ) -> Resource:
        public: dict[str, Any] = {
            "ipv4_enabled": server.ipv4 is not False,
            "ipv6_enabled": server.ipv6 is not False,
        }
        for family in ("ipv4", "ipv6"):
            value = getattr(server, family)
            if isinstance(value, PrimaryIp):
                public[family] = ips[value.name].ref("id")
        body: dict[str, Any] = {
            "name": server.name,
            "server_type": server.type,
            "image": server.image,
            "location": server.location,
            "labels": dict(labels),
            "public_net": [public],
        }
        if firewall is not None:
            body["firewall_ids"] = [firewall.ref("id")]
        if ssh_keys:
            body["ssh_keys"] = [key.ref("id") for key in ssh_keys]
        return Resource("hcloud_server", f"server-{server.name}", body)

    def render_record(
        self, record: DnsRecord, values: list[str], labels: Mapping[str, str]
    ) -> Resource:
        body: dict[str, Any] = {
            "zone": record.zone,
            "name": record.name,
            "type": record.type,
            "ttl": record.ttl,
            "records": [{"value": value} for value in values],
            "labels": dict(labels),
        }
        return Resource("hcloud_zone_rrset", record.resource_name, body)

    def address(self, server: Resource, family: str) -> str:
        return server.ref(f"{family}_address")

    def server_id(self, server: Resource) -> str:
        return server.ref("id")

    def server_status(self, server: Resource) -> str:
        return server.ref("status")

    def labels_of(self, values: Mapping[str, Any]) -> Mapping[str, str]:
        labels = values.get("labels")
        return labels if isinstance(labels, Mapping) else {}

    # ------------------------------------------------------------ prices
    def prices(self, token: str | None) -> Prices | None:
        """``GET /pricing``: monthly net prices per server type and primary IP."""
        if not token:
            return None
        request = urllib.request.Request(
            f"{self.endpoint}/pricing",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = json.loads(response.read(4 * 1024 * 1024))
        except (OSError, ValueError, urllib.error.URLError):
            return None
        return parse_prices(body)


def parse_prices(body: Any) -> Prices | None:
    """The monthly net prices of a ``GET /pricing`` answer (``None`` if malformed)."""
    pricing = body.get("pricing") if isinstance(body, dict) else None
    if not isinstance(pricing, dict):
        return None
    servers: dict[tuple[str, str], float] = {}
    ips: dict[tuple[str, str], float] = {}

    def monthly(entry: Any) -> float | None:
        try:
            return float(entry["price_monthly"]["net"])
        except (KeyError, TypeError, ValueError):
            return None

    for item in pricing.get("server_types") or ():
        if not isinstance(item, dict):
            continue
        for price in item.get("prices") or ():
            value = monthly(price)
            if value is not None and isinstance(price.get("location"), str):
                servers[(str(item.get("name")), price["location"])] = value
    for item in pricing.get("primary_ips") or ():
        if not isinstance(item, dict):
            continue
        for price in item.get("prices") or ():
            value = monthly(price)
            if value is not None and isinstance(price.get("location"), str):
                ips[(str(item.get("type")), price["location"])] = value
    return Prices(str(pricing.get("currency") or "EUR"), servers, ips)
