"""Nix host releases: a NixOS host that follows a Git ref from a binary cache (0.24.0).

:class:`NixHost` declares the host, the ref it follows (:class:`Tag` or
:class:`Branch`), the :class:`~piceli.dev.nix.NixCache` it substitutes
from and the :class:`HostCheck` s that must pass after a switch.
``piceli host module`` renders a NixOS module that installs the agent
(:mod:`piceli.hosts.agent`); ``piceli host status`` and ``piceli host
wait`` read what it reports, over SSH.

Importing this package is side-effect free.
"""

from __future__ import annotations

from piceli.hosts.model import (
    Branch,
    HostCheck,
    HostError,
    HostReport,
    NixHost,
    Services,
    Tag,
)

__all__ = [
    "Branch",
    "HostCheck",
    "HostError",
    "HostReport",
    "NixHost",
    "Services",
    "Tag",
]
