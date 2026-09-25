"""Where deployment state lives: this machine (``local``) or the cluster (``cluster``).

Maturity: **preview**.

A :class:`~piceli.pipeline.Pipeline` (``state="cluster"``) or a
``release.toml`` (``[release] state = "cluster"``) keeps its run journal,
release catalog, execution journal, secret versions and receipts in the
release namespace, behind a release-scoped Lease lock with fencing, so any
runner can plan and any other runner can apply, resume or roll back. See
``docs/state.md``.

Importing this package reads no file and contacts nothing.
"""

from piceli.state.backend import (
    BACKENDS,
    Session,
    StateScope,
    StateSettings,
    directory_lock,
    session,
)
from piceli.state.errors import StateError

__all__ = [
    "BACKENDS",
    "Session",
    "StateError",
    "StateScope",
    "StateSettings",
    "directory_lock",
    "session",
]
