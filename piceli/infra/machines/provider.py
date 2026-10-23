"""The provider contract: what a cloud provider renders for each declared object.

A :class:`Provider` turns :class:`~piceli.infra.machines.model.Server`,
:class:`~piceli.infra.machines.model.PrimaryIp`,
:class:`~piceli.infra.machines.model.Firewall` and
:class:`~piceli.infra.machines.model.DnsRecord` into OpenTofu resources (the
JSON configuration syntax), names its pinned OpenTofu provider (version and
lock-file hashes) and the environment variables its credential goes into.
Adding a provider is one subclass; nothing else changes. A provider that
also uploads images or holds buckets says so (``supports_images``,
``supports_buckets``, ``supports_s3_keys``) and renders them.

Rendering is pure: no network, no files, no time. Only :meth:`Provider.prices`
(when a command asks for prices) and :meth:`Provider.plan_check` (after
``tofu plan``) may contact the provider's API.

Importing this module is side-effect free.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from piceli.infra.machines.model import (
        Bucket,
        DnsRecord,
        Firewall,
        Image,
        InfraError,
        PrimaryIp,
        S3Key,
        Server,
    )

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
    #: Whether a ``Server(image=Image(...))`` can be uploaded to this provider.
    supports_images: bool = False
    #: Whether ``Bucket(provider=...)`` can be created at this provider.
    supports_buckets: bool = False
    #: Whether this provider serves ``DnsRecord`` s.
    supports_dns: bool = True
    #: Whether ``Server(provider=...)`` can be created here (a DNS-only
    #: provider holds none).
    supports_servers: bool = True
    #: Whether ``Server(user_data=UserData(...))`` is passed at creation.
    supports_user_data: bool = False
    #: Whether ``Bucket(keys=[S3Key(...)])`` pairs are created here (0.25.0).
    supports_s3_keys: bool = False
    #: The TTL of a ``DnsRecord`` that gives none, and the shortest accepted.
    default_ttl: int = 300
    min_ttl: int = 60

    # ------------------------------------------------------------ pinning
    def pin(self) -> Pin | None:
        """The pinned OpenTofu provider (``None`` for built-in resources)."""
        return None

    def token_env(self) -> str | None:
        """The environment variable the OpenTofu provider reads its token from."""
        return None

    def credential_env(self, parts: Mapping[str, str]) -> dict[str, str]:
        """The environment OpenTofu gets from the stored credential's parts
        (:func:`~piceli.infra.machines.credential_parts.load_parts`: by key,
        such as ``{"token": ...}`` or ``{"api_key": ..., "secret_api_key":
        ...}``).

        Default: the one part in :meth:`token_env`. A provider whose
        credential has several parts (an id and a secret) maps each to its
        variable; a missing part is refused without echoing anything
        (``infra-credential-invalid``).
        """
        variable = self.token_env()
        if variable is None:
            return {}
        if len(parts) != 1:
            raise InfraError(
                "infra-credential-invalid",
                f"credential {self.credentials!r} is not one API token",
            )
        return {variable: next(iter(parts.values()))}

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

    def render_image(self, image: Image, labels: Mapping[str, str]) -> Resource:
        """The uploaded image (only when :attr:`supports_images`)."""
        raise NotImplementedError(f"{self.name} does not upload images")

    def render_bucket(self, bucket: Bucket, labels: Mapping[str, str]) -> Resource:
        """The bucket (only when :attr:`supports_buckets`)."""
        raise NotImplementedError(f"{self.name} has no buckets")

    def render_s3_key(
        self, key: S3Key, bucket: Resource, labels: Mapping[str, str]
    ) -> Resource:
        """An S3 key pair of ``bucket`` (only when :attr:`supports_s3_keys`).

        Its secret ends up in the (encrypted) state; :meth:`s3_key_values`
        reads it back from ``tofu show -json``.
        """
        raise NotImplementedError(f"{self.name} creates no S3 keys")

    def s3_key_values(self, values: Mapping[str, Any]) -> tuple[str, str] | None:
        """``(access key id, secret)`` of a key pair as the state shows it."""
        return None

    def s3_key_enforcement(self, key: S3Key) -> dict[str, Any]:
        """What limits a key pair, for the plan: ``read_only_enforced``
        (``True`` only when the provider refuses its writes) and ``how``."""
        return {"read_only_enforced": False, "how": "not known for this provider"}

    def hides_id(self, type_: str) -> bool:
        """Whether a resource's id is a credential (an access key id): never
        reported, also not after a partial apply."""
        return False

    def render_user_data(self, server: Resource, reference: str) -> Resource:
        """``server`` with its user data set to ``reference`` (a sensitive variable).

        Only when :attr:`supports_user_data`. The user data is used at
        creation only: the resource ignores later changes to it, so a plan
        that does not create the server needs no value.
        """
        raise NotImplementedError(f"{self.name} takes no user data")

    def data_sources(self) -> list[Resource]:
        """Read-only lookups the resources refer to (``data.<type>.<name>``)."""
        return []

    @abstractmethod
    def render_record(
        self, record: DnsRecord, values: list[str], labels: Mapping[str, str]
    ) -> Resource: ...

    def render_records(
        self, record: DnsRecord, values: list[str], labels: Mapping[str, str]
    ) -> list[Resource]:
        """The record's resources (default: one record set, :meth:`render_record`)."""
        return [self.render_record(record, values, labels)]

    @abstractmethod
    def address(self, server: Resource, family: str) -> str:
        """A reference to the server's ``ipv4`` or ``ipv6`` address."""

    def record_address(
        self,
        server: Server,
        resource: Resource,
        family: str,
        ips: Mapping[str, Resource],
    ) -> str:
        """The reference a DNS record (at any provider) answers with for ``server``.

        Default :meth:`address`; a provider whose fixed address outlives the
        server (``ips``: the server's rendered ``PrimaryIp`` s) can point at it.
        """
        return self.address(resource, family)

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

    def resource_labels(
        self, type_: str, values: Mapping[str, Any]
    ) -> Mapping[str, str]:
        """The labels of a resource of OpenTofu type ``type_`` (default :meth:`labels_of`)."""
        return self.labels_of(values)

    def ledger_owned(self, type_: str) -> bool:
        """Whether resources of ``type_`` carry no labels (the ledger alone owns them)."""
        return False

    def known_error(self, diagnostics: Sequence[str]) -> InfraError | None:
        """A refusal OpenTofu reported, as its registered code (``None``: not known)."""
        return None

    def plan_check(
        self,
        document: Mapping[str, Any],
        credential: str | None,
        *,
        ledger: Collection[str] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Check what the plan needs that OpenTofu cannot see, before approval.

        ``document`` is ``tofu show -json`` of the plan; ``credential`` the
        provider's stored value; ``ledger`` the addresses Piceli created
        (``None``: every resource in the state). Raise :class:`InfraError`
        to fail the plan; return ``{"check": NAME, "ran": bool, ...}`` (or
        a list of them) for the plan's ``checks`` (never a secret), or
        ``None`` when there is nothing to check.
        """
        return None

    # ------------------------------------------------------------ prices
    def prices(self, token: str | None) -> Prices | None:
        """Monthly prices from the provider (``None``: not available)."""
        return None
