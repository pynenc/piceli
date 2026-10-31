"""The typed declaration of a Nix host release (0.24.0).

A user writes, in the same module as the cache and the servers::

    from piceli.infra import Branch, HostCheck, HostReport, NixCache, NixHost, Tag

    web = NixHost(
        "web-1",
        server=web_server,                  # a piceli.infra.Server, or "ssh://root@web-1.example.net"
        flake=".#web-1",                    # nixosConfigurations.web-1
        follow=Tag("v*"),                   # or Branch("main")
        cache=cache,
        checks=[
            HostCheck.unit("nginx.service"),
            HostCheck.http("http://127.0.0.1/healthz", timeout=90),
            HostCheck.command("test", "-s", "/var/lib/app/ready"),
        ],
        report=HostReport("https://collector.example.net:4318"),
        age_identity="web-1-age",           # sops-nix's key, by `piceli host secrets`
    )

:func:`agent_config` is the JSON the host agent reads; nothing here reaches
the network or the host. Importing this module is side-effect free.
"""

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "CONFIG_SCHEMA",
    "STATE_DIR",
    "Branch",
    "HostCheck",
    "HostError",
    "HostReport",
    "NixHost",
    "SshTarget",
    "Tag",
    "agent_config",
]

CONFIG_SCHEMA = "piceli.nix-host-config.v1"
STATE_DIR = "/var/lib/piceli-host"
#: sops-nix's ``sops.age.keyFile`` default in its documentation.
AGE_KEY_FILE = "/var/lib/sops-nix/key.txt"
#: Where sops-nix decrypts ``sops.secrets."piceli/cache.env"``.
CACHE_ENV = "/run/secrets/piceli/cache.env"
#: Where ``piceli host secrets`` puts the read pair Piceli created (0.25.0).
PICELI_CACHE_ENV = "/var/lib/piceli-host/cache.env"
#: Defaults (proposals): a poll every 5 minutes, 60 s per check, 20 attempts kept.
POLL_SECONDS = 300
CHECK_TIMEOUT = 60
HISTORY = 20
#: System generations kept on the host (a rollback needs the previous one).
GENERATIONS = 5
_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_PATTERN = re.compile(r"[A-Za-z0-9._+*?\[\]/-]{1,100}")
_BRANCH = re.compile(r"[A-Za-z0-9_+-][A-Za-z0-9._+/-]{0,199}")
_ATTR = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,511}")
_UNIT = re.compile(r"[A-Za-z0-9:_.@-]{1,200}")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})")
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_PATH = re.compile(r"/[A-Za-z0-9._/@+-]{1,255}")


class HostError(ValueError):
    """A host declaration or command was refused (``code`` is registered)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> HostError:
    return HostError("host-invalid", message)


@dataclass(frozen=True)
class Tag:
    """Follow the highest version among the tags that match ``pattern``.

    Tags compare as semantic versions (an optional ``v``, then
    ``MAJOR.MINOR.PATCH``, an optional ``-prerelease`` and ``+build``) after
    the pattern's literal :attr:`prefix`: ``Tag("web-v*")`` orders
    ``web-v1.10.0`` above ``web-v1.9.0`` and never sees ``api-v2.0.0`` or
    ``v3.0.0``. A matching tag whose rest is not a version is ignored (the
    status lists it). Pre-releases count only with ``prereleases=True``; a
    tag with build metadata (``web-v1.2.3+test``) is a test tag and never
    counts.
    """

    pattern: str = "v*"
    prereleases: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.pattern, str) or not _PATTERN.fullmatch(self.pattern):
            raise _invalid("Tag(pattern=) is a glob of tag names (v*)")

    @property
    def prefix(self) -> str:
        """The literal part before the version (``web-v`` of ``web-v*``)."""
        from piceli.hosts.agent import tag_prefix

        return tag_prefix(self.pattern)

    def version(self, tag: str) -> tuple[Any, ...] | None:
        """``tag``'s sort key under this pattern, ``None`` when it does not
        match or the rest after :attr:`prefix` is no version (pre-releases
        included: :attr:`prereleases` is the follower's choice)."""
        from piceli.hosts.agent import tag_version

        return tag_version(self.pattern, tag)

    def describe(self) -> dict[str, Any]:
        return {"kind": "tag", "pattern": self.pattern, "prereleases": self.prereleases}


@dataclass(frozen=True)
class Branch:
    """Follow the build that was last published for a branch."""

    name: str = "main"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not _BRANCH.fullmatch(self.name)
            or ".." in self.name
        ):
            raise _invalid("Branch(name=) is a branch name")

    def describe(self) -> dict[str, Any]:
        return {"kind": "branch", "name": self.name}


def _check_error(message: str) -> HostError:
    return HostError("host-check-invalid", message)


@dataclass(frozen=True)
class HostCheck:
    """A check that must pass after a switch; tried every 2 s until ``timeout``.

    Build them with :meth:`unit`, :meth:`http` or :meth:`command`. With
    ``stable`` seconds, it must keep passing that long: a failed try within
    the window starts it again, so a unit that starts and dies a few seconds
    later fails the check and the switch is rolled back (0.44.0).
    """

    kind: Literal["unit", "http", "command"]
    target: tuple[str, ...]
    timeout: float = CHECK_TIMEOUT
    name: str | None = None
    stable: float = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", tuple(self.target))
        if not isinstance(self.timeout, (int, float)) or not 1 <= self.timeout <= 3600:
            raise _check_error("HostCheck(timeout=) is 1 to 3600 seconds")
        if (
            not isinstance(self.stable, (int, float))
            or not 0 <= self.stable < self.timeout
        ):
            raise _check_error("HostCheck(stable=) is 0 to less than timeout seconds")
        if self.kind == "unit":
            if len(self.target) != 1 or not _UNIT.fullmatch(self.target[0]):
                raise _check_error("HostCheck.unit() takes a systemd unit name")
        elif self.kind == "http":
            parsed = urllib.parse.urlsplit(self.target[0] if self.target else "")
            if (
                len(self.target) != 1
                or parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or "@" in parsed.netloc
            ):
                raise _check_error(
                    "HostCheck.http() takes an http(s) URL without credentials"
                )
        elif self.kind == "command":
            if not self.target or not all(
                isinstance(part, str) and part and "\0" not in part
                for part in self.target
            ):
                raise _check_error(
                    "HostCheck.command() takes the argv, one per argument"
                )
        else:
            raise _check_error("a HostCheck is unit, http or command")
        if self.name is not None and not _LABEL.fullmatch(self.name):
            raise _check_error("HostCheck(name=) is a DNS label")

    @classmethod
    def unit(
        cls,
        unit: str,
        *,
        timeout: float = CHECK_TIMEOUT,
        name: str | None = None,
        stable: float = 0,
    ) -> HostCheck:
        """The systemd unit is ``active`` (for ``stable`` seconds)."""
        return cls("unit", (unit,), timeout, name, stable)

    @classmethod
    def http(
        cls,
        url: str,
        *,
        timeout: float = CHECK_TIMEOUT,
        name: str | None = None,
        stable: float = 0,
    ) -> HostCheck:
        """A GET of ``url`` from the host answers 2xx (for ``stable`` seconds)."""
        return cls("http", (url,), timeout, name, stable)

    @classmethod
    def command(
        cls,
        *argv: str,
        timeout: float = CHECK_TIMEOUT,
        name: str | None = None,
        stable: float = 0,
    ) -> HostCheck:
        """The command exits 0 (run as root, with the new system's PATH; for
        ``stable`` seconds)."""
        return cls("command", tuple(argv), timeout, name, stable)

    def describe(self, index: int) -> dict[str, Any]:
        return {
            "name": self.name or f"{self.kind}-{index + 1}",
            "kind": self.kind,
            "target": list(self.target),
            "timeout": float(self.timeout),
            **({"stable": float(self.stable)} if self.stable else {}),
        }


@dataclass(frozen=True)
class HostReport:
    """Where the host agent reports its deploys: an OTLP/HTTP endpoint (logs).

    :param endpoint: ``https://collector:4318`` (``/v1/logs`` is appended)
        or the full ``.../v1/logs`` URL. Plain ``http://`` needs
        ``insecure=True`` unless it is a loopback address.
    :param headers_file: A root-only file on the host with ``Name: value``
        lines (``Authorization: Bearer ...``): a sops-nix secret, such as
        ``/run/secrets/piceli/otlp-headers``. The rendered module, committed
        to Git, never holds a header value.
    """

    endpoint: str
    headers_file: str | None = None
    insecure: bool = False

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(str(self.endpoint))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise _invalid("HostReport(endpoint=) is an http(s) URL")
        if "@" in parsed.netloc or parsed.query:
            raise _invalid("HostReport(endpoint=) carries no credentials or query")
        if parsed.scheme == "http" and not self.insecure:
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                loopback = parsed.hostname == "localhost"
            if not loopback:
                raise _invalid(
                    "HostReport: plain http:// to another machine needs insecure=True"
                )
        if self.headers_file is not None and not _PATH.fullmatch(self.headers_file):
            raise _invalid("HostReport(headers_file=) is an absolute path on the host")

    def describe(self) -> dict[str, Any]:
        endpoint = str(self.endpoint).rstrip("/")
        if not endpoint.endswith("/v1/logs"):
            endpoint += "/v1/logs"
        return {"endpoint": endpoint, "headers_file": self.headers_file}


@dataclass(frozen=True)
class SshTarget:
    """Where ``piceli host status`` reads the host's status file."""

    address: str
    user: str = "root"
    port: int = 22

    @classmethod
    def parse(cls, text: str) -> SshTarget:
        """``ssh://[user@]host[:port]``, ``user@host`` or ``host``."""
        raw = text[len("ssh://") :] if text.startswith("ssh://") else text
        user, sep, rest = raw.rpartition("@")
        host, port = rest, "22"
        if rest.startswith("["):
            host, _, tail = rest[1:].partition("]")
            port = tail[1:] if tail.startswith(":") else "22"
        elif rest.count(":") == 1:
            host, port = rest.split(":")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not _HOST.fullmatch(host):
                raise _invalid("an SSH target is ssh://[user@]host[:port]") from None
        if (sep and not _USER.fullmatch(user)) or not port.isdigit():
            raise _invalid("an SSH target is ssh://[user@]host[:port]")
        if not 0 < int(port) < 65536:
            raise _invalid("an SSH target's port is 1 to 65535")
        return cls(host, user if sep else "root", int(port))


def _attribute(flake: str) -> str:
    """``.#NAME`` (as ``nixos-rebuild --flake``) is the system of
    ``nixosConfigurations.NAME``; a longer ``.#ATTR`` is taken as given."""
    attr = flake[2:] if flake.startswith(".#") else flake
    if not _ATTR.fullmatch(attr) or ".." in attr:
        raise _invalid("NixHost(flake=) is .#NAME or .#ATTR of the source's flake")
    if "." not in attr:
        attr = f"nixosConfigurations.{attr}.config.system.build.toplevel"
    return f".#{attr}"


@dataclass(frozen=True)
class NixHost:
    """A NixOS host that follows a Git ref of its flake from a binary cache.

    :param name: The host's name (a DNS label).
    :param server: A :class:`~piceli.infra.Server` Piceli created, or an SSH
        target (``ssh://root@host:22``) for a host it did not.
    :param flake: The system it runs: ``.#NAME`` (the flake's
        ``nixosConfigurations.NAME``) or a full ``.#ATTR``. The agent
        switches only to a build of this attribute.
    :param follow: :class:`Tag` (``Tag("v*")``) or :class:`Branch`.
    :param cache: The :class:`~piceli.infra.NixCache` the builds were
        published to; the host only reads it.
    :param checks: :class:`HostCheck` s that must pass after the switch; a
        failure switches back to the previous system.
    :param report: Where the agent reports (:class:`HostReport`), or ``None``.
    :param build: The manifest name the builds were published under
        (``--name`` of ``piceli dev nix build``; default: ``NAME`` of
        ``.#NAME``, else the host's name).
    :param read_credentials_file: A root-only file on the host with the
        cache's read S3 keys (``AWS_ACCESS_KEY_ID=`` and
        ``AWS_SECRET_ACCESS_KEY=`` lines). Default (0.25.0) when the cache's
        bucket declares ``S3Key(access="read")``, or (0.25.3) the cache names
        a pair the owner made (``NixCache(read_credentials=)``):
        ``/var/lib/piceli-host/cache.env``, which ``piceli host secrets``
        writes from that pair (the agent reads it on each poll). Otherwise ``/run/secrets/piceli/cache.env``
        (``sops.secrets."piceli/cache.env"``, decrypted by sops-nix) for an
        ``s3://`` cache, none for a ``file://`` or ``https://`` one. A path
        given here is the host's own file: ``host secrets`` leaves it alone.
    :param poll_seconds: How often the agent looks for a new build.
    :param history: How many attempts the status keeps.
    :param generations: How many system generations the host keeps (at
        least 2: a rollback needs the previous one). After a deploy that
        passed its checks the agent deletes older ones (never the current,
        the previous or a held system) and collects the store; the boot
        menu's limit follows (``lib.mkDefault``).
    :param age_identity: The local credential with the host's age identity
        (``piceli secrets age NAME --generate``) that ``piceli host secrets``
        puts on the host for sops-nix; ``None``: the host gets its key
        another way.
    :param age_key_file: Where it goes: sops-nix's ``sops.age.keyFile``.
    :param gate: A :class:`~piceli.infra.Promotion` (0.25.0): the agent
        switches only to a build whose signed manifest carries the gate's
        verified ``pinned`` (input and environment); a ``NixRelease`` of
        this host takes it as its gate.
    """

    name: str
    server: Any
    flake: str
    follow: Tag | Branch
    cache: Any
    checks: Sequence[HostCheck] = ()
    report: HostReport | None = None
    build: str | None = None
    read_credentials_file: str | None = None
    poll_seconds: int = POLL_SECONDS
    history: int = HISTORY
    generations: int = GENERATIONS
    age_identity: str | None = None
    age_key_file: str = AGE_KEY_FILE
    gate: Any = None
    attribute: str = field(init=False, default="")
    #: The credential ``piceli host secrets`` writes to
    #: ``read_credentials_file`` (``None``: the host's own file).
    cache_credentials: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        from piceli.dev.nix import NixCache
        from piceli.infra.machines.model import Server

        if not isinstance(self.name, str) or not _LABEL.fullmatch(self.name):
            raise _invalid("NixHost(name=) is a DNS label")
        if isinstance(self.server, str):
            SshTarget.parse(self.server)
        elif not isinstance(self.server, Server):
            raise _invalid(
                "NixHost(server=) is a piceli.infra.Server or ssh://user@host"
            )
        if not isinstance(self.flake, str):
            raise _invalid("NixHost(flake=) is .#NAME or .#ATTR")
        object.__setattr__(self, "attribute", _attribute(self.flake))
        if not isinstance(self.follow, (Tag, Branch)):
            raise _invalid("NixHost(follow=) is Tag(...) or Branch(...)")
        if not isinstance(self.cache, NixCache):
            raise _invalid("NixHost(cache=) is a piceli.infra.NixCache")
        if isinstance(self.follow, Tag) and self.cache.scheme == "https":
            raise _invalid(
                "NixHost: following tags lists the cache; give its s3:// bucket "
                "or file:// directory (an https:// cache can follow a Branch)"
            )
        checks = tuple(self.checks)
        if not all(isinstance(check, HostCheck) for check in checks):
            raise _check_error("NixHost(checks=) is a list of HostCheck")
        names = [check.describe(i)["name"] for i, check in enumerate(checks)]
        if len(set(names)) != len(names):
            raise _check_error("NixHost(checks=): two checks have the same name")
        object.__setattr__(self, "checks", checks)
        if self.report is not None and not isinstance(self.report, HostReport):
            raise _invalid("NixHost(report=) is a HostReport")
        build = self.build
        if build is None:
            parts = self.attribute[2:].split(".")
            build = (
                parts[1]
                if parts[0] == "nixosConfigurations" and _LABEL.fullmatch(parts[1])
                else self.name
            )
        if not _LABEL.fullmatch(build):
            raise _invalid("NixHost(build=) is the builds' manifest name (a DNS label)")
        object.__setattr__(self, "build", build)
        path = self.read_credentials_file
        if path is None and self.cache.read_credentials is not None:
            path = PICELI_CACHE_ENV
            object.__setattr__(self, "cache_credentials", self.cache.read_credentials)
        if path is None and self.cache.scheme == "s3":
            path = CACHE_ENV
        if path is not None and not _PATH.fullmatch(path):
            raise _invalid("NixHost(read_credentials_file=) is an absolute path")
        object.__setattr__(self, "read_credentials_file", path)
        for value, low, high, what in (
            (self.poll_seconds, 30, 86400, "poll_seconds"),
            (self.history, 1, 1000, "history"),
            (self.generations, 2, 1000, "generations"),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not low <= value <= high
            ):
                raise _invalid(f"NixHost({what}=) is {low} to {high}")
        if self.age_identity is not None:
            from piceli.profiles import ProfileError, check_name

            try:
                check_name(self.age_identity)
            except (ProfileError, TypeError):
                raise _invalid("NixHost(age_identity=) is a credential name") from None
        if not isinstance(self.age_key_file, str) or not _PATH.fullmatch(
            self.age_key_file
        ):
            raise _invalid("NixHost(age_key_file=) is an absolute path")
        if self.gate is not None:
            from piceli.infra.promotion import Promotion

            if not isinstance(self.gate, Promotion):
                raise _invalid("NixHost(gate=) is a piceli.infra.Promotion")

    def ssh_target(self, address: str | None = None) -> SshTarget:
        """Where its status is read: the SSH target, or the server's ``Ssh``
        at ``address`` (its ``Ssh(address=)``, else the applied address)."""
        if isinstance(self.server, str):
            return SshTarget.parse(self.server)
        ssh = self.server.ssh
        found = ssh.address or address
        if not found:
            raise HostError(
                "host-address-unknown",
                f"server {self.server.name} has no Ssh(address=); pass --infra "
                "MODULE:ATTR so its applied address is read",
            )
        return SshTarget(found, ssh.user, ssh.port)


def _objects(cache: Any) -> dict[str, Any]:
    if cache.scheme == "file":
        return {"kind": "file", "root": urllib.parse.urlsplit(str(cache.url)).path}
    if cache.scheme == "s3":
        return {
            "kind": "s3",
            "endpoint": cache.endpoint or "s3.amazonaws.com",
            "bucket": cache.bucket_name,
            "region": cache.region,
        }
    return {"kind": "https", "base": str(cache.url).rstrip("/")}


def agent_config(host: NixHost) -> dict[str, Any]:
    """The agent's configuration (no secret: paths of root-only files only).

    The rendered module adds ``system`` and ``tools`` (the host's own)."""
    return {
        "schema": CONFIG_SCHEMA,
        "host": host.name,
        "build": host.build,
        "attribute": host.attribute,
        "follow": host.follow.describe(),
        "cache": {
            "store_url": host.cache.store_url,
            "public_key": host.cache.trusted_key(),
            "objects": _objects(host.cache),
        },
        "read_credentials_file": host.read_credentials_file,
        "checks": [check.describe(i) for i, check in enumerate(host.checks)],
        "report": host.report.describe() if host.report is not None else None,
        "poll_seconds": host.poll_seconds,
        "history": host.history,
        "generations": host.generations,
        "state_dir": STATE_DIR,
        # 0.25.0, only when declared: the build must carry a verified pin.
        **({"gate": host.gate.to_dict()} if host.gate is not None else {}),
    }
