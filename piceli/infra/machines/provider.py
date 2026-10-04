"""The provider contract: what a cloud provider renders for each declared object.

A :class:`Provider` turns :class:`~piceli.infra.machines.model.Server`,
:class:`~piceli.infra.machines.model.PrimaryIp`,
:class:`~piceli.infra.machines.model.Firewall` and
:class:`~piceli.infra.machines.model.DnsRecord` into OpenTofu resources (the
JSON configuration syntax), names its pinned OpenTofu provider (version and
lock-file hashes) and the environment variable its token goes into. Adding a
provider (OVH, Infomaniak) is one subclass; nothing else changes.

Rendering is pure: no network, no files, no time. Only :meth:`Provider.prices`
may contact the provider's API, and only when a command asks for prices.

Importing this module is side-effect free.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from piceli.infra.machines.model import DnsRecord, Firewall, PrimaryIp, Server

__all__ = ["Pin", "Prices", "Provider", "Resource"]


@dataclass(frozen=True)
class Pin:
    """An OpenTofu provider pinned by version and lock-file hashes."""

    local_name: str
    source: str
    version: str
    hashes: tuple[str, ...]

    def lock_block(self) -> str:
        """The provider's block of ``.terraform.lock.hcl``."""
        lines = [
            f'provider "{self.source}" {{',
            f'  version     = "{self.version}"',
            f'  constraints = "{self.version}"',
            "  hashes = [",
            *[f'    "{item}",' for item in sorted(self.hashes)],
            "  ]",
            "}",
        ]
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Resource:
    """One OpenTofu resource: ``type.name`` and its body."""

    type: str
    name: str
    body: Mapping[str, Any]

    @property
    def address(self) -> str:
        return f"{self.type}.{self.name}"

    def ref(self, attribute: str) -> str:
        """``${type.name.attribute}``: a reference in the JSON syntax."""
        return f"${{{self.type}.{self.name}.{attribute}}}"


@dataclass(frozen=True)
class Prices:
    """Monthly prices (net) from the provider: per server type and per primary IP."""

    currency: str
    servers: Mapping[tuple[str, str], float]
    primary_ips: Mapping[tuple[str, str], float]

    def server(self, type_: str, location: str) -> float | None:
        return self.servers.get((type_, location))

    def primary_ip(self, kind: str, location: str) -> float | None:
        return self.primary_ips.get((kind, location))


class Provider(ABC):
    """A cloud provider Piceli can render servers, IPs, firewalls and records for.

    Subclasses are frozen dataclasses (compared by value) with at least
    ``credentials`` (the local credential holding the API token, ``piceli
    secrets provider NAME``) and ``location`` (the default location).
    """

    #: The provider's name in plans, status and labels (``hetzner``).
    name: str = ""
    credentials: str | None
    location: str | None

    # ------------------------------------------------------------ pinning
    def pin(self) -> Pin | None:
        """The pinned OpenTofu provider (``None`` for built-in resources)."""
        return None

    def token_env(self) -> str | None:
        """The environment variable the OpenTofu provider reads its token from."""
        return None

    def provider_block(self) -> dict[str, Any] | None:
        """The ``provider "<local name>"`` body (never the token)."""
        return None

    def describe(self) -> dict[str, Any]:
        pin = self.pin()
        return {
            "name": self.name,
            "source": pin.source if pin else None,
            "version": pin.version if pin else None,
            "credentials": self.credentials,
            "location": self.location,
        }

    # ------------------------------------------------------------ checks
    def check_server(self, server: Server) -> None:  # noqa: B027  optional hook
        """Refuse what this provider cannot create (``infra-invalid``)."""

    # ------------------------------------------------------------ rendering
    @abstractmethod
    def render_primary_ip(
        self, ip: PrimaryIp, server: Server, labels: Mapping[str, str]
    ) -> Resource: ...

    @abstractmethod
    def render_firewall(
        self, firewall: Firewall, labels: Mapping[str, str]
    ) -> list[Resource]: ...

    @abstractmethod
    def render_server(
        self,
        server: Server,
        *,
        ips: Mapping[str, Resource],
        firewall: Resource | None,
        labels: Mapping[str, str],
        ssh_keys: list[Resource],
    ) -> Resource: ...

    def render_ssh_key(
        self, name: str, digest: str, public_key: str, labels: Mapping[str, str]
    ) -> Resource | None:
        """A public key resource named ``name`` at the provider (``None``: none needed)."""
        return None

    @abstractmethod
    def render_record(
        self, record: DnsRecord, values: list[str], labels: Mapping[str, str]
    ) -> Resource: ...

    @abstractmethod
    def address(self, server: Resource, family: str) -> str:
        """A reference to the server's ``ipv4`` or ``ipv6`` address."""

    @abstractmethod
    def server_id(self, server: Resource) -> str:
        """A reference to the server's id at the provider."""

    def server_status(self, server: Resource) -> str | None:
        """A reference to the server's status, if the provider has one."""
        return None

    # ------------------------------------------------------------ state
    @abstractmethod
    def labels_of(self, values: Mapping[str, Any]) -> Mapping[str, str]:
        """The labels of a resource as OpenTofu's state or plan shows it."""

    # ------------------------------------------------------------ prices
    def prices(self, token: str | None) -> Prices | None:
        """Monthly prices from the provider (``None``: not available)."""
        return None
