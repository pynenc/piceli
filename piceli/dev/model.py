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

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _LABEL.fullmatch(self.name):
            raise _invalid(f"profile name {self.name!r} is not a DNS label")
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
        }


@dataclass(frozen=True)
class DevBuilds:
    """Development builds on one builder node (``Cluster(dev=...)``).

    :param node: The declared node runs execute on (usually the builder).
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
    """

    node: str
    image: str
    profiles: Sequence[DevProfile] = ()
    slots: int | Literal["auto"] = "auto"
    run_cpu: str = "4"
    run_memory: str = "8Gi"
    run_storage: str = "20Gi"
    cache_size: str = "200Gi"
    timeout: str = "1h"
    network: Literal["fetch", "none"] = "fetch"

    def __post_init__(self) -> None:
        if not isinstance(self.node, str) or not _LABEL.fullmatch(self.node):
            raise _invalid("node must name a declared node")
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
        )

    def describe(self) -> dict[str, Any]:
        """The declaration as data (in the cluster's description and config)."""
        return {
            "node": self.node,
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
            )
            for item in value.get("profiles") or ()
        ]
        return cls(
            node=str(value["node"]),
            image=str(value["image"]),
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
