"""The typed declaration of machines: servers, fixed IPs, firewalls, DNS records.

Every class is a frozen dataclass checked when it is built, so a wrong
declaration fails at import with ``infra-invalid`` and never halfway through
a plan. Values are normalised (lists become tuples, CIDRs their canonical
text) so that :func:`piceli.infra.machines.render.render` is deterministic.

Importing this module is side-effect free.
"""

from __future__ import annotations

import ipaddress
import re
import string
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from piceli.infra import Cluster
    from piceli.infra.machines.provider import Provider
    from piceli.infra.machines.state import StateBackend

__all__ = [
    "DnsRecord",
    "Firewall",
    "Hook",
    "InfraError",
    "Infrastructure",
    "PrimaryIp",
    "Rule",
    "Server",
    "Ssh",
]

#: A name Piceli gives a resource: a DNS label (also an OpenTofu identifier
#: once it starts with a letter).
_NAME = re.compile(r"[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?")
_ZONE = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
)
_RECORD_NAME = re.compile(
    r"@|(?:\*\.)?(?:[a-z0-9_](?:[-a-z0-9_]{0,61}[a-z0-9])?)(?:\.[a-z0-9_](?:[-a-z0-9_]{0,61}[a-z0-9])?)*|\*"
)
_LABEL_KEY = re.compile(
    r"(?:[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?/)?[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?"
)
_LABEL_VALUE = re.compile(r"(?:[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?")
_TOKEN = re.compile(r"[A-Za-z0-9][-A-Za-z0-9_.]{0,127}")
_HOST = re.compile(r"[A-Za-z0-9](?:[-A-Za-z0-9.]{0,251}[A-Za-z0-9])?")
_USER = re.compile(r"[a-z_][-a-z0-9_]{0,31}")
#: Placeholders a :class:`Hook` may use, filled from the server's outputs.
HOOK_FIELDS = ("name", "id", "ipv4", "ipv6", "address")
RECORD_TYPES = ("A", "AAAA", "CNAME", "TXT")
#: Labels on every resource Piceli renders (ownership; destroy checks them).
MANAGED_BY = "piceli.io/managed-by"
INFRA_LABEL = "piceli.io/infra"
RESOURCE_LABEL = "piceli.io/resource"


class InfraError(ValueError):
    """A machine declaration or ``piceli infra`` command refused, with a code."""

    def __init__(self, code: str, message: str, details: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        #: Redacted diagnostics (OpenTofu's errors) for stderr, never stdout.
        self.details = list(details)


def _invalid(message: str) -> InfraError:
    return InfraError("infra-invalid", message)


def _name(value: object, what: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise _invalid(
            f"{what} must be a DNS label starting with a letter (like edge-1), got {value!r}"
        )
    return value


def _tuple(value: object, what: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise _invalid(f"{what} must be a list")
    return tuple(value)


def _labels(value: object, what: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise _invalid(f"{what} must be a mapping of strings")
    found: dict[str, str] = {}
    for key, item in sorted(value.items()):
        if (
            not isinstance(key, str)
            or not isinstance(item, str)
            or not _LABEL_KEY.fullmatch(key)
            or not _LABEL_VALUE.fullmatch(item)
        ):
            raise _invalid(f"{what}: {key!r} is not a valid label")
        if key.startswith("piceli.io/"):
            raise _invalid(f"{what}: piceli.io/ labels are Piceli's own")
        found[key] = item
    return found


def _port(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise _invalid(f"{what} must be a port in 1..65535")
    return value


@dataclass(frozen=True)
class Rule:
    """One inbound firewall rule: a protocol, a port or range, the allowed sources.

    Build it with :meth:`tcp`, :meth:`udp` or :meth:`icmp`. Sources default to
    every address (``0.0.0.0/0`` and ``::/0``); everything not allowed by a
    rule is dropped. Outbound traffic is not filtered.
    """

    protocol: Literal["tcp", "udp", "icmp"]
    port: int | tuple[int, int] | None = None
    sources: tuple[str, ...] = ("0.0.0.0/0", "::/0")
    name: str | None = None

    def __post_init__(self) -> None:
        if self.protocol not in ("tcp", "udp", "icmp"):
            raise _invalid("Rule protocol must be tcp, udp or icmp")
        if self.protocol == "icmp":
            if self.port is not None:
                raise _invalid("an icmp Rule has no port")
        elif isinstance(self.port, tuple):
            low, high = (_port(p, "Rule port") for p in self.port)
            if low >= high:
                raise _invalid("a Rule port range is (low, high) with low < high")
        else:
            _port(self.port, "Rule port")
        sources = _tuple(self.sources, "Rule(sources=)")
        if not sources:
            raise _invalid("a Rule needs at least one source")
        normal: list[str] = []
        for item in sources:
            try:
                normal.append(str(ipaddress.ip_network(item, strict=True)))
            except (TypeError, ValueError):
                raise _invalid(
                    f"Rule source {item!r} is not a CIDR (like 10.0.0.0/8)"
                ) from None
        object.__setattr__(self, "sources", tuple(sorted(set(normal))))
        if self.name is not None:
            _name(self.name, "Rule name")

    @classmethod
    def tcp(
        cls,
        port: int | tuple[int, int],
        *,
        sources: Sequence[str] = ("0.0.0.0/0", "::/0"),
        name: str | None = None,
    ) -> Rule:
        """Allow TCP to ``port`` (or the range ``(low, high)``) from ``sources``."""
        return cls("tcp", port, tuple(sources), name)

    @classmethod
    def udp(
        cls,
        port: int | tuple[int, int],
        *,
        sources: Sequence[str] = ("0.0.0.0/0", "::/0"),
        name: str | None = None,
    ) -> Rule:
        """Allow UDP to ``port`` (or the range ``(low, high)``) from ``sources``."""
        return cls("udp", port, tuple(sources), name)

    @classmethod
    def icmp(
        cls, *, sources: Sequence[str] = ("0.0.0.0/0", "::/0"), name: str | None = None
    ) -> Rule:
        """Allow ICMP (ping) from ``sources``."""
        return cls("icmp", None, tuple(sources), name)

    @property
    def port_text(self) -> str | None:
        """``"443"`` or ``"1000-2000"``; ``None`` for icmp."""
        if self.port is None:
            return None
        if isinstance(self.port, tuple):
            return f"{self.port[0]}-{self.port[1]}"
        return str(self.port)

    def describe(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "port": self.port_text,
            "sources": list(self.sources),
            "name": self.name,
        }


@dataclass(frozen=True)
class Firewall:
    """A named set of inbound :class:`Rule` s, attached to the servers that list it."""

    name: str
    rules: tuple[Rule, ...] = ()

    def __post_init__(self) -> None:
        _name(self.name, "Firewall name")
        rules = _tuple(self.rules, "Firewall(rules=)")
        if not all(isinstance(rule, Rule) for rule in rules):
            raise _invalid(f"Firewall {self.name}: rules are Rule objects")
        if len(set(rules)) != len(rules):
            raise _invalid(f"Firewall {self.name}: a rule is listed twice")
        object.__setattr__(self, "rules", rules)

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "rules": [rule.describe() for rule in self.rules]}


@dataclass(frozen=True)
class PrimaryIp:
    """A fixed address that outlives its server (``Server(ipv4=PrimaryIp(...))``).

    It is created in the server's location and assigned to it. Replacing
    the server keeps the address. ``protect=True`` turns on the provider's
    delete protection; ``piceli infra destroy`` then fails until it is
    turned off again.
    """

    name: str
    kind: Literal["ipv4", "ipv6"] = "ipv4"
    protect: bool = False

    def __post_init__(self) -> None:
        _name(self.name, "PrimaryIp name")
        if self.kind not in ("ipv4", "ipv6"):
            raise _invalid("PrimaryIp kind must be ipv4 or ipv6")
        if not isinstance(self.protect, bool):
            raise _invalid("PrimaryIp(protect=) is True or False")


@dataclass(frozen=True)
class Hook:
    """An external command that installs the server's operating system.

    Piceli does not own the OS. ``argv`` runs without a shell, from the
    working directory of ``piceli infra install``, after the owner approves
    its rendered form. Placeholders: ``{name}``, ``{id}``, ``{ipv4}``,
    ``{ipv6}`` and ``{address}`` (the SSH address). Example::

        Hook(["nixos-anywhere", "--flake", ".#edge-1", "root@{ipv4}"])
    """

    argv: tuple[str, ...]
    timeout: int = 3600

    def __post_init__(self) -> None:
        argv = _tuple(self.argv, "Hook(argv)")
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise _invalid("Hook argv is a non-empty list of non-empty strings")
        for part in argv:
            if "\0" in part or "\n" in part:
                raise _invalid("a Hook argument has no NUL or newline")
            try:
                fields = [
                    f for _, f, _, _ in string.Formatter().parse(part) if f is not None
                ]
            except ValueError:
                raise _invalid(
                    f"Hook argument {part!r} has unbalanced braces"
                ) from None
            for item in fields:
                if item not in HOOK_FIELDS:
                    raise _invalid(
                        f"Hook placeholder {{{item}}} is not one of "
                        + ", ".join(f"{{{f}}}" for f in HOOK_FIELDS)
                    )
        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, int)
            or not (1 <= self.timeout <= 24 * 3600)
        ):
            raise _invalid("Hook(timeout=) is seconds in 1..86400")
        object.__setattr__(self, "argv", argv)

    def render(self, values: Mapping[str, str]) -> list[str]:
        """``argv`` with its placeholders filled (missing values refused)."""
        rendered: list[str] = []
        for part in self.argv:
            try:
                rendered.append(part.format_map(dict(values)))
            except KeyError as error:
                raise InfraError(
                    "infra-install-unavailable",
                    f"the hook needs {{{error.args[0]}}}, which the server does not have",
                ) from None
        return rendered


@dataclass(frozen=True)
class Ssh:
    """How Piceli reaches a server over SSH (to fetch its k3s kubeconfig).

    ``address`` defaults to the server's IPv4; a private-network name (for
    example a VPN host name) works as well. The host key is scanned, shown
    and pinned by the approval of ``piceli infra register``.
    """

    user: str = "root"
    address: str | None = None
    port: int = 22
    kubeconfig_path: str = "/etc/rancher/k3s/k3s.yaml"

    def __post_init__(self) -> None:
        if not isinstance(self.user, str) or not _USER.fullmatch(self.user):
            raise _invalid("Ssh(user=) must be a user name")
        if self.address is not None:
            if not isinstance(self.address, str):
                raise _invalid("Ssh(address=) is a host name or an IP address")
            try:
                ipaddress.ip_address(self.address)
            except ValueError:
                if not _HOST.fullmatch(self.address):
                    raise _invalid(
                        "Ssh(address=) is a host name or an IP address"
                    ) from None
        _port(self.port, "Ssh(port=)")
        if (
            not isinstance(self.kubeconfig_path, str)
            or not self.kubeconfig_path.startswith("/")
            or any(c.isspace() or c in "'\"$`\\;&|<>" for c in self.kubeconfig_path)
        ):
            raise _invalid("Ssh(kubeconfig_path=) is an absolute path without spaces")


def _family(value: object, what: str) -> bool | PrimaryIp:
    if isinstance(value, (bool, PrimaryIp)):
        return value
    raise _invalid(f"{what} is True, False or a PrimaryIp")


@dataclass(frozen=True)
class Server:
    """One machine at a provider.

    :param name: The server's name at the provider (a DNS label).
    :param provider: The provider (for example ``Hetzner(...)``).
    :param type: The provider's server type (``cax11``).
    :param image: The image it boots first (the install hook may replace it).
    :param location: The provider's location (default: the provider's).
    :param ipv4: ``True`` (an address that goes with the server), ``False``,
        or a :class:`PrimaryIp` (a fixed address).
    :param ipv6: The same for IPv6.
    :param firewall: A :class:`Firewall`, or a list of :class:`Rule` s (a
        firewall named after the server), or ``None``.
    :param ssh_keys: Public keys put into the first boot image.
    :param install: The :class:`Hook` that installs the OS.
    :param ssh: How to reach it to fetch the k3s kubeconfig (:class:`Ssh`).
    :param cluster: The :class:`~piceli.infra.Cluster` its k3s becomes
        (``piceli infra register``).
    :param labels: Extra provider labels (``piceli.io/`` keys are Piceli's).
    """

    name: str
    provider: Provider
    type: str
    image: str
    location: str | None = None
    ipv4: bool | PrimaryIp = True
    ipv6: bool | PrimaryIp = True
    firewall: Firewall | Sequence[Rule] | None = None
    ssh_keys: tuple[str, ...] = ()
    install: Hook | None = None
    ssh: Ssh = field(default_factory=Ssh)
    cluster: Cluster | None = None
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from piceli.infra import Cluster
        from piceli.infra.machines.provider import Provider

        _name(self.name, "Server name")
        if not isinstance(self.provider, Provider):
            raise _invalid(
                f"Server {self.name}: provider must be a Provider (like Hetzner(...))"
            )
        for attribute in ("type", "image"):
            if not isinstance(getattr(self, attribute), str) or not _TOKEN.fullmatch(
                getattr(self, attribute)
            ):
                raise _invalid(
                    f"Server {self.name}: {attribute} must be a provider name like cax11"
                )
        location = (
            self.location if self.location is not None else self.provider.location
        )
        if (
            location is None
            or not isinstance(location, str)
            or not _TOKEN.fullmatch(location)
        ):
            raise _invalid(
                f"Server {self.name}: a location is required (Server or provider)"
            )
        object.__setattr__(self, "location", location)
        ipv4 = _family(self.ipv4, f"Server {self.name}: ipv4")
        ipv6 = _family(self.ipv6, f"Server {self.name}: ipv6")
        if isinstance(ipv4, PrimaryIp) and ipv4.kind != "ipv4":
            raise _invalid(f"Server {self.name}: ipv4= needs PrimaryIp(kind='ipv4')")
        if isinstance(ipv6, PrimaryIp) and ipv6.kind != "ipv6":
            raise _invalid(f"Server {self.name}: ipv6= needs PrimaryIp(kind='ipv6')")
        if ipv4 is False and ipv6 is False and self.ssh.address is None:
            raise _invalid(
                f"Server {self.name}: no public address and no Ssh(address=)"
            )
        firewall = self.firewall
        if firewall is not None and not isinstance(firewall, Firewall):
            rules = _tuple(firewall, f"Server {self.name}: firewall")
            if not all(isinstance(rule, Rule) for rule in rules):
                raise _invalid(
                    f"Server {self.name}: firewall is a Firewall or a list of Rule"
                )
            firewall = Firewall(self.name, rules)
        object.__setattr__(self, "firewall", firewall)
        keys = _tuple(self.ssh_keys, f"Server {self.name}: ssh_keys")
        for key in keys:
            parts = key.split() if isinstance(key, str) else []
            if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
                raise _invalid(
                    f"Server {self.name}: an ssh key is a public key line (ssh-ed25519 AAAA...)"
                )
        object.__setattr__(self, "ssh_keys", tuple(sorted(set(keys))))
        if self.install is not None and not isinstance(self.install, Hook):
            raise _invalid(f"Server {self.name}: install is a Hook")
        if not isinstance(self.ssh, Ssh):
            raise _invalid(f"Server {self.name}: ssh is an Ssh")
        if self.cluster is not None and not isinstance(self.cluster, Cluster):
            raise _invalid(f"Server {self.name}: cluster is a piceli.infra.Cluster")
        object.__setattr__(
            self, "labels", _labels(self.labels, f"Server {self.name}: labels")
        )
        self.provider.check_server(self)

    def __hash__(self) -> int:
        return hash(("Server", self.name))

    def primary_ips(self) -> tuple[PrimaryIp, ...]:
        return tuple(ip for ip in (self.ipv4, self.ipv6) if isinstance(ip, PrimaryIp))

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider.name,
            "type": self.type,
            "image": self.image,
            "location": self.location,
            "ipv4": self.ipv4.name if isinstance(self.ipv4, PrimaryIp) else self.ipv4,
            "ipv6": self.ipv6.name if isinstance(self.ipv6, PrimaryIp) else self.ipv6,
            "firewall": self.firewall.name
            if isinstance(self.firewall, Firewall)
            else None,
            "install": list(self.install.argv) if self.install is not None else None,
            "cluster": self.cluster.name if self.cluster is not None else None,
        }


@dataclass(frozen=True)
class DnsRecord:
    """One record set in a zone the owner already has at the provider.

    Give exactly one of ``value`` (a string or a list of strings), ``server``
    or ``servers`` (A takes their IPv4, AAAA their IPv6). Several servers
    give one record set with every address (round robin). Piceli manages the
    record set, never the zone. Health-checked failover is a later seam: it
    changes the answers of the same record set (see the infrastructure docs).
    """

    zone: str
    name: str
    type: Literal["A", "AAAA", "CNAME", "TXT"]
    value: str | Sequence[str] | None = None
    server: Server | None = None
    servers: Sequence[Server] = ()
    ttl: int = 300
    provider: Provider | None = None

    def __post_init__(self) -> None:
        from piceli.infra.machines.provider import Provider

        if not isinstance(self.zone, str) or not _ZONE.fullmatch(self.zone):
            raise _invalid(
                f"DnsRecord zone {self.zone!r} must be a domain like example.com"
            )
        if not isinstance(self.name, str) or not _RECORD_NAME.fullmatch(self.name):
            raise _invalid(
                f"DnsRecord name {self.name!r}: '@' for the apex, else a relative name"
            )
        if self.type not in RECORD_TYPES:
            raise _invalid(f"DnsRecord type must be one of {', '.join(RECORD_TYPES)}")
        servers = _tuple(self.servers, "DnsRecord(servers=)")
        if self.server is not None:
            servers = (self.server, *servers)
        if not all(isinstance(item, Server) for item in servers):
            raise _invalid("DnsRecord servers are Server objects")
        if len({item.name for item in servers}) != len(servers):
            raise _invalid("DnsRecord lists a server twice")
        object.__setattr__(self, "server", None)
        object.__setattr__(self, "servers", servers)
        value = self.value
        if value is not None:
            values = (
                (value,)
                if isinstance(value, str)
                else _tuple(value, "DnsRecord(value=)")
            )
            if not values or not all(
                isinstance(v, str) and v and len(v) <= 4096 for v in values
            ):
                raise _invalid(
                    "DnsRecord value is a non-empty string or list of strings"
                )
            object.__setattr__(self, "value", tuple(sorted(set(values))))
        if (value is None) == (not servers):
            raise _invalid(f"DnsRecord {self.key}: give value= or server(s)=, not both")
        if servers and self.type not in ("A", "AAAA"):
            raise _invalid(f"DnsRecord {self.key}: server= needs type A or AAAA")
        family = "ipv4" if self.type == "A" else "ipv6"
        for item in servers:
            if getattr(item, family) is False:
                raise _invalid(
                    f"DnsRecord {self.key}: server {item.name} has no {family}"
                )
        if isinstance(self.value, tuple) and self.type in ("A", "AAAA"):
            for text in self.value:
                try:
                    address = ipaddress.ip_address(text)
                except ValueError:
                    raise _invalid(
                        f"DnsRecord {self.key}: {text!r} is not an address"
                    ) from None
                if (address.version == 4) != (self.type == "A"):
                    raise _invalid(
                        f"DnsRecord {self.key}: {text!r} does not fit type {self.type}"
                    )
        if (
            isinstance(self.ttl, bool)
            or not isinstance(self.ttl, int)
            or not 60 <= self.ttl <= 86400
        ):
            raise _invalid(f"DnsRecord {self.key}: ttl is seconds in 60..86400")
        provider = self.provider
        if provider is None and servers:
            provider = servers[0].provider
        if not isinstance(provider, Provider):
            raise _invalid(f"DnsRecord {self.key}: provider= is required with value=")
        if any(item.provider != provider for item in servers):
            raise _invalid(f"DnsRecord {self.key}: its servers are at another provider")
        object.__setattr__(self, "provider", provider)

    @property
    def key(self) -> str:
        """``name.zone/TYPE`` (``@`` for the apex)."""
        return f"{self.name}.{self.zone}/{self.type}"

    @property
    def resource_name(self) -> str:
        """The OpenTofu resource name: ``record-<zone>-<name>-<type>`` as a label."""
        name = {"@": "apex", "*": "wildcard"}.get(self.name, self.name)
        text = f"record-{self.zone}-{name}-{self.type}".lower()
        return re.sub(r"[^a-z0-9-]+", "-", text.replace("*", "wildcard"))

    def describe(self) -> dict[str, Any]:
        return {
            "zone": self.zone,
            "name": self.name,
            "type": self.type,
            "ttl": self.ttl,
            "value": list(self.value) if isinstance(self.value, tuple) else None,
            "servers": [item.name for item in self.servers],
        }


@dataclass(frozen=True)
class Infrastructure:
    """Everything one OpenTofu state holds: servers and DNS records.

    :param name: Names the state (``piceli.io/infra=<name>`` on every resource).
    :param servers: The :class:`Server` s.
    :param records: The :class:`DnsRecord` s.
    :param state_dir: Where the encrypted state lives (default
        ``$XDG_STATE_HOME/piceli/infra/<name>``); never inside a Git work tree.
    :param state_key: The local credential holding the state passphrase
        (``piceli secrets state-key NAME``; default ``<name>-state``).
    :param backend: Where the encrypted state is stored: ``LocalState()``
        (default, the state directory) or ``HttpState(...)``.
    """

    name: str
    servers: tuple[Server, ...] = ()
    records: tuple[DnsRecord, ...] = ()
    state_dir: Path | None = None
    state_key: str | None = None
    backend: StateBackend | None = None

    def __post_init__(self) -> None:
        from piceli.infra.machines.state import LocalState, StateBackend
        from piceli.profiles import ProfileError, check_name

        _name(self.name, "Infrastructure name")
        servers = _tuple(self.servers, "Infrastructure(servers=)")
        records = _tuple(self.records, "Infrastructure(records=)")
        if not all(isinstance(item, Server) for item in servers):
            raise _invalid("Infrastructure servers are Server objects")
        if not all(isinstance(item, DnsRecord) for item in records):
            raise _invalid("Infrastructure records are DnsRecord objects")
        object.__setattr__(self, "servers", servers)
        object.__setattr__(self, "records", records)
        names = [item.name for item in servers]
        if len(set(names)) != len(names):
            raise _invalid("a server is declared twice")
        for record in records:
            for item in record.servers:
                if item.name not in names or self.server(item.name) != item:
                    raise _invalid(
                        f"DnsRecord {record.key}: server {item.name} is not in servers="
                    )
        keys = [record.key for record in records]
        if len(set(keys)) != len(keys):
            raise _invalid("a DNS record set is declared twice")
        firewalls: dict[str, Firewall] = {}
        ips: dict[str, str] = {}
        for item in servers:
            if isinstance(item.firewall, Firewall):
                seen = firewalls.setdefault(item.firewall.name, item.firewall)
                if seen != item.firewall:
                    raise _invalid(
                        f"two different firewalls are named {item.firewall.name}"
                    )
            for ip in item.primary_ips():
                if ips.setdefault(ip.name, item.name) != item.name:
                    raise _invalid(f"PrimaryIp {ip.name} is given to two servers")
        providers: dict[str, Provider] = {}
        for provider in [item.provider for item in servers] + [
            r.provider for r in records
        ]:
            assert provider is not None
            if providers.setdefault(provider.name, provider) != provider:
                raise _invalid(f"two different {provider.name} providers are declared")
        if self.state_dir is not None:
            if not isinstance(self.state_dir, (str, Path)):
                raise _invalid("Infrastructure(state_dir=) is a path")
            object.__setattr__(self, "state_dir", Path(self.state_dir).expanduser())
        key = self.state_key if self.state_key is not None else f"{self.name}-state"
        try:
            check_name(key)
        except ProfileError:
            raise _invalid(
                "Infrastructure(state_key=) must be a credential name"
            ) from None
        object.__setattr__(self, "state_key", key)
        backend = self.backend if self.backend is not None else LocalState()
        if not isinstance(backend, StateBackend):
            raise _invalid("Infrastructure(backend=) is LocalState() or HttpState(...)")
        object.__setattr__(self, "backend", backend)

    def server(self, name: str) -> Server | None:
        """The declared server ``name``, if any."""
        return next((item for item in self.servers if item.name == name), None)

    def providers(self) -> list[Provider]:
        """Each provider once, sorted by name."""
        found: dict[str, Provider] = {}
        for item in self.servers:
            found.setdefault(item.provider.name, item.provider)
        for record in self.records:
            assert record.provider is not None
            found.setdefault(record.provider.name, record.provider)
        return [found[name] for name in sorted(found)]

    def firewalls(self) -> list[Firewall]:
        """Each firewall once, sorted by name."""
        found: dict[str, Firewall] = {}
        for item in self.servers:
            if isinstance(item.firewall, Firewall):
                found.setdefault(item.firewall.name, item.firewall)
        return [found[name] for name in sorted(found)]

    def describe(self) -> dict[str, Any]:
        """The declaration as data (``piceli.infra.v1``); no credentials, no paths."""
        return {
            "schema": "piceli.infra.v1",
            "name": self.name,
            "servers": [item.describe() for item in self.servers],
            "firewalls": [item.describe() for item in self.firewalls()],
            "records": [item.describe() for item in self.records],
            "providers": [item.describe() for item in self.providers()],
            "backend": self.backend.describe() if self.backend is not None else None,
        }
