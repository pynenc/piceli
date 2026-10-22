"""``Cluster(dev=DevBuilds(...))``: the declaration of development builds (0.18.0).

The owner declares where runs execute (a builder node), with which image
and tools (profiles), how big each run is, how many run at once and how
large the shared cache may grow. ``piceli cluster init`` plans and installs
it: the owner's approval of that plan authorizes the runs it bounds.

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from piceli.pipeline.errors import PipelineError

#: Where runs, their cache and their settings live.
NAMESPACE = "piceli-dev"
CACHE_CLAIM = "piceli-dev-cache"
CONFIG_MAP = "piceli-dev-config"
#: Where the scheduler publishes runs, queue and cache use (piceli-system).
STATUS_CONFIG_MAP = "piceli-dev-status"

_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_PINNED = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
_TOOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_RESERVED_ENV = {"CARGO_TARGET_DIR", "CARGO_HOME", "HOME", "PICELI_DEV_SPEC"}


class DevError(PipelineError):
    """A development-build command was refused or a run failed (``dev-*`` codes)."""


def _invalid(message: str) -> Exception:
    from piceli.infra.cluster import ClusterError

    return ClusterError("cluster-invalid", f"DevBuilds: {message}")


def _bytes(value: Any) -> int | None:
    try:
        from piceli.restore.claims import quantity

        return int(quantity(value))
    except Exception:
        return None


def _cpu(value: Any) -> float | None:
    text = str(value)
    try:
        return float(text[:-1]) / 1000 if text.endswith("m") else float(text)
    except ValueError:
        return None


def parse_seconds(value: str | int) -> int:
    from piceli.gitops.config import parse_duration

    return int(value) if isinstance(value, int) else int(parse_duration(value))


@dataclass(frozen=True)
class DevProfile:
    """A toolchain a run uses: image, tools it must find, environment, size.

    :param name: ``--profile NAME`` (a DNS label); the first profile is the
        default.
    :param image: The run's image, pinned by digest; default the
        ``DevBuilds`` image (the builder image: cargo, cargo-zigbuild, zig,
        rustup targets, node).
    :param tools: Commands the image must have (checked before the command
        runs: ``dev-tool-missing``).
    :param env: Extra environment (never ``CARGO_TARGET_DIR``, ``CARGO_HOME``
        or ``HOME``, which Piceli sets).
    :param prefetch: ``"cargo"``: ``cargo fetch --locked`` before the
        command, so ``--offline`` commands find their crates.
    :param toolchain: The command whose output keys the cache (a new
        toolchain gets new lineages); ``None``: one key for the profile.
    :param cpu: CPU request of a run (default ``DevBuilds.run_cpu``).
    :param memory: Memory request and limit (default ``DevBuilds.run_memory``).
    :param timeout: The command's deadline (default ``DevBuilds.timeout``).
    :param toolchains: 0.22: tools installed once into the cache and put
        first on ``PATH``: ``{"node": ".nvmrc", "binaryen": "123",
        "wasm-bindgen": "Cargo.lock"}``, each a path relative to the run's
        tree root or a version (Rust follows the source's own
        ``rust-toolchain.toml``).
    :param nix: 0.24: runs use the build host's own Nix: a docker host
        mounts its ``/nix`` read-only into the run (the store and the
        daemon's socket; builds happen in the host daemon's sandbox) and
        sets ``NIX_REMOTE=daemon``; a native host uses its ``nix``. See
        :meth:`for_nix`.
    """

    name: str
    image: str | None = None
    tools: Sequence[str] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    prefetch: Literal["cargo"] | None = None
    toolchain: Sequence[str] | None = ("rustc", "--version")
    cpu: str | None = None
    memory: str | None = None
    timeout: str | None = None
    toolchains: Mapping[str, str] = field(default_factory=dict)
    nix: bool = False

    @classmethod
    def for_nix(cls, name: str = "nix", **options: Any) -> DevProfile:
        """A profile for ``piceli dev nix build``: the host's Nix, keyed by
        ``nix --version`` (0.24.0)."""
        options.setdefault("tools", ["nix"])
        options.setdefault("toolchain", ["nix", "--version"])
        return cls(name, nix=True, **options)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _LABEL.fullmatch(self.name):
            raise _invalid(f"profile name {self.name!r} is not a DNS label")
        if not isinstance(self.nix, bool):
            raise _invalid(f"profile {self.name}: nix is True or False")
        if self.toolchains:
            from piceli.dev.toolchains import ToolchainError, check_declared

            try:
                pins = check_declared(
                    self.toolchains, f"profile {self.name}: toolchains"
                )
            except ToolchainError as error:
                raise _invalid(str(error)) from None
            if "rust" in pins:
                raise _invalid(
                    f"profile {self.name}: toolchains cannot pin rust; runs follow "
                    "the source's rust-toolchain.toml"
                )
            object.__setattr__(self, "toolchains", pins)
        else:
            object.__setattr__(self, "toolchains", {})
        if self.image is not None and not _PINNED.fullmatch(str(self.image)):
            raise _invalid(f"profile {self.name}: image must be pinned by digest")
        tools = tuple(str(tool) for tool in self.tools)
        if not all(_TOOL.fullmatch(tool) for tool in tools):
            raise _invalid(f"profile {self.name}: tools are command names")
        object.__setattr__(self, "tools", tools)
        env = {str(k): str(v) for k, v in dict(self.env).items()}
        if any(not _ENV.fullmatch(k) or k in _RESERVED_ENV for k in env):
            raise _invalid(
                f"profile {self.name}: env names are variables Piceli does not set"
            )
        object.__setattr__(self, "env", env)
        if self.prefetch not in (None, "cargo"):
            raise _invalid(f"profile {self.name}: prefetch is None or 'cargo'")
        if self.toolchain is not None:
            object.__setattr__(self, "toolchain", tuple(str(p) for p in self.toolchain))
        if self.cpu is not None and (_cpu(self.cpu) or 0) <= 0:
            raise _invalid(f"profile {self.name}: cpu")
        if self.memory is not None and (_bytes(self.memory) or 0) <= 0:
            raise _invalid(f"profile {self.name}: memory")
        if self.timeout is not None:
            parse_seconds(self.timeout)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "image": self.image,
            "tools": list(self.tools),
            "env": dict(sorted(self.env.items())),
            "prefetch": self.prefetch,
            "toolchain": None if self.toolchain is None else list(self.toolchain),
            "cpu": self.cpu,
            "memory": self.memory,
            "timeout": self.timeout,
            # 0.22: only when declared, so other configs are unchanged.
            **({"toolchains": dict(self.toolchains)} if self.toolchains else {}),
            **({"nix": True} if self.nix else {}),
        }


_PLATFORMS = ("linux/amd64", "linux/arm64")
_CONTEXT = re.compile(
    r"(?:(?:ssh|tcp|unix|npipe)://\S+|[A-Za-z0-9][A-Za-z0-9._-]{0,63})"
)
_CLI = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}")
HOST_KINDS = ("docker", "native", "cluster")
#: 0.22.0: health events within the window that quarantine a build host.
QUARANTINE_AFTER = 2
QUARANTINE_WINDOW = "24h"
_DEFAULT = object()


@dataclass(frozen=True)
class BuildHost:
    """A host development runs (and, with ``piceli builder serve``, GitOps
    builds) execute on (0.20.0; ``DevBuilds(hosts=[...])``).

    Build them with :meth:`docker`, :meth:`native` or :meth:`cluster`; the
    order of ``hosts`` is the order of preference.

    :param name: The host's name in ``--host``, results and status.
    :param kind: ``docker``: Linux containers through a Docker-compatible
        CLI (Docker Desktop, OrbStack, colima, Podman, a Linux host's
        Docker), local or over SSH; ``native``: the wrapper runs on this
        machine itself, no container (macOS-only builds); ``cluster``: a
        Job on the declared node (``DevBuilds(node=)``).
    :param context: ``docker`` only: ``None`` (the CLI's default),
        ``ssh://user@host`` (the CLI's own SSH transport: nothing to install
        on the host but the container engine) or a CLI context name.
    :param cli: ``docker`` only: the executable (``docker``, ``podman``).
    :param node: ``cluster`` only: the node (``DevBuilds.node``).
    :param slots: Runs at once on this host. Its queue lives on the host
        (container names, or lock files for ``native``), so every client of
        the host shares it, whether or not the cluster is reachable.
    :param cpu: CPU limit of one run (``docker``; default: none).
    :param memory: Memory limit of one run (``docker``; default: the
        profile's).
    :param platforms: The platforms it builds natively (default: the
        host's own architecture, read when it is first used).
    :param state_dir: ``native`` only: where its cache and queue live
        (default ``~/.cache/piceli/dev/NAME``).
    :param image: The run image here (default: the profile's).
    :param quarantine_after: Health events (a crashed compiler, a segfault,
        an illegal instruction, a machine check, a corrupt cached blob; see
        :mod:`piceli.dev.health`) within ``quarantine_window`` that
        quarantine the host (0.22.0): runs skip it and ``piceli builder
        serve`` on it stops claiming builds until ``piceli dev release-host``.
        ``None``: never quarantined.
    :param quarantine_window: How far back events count (``24h``).
    """

    name: str
    kind: Literal["docker", "native", "cluster"]
    context: str | None = None
    cli: str = "docker"
    node: str | None = None
    slots: int = 1
    cpu: str | None = None
    memory: str | None = None
    platforms: Sequence[str] = ()
    state_dir: str | None = None
    image: str | None = None
    quarantine_after: int | None = QUARANTINE_AFTER
    quarantine_window: str = QUARANTINE_WINDOW

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _LABEL.fullmatch(self.name):
            raise _invalid("a build host's name is a DNS label")
        if self.quarantine_after is not None and (
            not isinstance(self.quarantine_after, int)
            or isinstance(self.quarantine_after, bool)
            or self.quarantine_after < 1
        ):
            raise _invalid(
                f"host {self.name}: quarantine_after is a positive integer or None"
            )
        try:
            parse_seconds(self.quarantine_window)
        except Exception:
            raise _invalid(
                f"host {self.name}: quarantine_window is a duration (24h, 90m)"
            ) from None
        if self.kind not in HOST_KINDS:
            raise _invalid(f"host {self.name}: kind is one of {', '.join(HOST_KINDS)}")
        if self.context is not None and (
            self.kind != "docker" or not _CONTEXT.fullmatch(self.context)
        ):
            raise _invalid(
                f"host {self.name}: context is ssh://user@host, a socket URL or "
                "a CLI context name (docker hosts only)"
            )
        if not _CLI.fullmatch(self.cli):
            raise _invalid(f"host {self.name}: cli is an executable name")
        if (self.kind == "cluster") != (self.node is not None):
            raise _invalid(f"host {self.name}: node is given for cluster hosts only")
        if self.node is not None and not _LABEL.fullmatch(self.node):
            raise _invalid(f"host {self.name}: node must name a declared node")
        if (
            not isinstance(self.slots, int)
            or isinstance(self.slots, bool)
            or self.slots < 1
        ):
            raise _invalid(f"host {self.name}: slots is a positive integer")
        if self.cpu is not None and (_cpu(self.cpu) or 0) <= 0:
            raise _invalid(f"host {self.name}: cpu is a CPU quantity (4, 500m)")
        if self.memory is not None and (_bytes(self.memory) or 0) <= 0:
            raise _invalid(f"host {self.name}: memory is a byte quantity (8Gi)")
        platforms = tuple(str(item) for item in self.platforms)
        if not all(item in _PLATFORMS for item in platforms):
            raise _invalid(
                f"host {self.name}: platforms are among {', '.join(_PLATFORMS)}"
            )
        object.__setattr__(self, "platforms", platforms)
        if self.image is not None and not _PINNED.fullmatch(self.image):
            raise _invalid(f"host {self.name}: image must be pinned by digest")

    @classmethod
    def docker(
        cls, name: str, *, context: str | None = None, **options: Any
    ) -> BuildHost:
        """Linux containers through a Docker-compatible CLI, local or ``ssh://``."""
        return cls(name, "docker", context=context, **options)

    @classmethod
    def native(cls, name: str, **options: Any) -> BuildHost:
        """This machine itself, no container (macOS-only builds)."""
        return cls(name, "native", **options)

    @classmethod
    def cluster(
        cls, name: str, *, node: str | None = None, **options: Any
    ) -> BuildHost:
        """A Job on a cluster node (``node`` defaults to ``name``)."""
        return cls(name, "cluster", node=node or name, **options)

    def describe(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "name": self.name,
                "kind": self.kind,
                "context": self.context,
                "cli": self.cli if self.kind == "docker" else None,
                "node": self.node,
                "slots": self.slots,
                "cpu": self.cpu,
                "memory": self.memory,
                "platforms": list(self.platforms),
                "state_dir": self.state_dir,
                "image": self.image,
                # 0.22.0: only when changed, so earlier declarations keep
                # their hashes.
                "quarantine_after": self.quarantine_after
                if self.quarantine_after != QUARANTINE_AFTER
                else _DEFAULT,
                "quarantine_window": self.quarantine_window
                if self.quarantine_window != QUARANTINE_WINDOW
                else _DEFAULT,
            }.items()
            if value not in (None, []) and value is not _DEFAULT
        } | ({"quarantine_after": None} if self.quarantine_after is None else {})

    def quarantine_policy(self) -> tuple[int | None, int]:
        """``(events, window seconds)`` that quarantine this host."""
        return self.quarantine_after, parse_seconds(self.quarantine_window)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BuildHost:
        return cls(
            str(value["name"]),
            value["kind"],
            context=value.get("context"),
            cli=str(value.get("cli") or "docker"),
            node=value.get("node"),
            slots=int(value.get("slots") or 1),
            cpu=value.get("cpu"),
            memory=value.get("memory"),
            platforms=value.get("platforms") or (),
            state_dir=value.get("state_dir"),
            image=value.get("image"),
            quarantine_after=value.get("quarantine_after", QUARANTINE_AFTER),
            quarantine_window=str(value.get("quarantine_window") or QUARANTINE_WINDOW),
        )


@dataclass(frozen=True)
class DevBuilds:
    """Development builds (``Cluster(dev=...)``): on a builder node, and since
    0.20.0 on declared build hosts.

    :param node: The declared node in-cluster runs execute on (usually the
        builder); ``None``: no in-cluster runs (then ``hosts`` is required).
    :param image: The default run image, pinned by digest (Piceli's builder
        image: ``ghcr.io/pynenc/piceli-builder@sha256:...``).
    :param profiles: :class:`DevProfile` items; the first is the default
        (one profile ``default`` when none is given).
    :param slots: Runs at once: an integer, or ``"auto"``: as many runs of
        ``run_cpu`` and ``run_memory`` as the node's allocatable CPU and
        memory hold (at least one).
    :param run_cpu: CPU request of one run (no CPU limit: an idle node lends
        its cores).
    :param run_memory: Memory request and limit of one run.
    :param run_storage: The run's scratch (its upload and extracted tree).
    :param cache_size: The shared cache claim; free lineages are evicted,
        least recently used first, when their bytes pass it.
    :param timeout: A command's deadline.
    :param network: ``"fetch"``: runs may reach public addresses on ports
        80 and 443 (crates, toolchains) and DNS, never cluster or private
        addresses; ``"none"``: DNS only.
    :param hosts: :class:`BuildHost` items, in order of preference (0.20.0).
        With ``node`` and no ``BuildHost.cluster`` among them, the node is
        the first host.
    """

    node: str | None
    image: str
    profiles: Sequence[DevProfile] = ()
    slots: int | Literal["auto"] = "auto"
    run_cpu: str = "4"
    run_memory: str = "8Gi"
    run_storage: str = "20Gi"
    cache_size: str = "200Gi"
    timeout: str = "1h"
    network: Literal["fetch", "none"] = "fetch"
    hosts: Sequence[BuildHost] = ()

    def __post_init__(self) -> None:
        if self.node is not None and (
            not isinstance(self.node, str) or not _LABEL.fullmatch(self.node)
        ):
            raise _invalid("node must name a declared node")
        hosts = tuple(self.hosts)
        if not all(isinstance(item, BuildHost) for item in hosts):
            raise _invalid("hosts are BuildHost.docker/native/cluster(...) items")
        names = [item.name for item in hosts]
        if len(set(names)) != len(names):
            raise _invalid("a build host is declared twice")
        for item in hosts:
            if item.kind == "cluster" and item.node != self.node:
                raise _invalid(
                    f"host {item.name}: a cluster host runs on DevBuilds(node=)"
                )
        if self.node is not None and not any(i.kind == "cluster" for i in hosts):
            hosts = (BuildHost.cluster(self.node),) + hosts
        if not hosts:
            raise _invalid("declare node= (in-cluster runs) or hosts=[...]")
        object.__setattr__(self, "hosts", hosts)
        if not isinstance(self.image, str) or not _PINNED.fullmatch(self.image):
            raise _invalid("image must be pinned by digest (repository@sha256:...)")
        profiles = tuple(self.profiles) or (DevProfile("default"),)
        if not all(isinstance(item, DevProfile) for item in profiles):
            raise _invalid("profiles are DevProfile(...) items")
        names = [item.name for item in profiles]
        if len(set(names)) != len(names):
            raise _invalid("a profile is declared twice")
        object.__setattr__(self, "profiles", profiles)
        if self.slots != "auto" and (
            not isinstance(self.slots, int)
            or isinstance(self.slots, bool)
            or self.slots < 1
        ):
            raise _invalid('slots is a positive integer or "auto"')
        if (_cpu(self.run_cpu) or 0) <= 0:
            raise _invalid("run_cpu is a CPU quantity (4, 500m)")
        for name in ("run_memory", "run_storage"):
            if (_bytes(getattr(self, name)) or 0) <= 0:
                raise _invalid(f"{name} is a byte quantity (8Gi)")
        if (_bytes(self.cache_size) or 0) < 1 << 30:
            raise _invalid("cache_size is at least 1Gi")
        parse_seconds(self.timeout)
        if self.network not in ("fetch", "none"):
            raise _invalid('network is "fetch" or "none"')

    def profile(self, name: str | None) -> DevProfile:
        """The profile ``name`` with the defaults filled in (``None``: the first).

        :raises DevError: ``dev-profile-unknown``.
        """
        found = (
            self.profiles[0]
            if name is None
            else next((item for item in self.profiles if item.name == name), None)
        )
        if found is None:
            raise DevError(
                "dev-profile-unknown",
                f"no dev profile {name!r}; declared: "
                + ", ".join(item.name for item in self.profiles),
            )
        return DevProfile(
            found.name,
            image=found.image or self.image,
            tools=found.tools,
            env=found.env,
            prefetch=found.prefetch,
            toolchain=found.toolchain,
            cpu=found.cpu or self.run_cpu,
            memory=found.memory or self.run_memory,
            timeout=found.timeout or self.timeout,
            toolchains=found.toolchains,
            nix=found.nix,
        )

    def host(self, name: str | None) -> BuildHost | None:
        """The declared host ``name`` (``None``: none given)."""
        return next((item for item in self.hosts if item.name == name), None)

    def describe(self) -> dict[str, Any]:
        """The declaration as data (in the cluster's description and config)."""
        return {
            "node": self.node,
            "hosts": [item.describe() for item in self.hosts],
            "image": self.image,
            "profiles": [item.describe() for item in self.profiles],
            "slots": self.slots,
            "run_cpu": self.run_cpu,
            "run_memory": self.run_memory,
            "run_storage": self.run_storage,
            "cache_size": self.cache_size,
            "timeout": self.timeout,
            "network": self.network,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DevBuilds:
        """The declaration read back from ``piceli-dev-config``."""
        profiles = [
            DevProfile(
                str(item["name"]),
                image=item.get("image"),
                tools=item.get("tools") or (),
                env=item.get("env") or {},
                prefetch=item.get("prefetch"),
                toolchain=item.get("toolchain"),
                cpu=item.get("cpu"),
                memory=item.get("memory"),
                timeout=item.get("timeout"),
                toolchains=item.get("toolchains") or {},
                nix=bool(item.get("nix", False)),
            )
            for item in value.get("profiles") or ()
        ]
        node = value.get("node")
        return cls(
            node=str(node) if node else None,
            image=str(value["image"]),
            hosts=[BuildHost.from_dict(item) for item in value.get("hosts") or ()],
            profiles=profiles,
            slots=value.get("slots", "auto"),
            run_cpu=str(value.get("run_cpu", "4")),
            run_memory=str(value.get("run_memory", "8Gi")),
            run_storage=str(value.get("run_storage", "20Gi")),
            cache_size=str(value.get("cache_size", "200Gi")),
            timeout=str(value.get("timeout", "1h")),
            network=value.get("network", "fetch"),
        )


def cpu_of(value: str) -> float:
    return _cpu(value) or 0.0


def bytes_of(value: str) -> int:
    return _bytes(value) or 0
