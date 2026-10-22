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
        report=HostReport("https://poet.example.net:4318"),
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
#: Defaults (proposals): a poll every 5 minutes, 60 s per check, 20 attempts kept.
POLL_SECONDS = 300
CHECK_TIMEOUT = 60
HISTORY = 20
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
    ``MAJOR.MINOR.PATCH``, an optional ``-prerelease`` and ``+build``); a
    matching tag that is not one is ignored (the status lists it).
    Pre-releases count only with ``prereleases=True``.
    """

    pattern: str = "v*"
    prereleases: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.pattern, str) or not _PATTERN.fullmatch(self.pattern):
            raise _invalid("Tag(pattern=) is a glob of tag names (v*)")

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

    Build them with :meth:`unit`, :meth:`http` or :meth:`command`.
    """

    kind: Literal["unit", "http", "command"]
    target: tuple[str, ...]
    timeout: float = CHECK_TIMEOUT
    name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", tuple(self.target))
        if not isinstance(self.timeout, (int, float)) or not 1 <= self.timeout <= 3600:
            raise _check_error("HostCheck(timeout=) is 1 to 3600 seconds")
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
        cls, unit: str, *, timeout: float = CHECK_TIMEOUT, name: str | None = None
    ) -> HostCheck:
        """The systemd unit is ``active``."""
        return cls("unit", (unit,), timeout, name)

    @classmethod
    def http(
        cls, url: str, *, timeout: float = CHECK_TIMEOUT, name: str | None = None
    ) -> HostCheck:
        """A GET of ``url`` from the host answers 2xx."""
        return cls("http", (url,), timeout, name)

    @classmethod
    def command(
        cls, *argv: str, timeout: float = CHECK_TIMEOUT, name: str | None = None
    ) -> HostCheck:
        """The command exits 0 (run as root, with the new system's PATH)."""
        return cls("command", tuple(argv), timeout, name)

    def describe(self, index: int) -> dict[str, Any]:
        return {
            "name": self.name or f"{self.kind}-{index + 1}",
            "kind": self.kind,
            "target": list(self.target),
            "timeout": float(self.timeout),
        }


@dataclass(frozen=True)
class HostReport:
    """Where the host agent reports its deploys: an OTLP/HTTP endpoint (logs).

    :param endpoint: ``https://collector:4318`` (``/v1/logs`` is appended)
        or the full ``.../v1/logs`` URL. Plain ``http://`` needs
        ``insecure=True`` unless it is a loopback address.
    :param headers_file: A root-only file on the host with ``Name: value``
        lines (``Authorization: Bearer ...``) the operator puts there; the
        rendered module, committed to Git, never holds a header value.
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
        cache's **read-only** S3 keys (``AWS_ACCESS_KEY_ID=`` and
        ``AWS_SECRET_ACCESS_KEY=`` lines); default
        ``/etc/piceli-host/cache.env`` when the cache declares
        ``read_credentials``, none for a public cache.
    :param poll_seconds: How often the agent looks for a new build.
    :param history: How many attempts the status keeps.
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
    attribute: str = field(init=False, default="")

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
            path = "/etc/piceli-host/cache.env"
        if path is not None and not _PATH.fullmatch(path):
            raise _invalid("NixHost(read_credentials_file=) is an absolute path")
        object.__setattr__(self, "read_credentials_file", path)
        for value, low, high, what in (
            (self.poll_seconds, 30, 86400, "poll_seconds"),
            (self.history, 1, 1000, "history"),
        ):
            if not isinstance(value, int) or not low <= value <= high:
                raise _invalid(f"NixHost({what}=) is {low} to {high}")

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
            "public_key": host.cache.public_key,
            "objects": _objects(host.cache),
        },
        "read_credentials_file": host.read_credentials_file,
        "checks": [check.describe(i) for i, check in enumerate(host.checks)],
        "report": host.report.describe() if host.report is not None else None,
        "poll_seconds": host.poll_seconds,
        "history": host.history,
        "state_dir": STATE_DIR,
    }
