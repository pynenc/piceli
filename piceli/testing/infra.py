"""Test helpers for ``piceli infra``: a provider that creates nothing, a mock Hetzner API.

:class:`FakeProvider` renders every server, IP, firewall and record as
OpenTofu's built-in ``terraform_data`` resource: a real ``tofu plan`` and
``tofu apply`` run, the state is real (and encrypted), but no provider is
downloaded and no network is used. Its servers get the addresses the test
declares (``addresses={"edge-1": "127.0.0.1"}``).

:class:`MockHetznerApi` serves ``GET /v1/pricing`` (and records requests)
on 127.0.0.1, for the price estimates of :class:`~piceli.infra.Hetzner`.

Importing this module is side-effect free; the mock API starts only inside
its ``with`` block.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from piceli.infra.machines.model import DnsRecord, Firewall, PrimaryIp, Server
from piceli.infra.machines.provider import Prices, Provider, Resource

__all__ = ["PRICING", "FakeProvider", "MockHetznerApi", "mock_hetzner_api"]


@dataclass(frozen=True)
class FakeProvider(Provider):
    """Servers as ``terraform_data``: real OpenTofu, nothing created anywhere.

    :param addresses: IPv4 per server name (default ``192.0.2.10``).
    :param addresses6: IPv6 per server name (default ``2001:db8::10``).
    :param monthly: Monthly price per server type (for estimates).
    """

    credentials: str | None = None
    location: str | None = "lab"
    addresses: Mapping[str, str] = field(default_factory=dict)
    addresses6: Mapping[str, str] = field(default_factory=dict)
    monthly: Mapping[str, float] = field(default_factory=dict)

    name = "fake"

    def __hash__(self) -> int:
        return hash(("FakeProvider", self.location))

    def render_primary_ip(
        self, ip: PrimaryIp, server: Server, labels: Mapping[str, str]
    ) -> Resource:
        address = self._address(server, ip.kind)
        return Resource(
            "terraform_data",
            f"ip-{ip.name}",
            {"input": {"kind": "primary-ip", "name": ip.name, "address": address,
                       "labels": dict(labels)}},
        )  # fmt: skip

    def render_firewall(
        self, firewall: Firewall, labels: Mapping[str, str]
    ) -> list[Resource]:
        return [
            Resource(
                "terraform_data",
                f"firewall-{firewall.name}",
                {
                    "input": {
                        "kind": "firewall",
                        "name": firewall.name,
                        "rules": [rule.describe() for rule in firewall.rules],
                        "labels": dict(labels),
                    }
                },
            )
        ]

    def render_server(
        self,
        server: Server,
        *,
        ips: Mapping[str, Resource],
        firewall: Resource | None,
        labels: Mapping[str, str],
        ssh_keys: list[Resource],
    ) -> Resource:
        body: dict[str, Any] = {
            "kind": "server",
            "name": server.name,
            "type": server.type,
            "image": server.image,
            "location": server.location,
            "labels": dict(labels),
            "status": "running",
            "ssh_keys": list(server.ssh_keys),
        }
        for family in ("ipv4", "ipv6"):
            value = getattr(server, family)
            if isinstance(value, PrimaryIp):
                body[family] = ips[value.name].ref("output.address")
            elif value:
                body[family] = self._address(server, family)
        if firewall is not None:
            body["firewall"] = firewall.ref("id")
        return Resource("terraform_data", f"server-{server.name}", {"input": body})

    def render_record(
        self, record: DnsRecord, values: list[str], labels: Mapping[str, str]
    ) -> Resource:
        return Resource(
            "terraform_data",
            record.resource_name,
            {"input": {"kind": "record", "zone": record.zone, "name": record.name,
                       "type": record.type, "ttl": record.ttl, "records": values,
                       "labels": dict(labels)}},
        )  # fmt: skip

    def address(self, server: Resource, family: str) -> str:
        return server.ref(f"output.{family}")

    def server_id(self, server: Resource) -> str:
        return server.ref("id")

    def server_status(self, server: Resource) -> str:
        return server.ref("output.status")

    def labels_of(self, values: Mapping[str, Any]) -> Mapping[str, str]:
        found = values.get("input")
        labels = found.get("labels") if isinstance(found, Mapping) else None
        return labels if isinstance(labels, Mapping) else {}

    def prices(self, token: str | None) -> Prices | None:
        if not self.monthly:
            return None
        location = self.location or "lab"
        return Prices(
            "EUR",
            {(name, location): value for name, value in self.monthly.items()},
            {},
        )

    def _address(self, server: Server, family: str) -> str:
        if family == "ipv4":
            return self.addresses.get(server.name, "192.0.2.10")
        return self.addresses6.get(server.name, "2001:db8::10")


#: A ``GET /v1/pricing`` answer in the shape of Hetzner's API (made-up prices).
PRICING: dict[str, Any] = {
    "pricing": {
        "currency": "EUR",
        "vat_rate": "19.000000",
        "server_types": [
            {
                "id": 45,
                "name": "cax11",
                "prices": [
                    {
                        "location": location,
                        "price_hourly": {"net": "0.0060", "gross": "0.0071"},
                        "price_monthly": {"net": "3.7900", "gross": "4.5101"},
                    }
                    for location in ("fsn1", "nbg1", "hel1")
                ],
            }
        ],
        "primary_ips": [
            {
                "type": "ipv4",
                "prices": [
                    {
                        "location": location,
                        "price_hourly": {"net": "0.0008", "gross": "0.0010"},
                        "price_monthly": {"net": "0.5000", "gross": "0.5950"},
                    }
                    for location in ("fsn1", "nbg1", "hel1")
                ],
            },
            {
                "type": "ipv6",
                "prices": [
                    {
                        "location": location,
                        "price_hourly": {"net": "0.0000", "gross": "0.0000"},
                        "price_monthly": {"net": "0.0000", "gross": "0.0000"},
                    }
                    for location in ("fsn1", "nbg1", "hel1")
                ],
            },
        ],
    }
}


@dataclass
class MockHetznerApi:
    """A loopback HTTP server answering like Hetzner's API (``/v1/pricing``).

    ``token`` is the only accepted bearer token; ``requests`` records
    ``(method, path, authorized)`` of every request (never the token).
    """

    token: str
    url: str = ""
    requests: list[tuple[str, str, bool]] = field(default_factory=list)
    pricing: dict[str, Any] = field(default_factory=lambda: PRICING)


def _error(kind: str, message: str) -> dict[str, Any]:
    """An error body as Hetzner's API sends it."""
    return {"error": {"code": kind, "message": message}}


@contextmanager
def mock_hetzner_api(token: str) -> Iterator[MockHetznerApi]:
    """Serve a :class:`MockHetznerApi` on 127.0.0.1 while the block runs."""
    api = MockHetznerApi(token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # quiet
            return

        def do_GET(self) -> None:
            authorized = self.headers.get("Authorization") == f"Bearer {api.token}"
            api.requests.append(("GET", self.path, authorized))
            if not authorized:
                self._send(401, _error("unauthorized", "unable to authenticate"))
            elif self.path.split("?")[0] == "/v1/pricing":
                self._send(200, api.pricing)
            else:
                self._send(404, _error("not_found", "not found"))

        def _send(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    api.url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield api
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
