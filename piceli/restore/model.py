"""Typed declarations of restore points: quiesce hooks and pipeline settings.

Maturity: **preview** (the API may change before 1.0).

A *restore point* is a verified archive of every retained claim
(PersistentVolumeClaim) a release touches, taken before the release changes
the workloads that write them. :class:`RestorePoints` turns it on for a
:class:`~piceli.pipeline.Pipeline`; :class:`Quiesce` hooks, declared with
``app.quiesce(workload, ...)``, run in each writer pod before the writers are
stopped (flush a cache, checkpoint a database). See
``docs/restore_points.md``.

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_PINNED = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
#: Field manager of the scale requests that stop and restart writers.
FIELD_MANAGER = "piceli-restore-point"


class RestorePointError(ValueError):
    """A restore point could not be planned, taken, verified or restored.

    ``code`` is registered in :mod:`piceli.errors`; the message never holds a
    secret value or file content. ``failed`` is ``True`` when something ran
    (exit 1) and ``False`` for a refusal before any change (exit 2).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        failed: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.failed = failed
        self.details = dict(details or {})


class Quiesce(BaseModel):
    """A hook run in every pod of a writer before it is stopped.

    Build hooks with :meth:`http` (a request to the pod through the API
    server's pod proxy) or :meth:`exec` (a command in the pod), and declare
    them with ``app.quiesce(workload, hook, ...)``. Writers are always stopped
    afterwards (scaled to zero) and Piceli waits until their pods are gone,
    terminating pods included; a hook only makes the data on disk complete
    first (flush, checkpoint, ``SAVE``).

    Invariants: an ``http`` hook has a ``path`` starting with ``/`` and a
    port; an ``exec`` hook has a non-empty ``command``. A hook that fails or
    times out stops the restore point before anything is stopped.

    Example::

        app.quiesce(cache, Quiesce.exec(["redis-cli", "SAVE"]))
        app.quiesce(api, Quiesce.http("/admin/flush", 8080))
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["http", "exec"]
    path: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    method: Literal["GET", "POST", "PUT"] = "POST"
    expect: int = Field(default=200, ge=100, le=599)
    command: tuple[str, ...] | None = None
    container: str | None = Field(default=None, min_length=1, max_length=63)
    timeout_seconds: int = Field(default=30, ge=1, le=3600)

    @model_validator(mode="after")
    def _shape(self) -> Quiesce:
        if self.action == "http":
            if self.port is None or not (self.path or "").startswith("/"):
                raise ValueError(
                    "an http quiesce hook needs a port and a path starting '/'"
                )
            if self.command is not None:
                raise ValueError("an http quiesce hook has no command")
        else:
            if not self.command:
                raise ValueError("an exec quiesce hook needs a command")
            if self.path is not None or self.port is not None:
                raise ValueError("an exec quiesce hook has only a command")
        return self

    @classmethod
    def http(
        cls,
        path: str,
        port: int,
        *,
        method: Literal["GET", "POST", "PUT"] = "POST",
        expect: int = 200,
        timeout_seconds: int = 30,
    ) -> Quiesce:
        """Send ``method path`` to ``port`` of each writer pod; expect ``expect``."""
        return cls(
            action="http",
            path=path,
            port=port,
            method=method,
            expect=expect,
            timeout_seconds=timeout_seconds,
        )

    @classmethod
    def exec(
        cls,
        command: list[str] | tuple[str, ...],
        *,
        container: str | None = None,
        timeout_seconds: int = 30,
    ) -> Quiesce:
        """Run ``command`` (no shell) in each writer pod; it must exit 0.

        Its output is never printed or stored.
        """
        return cls(
            action="exec",
            command=tuple(command),
            container=container,
            timeout_seconds=timeout_seconds,
        )

    def describe(self) -> dict[str, Any]:
        """A JSON-safe description (plans and the combined hash)."""
        if self.action == "http":
            return {
                "type": "http",
                "method": self.method,
                "path": self.path,
                "port": self.port,
                "expect": self.expect,
                "timeout_seconds": self.timeout_seconds,
            }
        return {
            "type": "exec",
            "command": list(self.command or ()),
            **({"container": self.container} if self.container else {}),
            "timeout_seconds": self.timeout_seconds,
        }


class RestorePoints(BaseModel):
    """Take a restore point before a release changes a stateful workload.

    With ``Pipeline(..., restore_points=RestorePoints())`` a ``piceli deploy``
    run gets a ``backup`` stage before its release plan: for every workload
    whose container images or storage settings (claim templates, claim
    mounts) the release changes, it archives each retained claim the
    workload writes, one per StatefulSet replica, as follows. Quiesce hooks
    run in the writers' pods; every writer of the claim is scaled to zero;
    Piceli waits until no pod mounts the claim writably (terminating pods
    included); a helper Job mounts the claim read-only and streams a gzip
    tarball to ``directory`` on the machine that runs Piceli, with a SHA-256;
    the archive is read back, listed and checked against a content digest
    computed in the cluster. The apply then starts the writers with the new
    release. ``piceli restore`` puts a restore point back.

    :param directory: Where archives are written, relative to the declaring
        file; default ``<state_dir>/restore-points``. Local only: never
        pruned by ``cache prune``, never copied to shared state.
    :param image: The helper Job's image, pinned by digest (it needs ``sh``,
        ``tar``, ``gzip``, ``find``, ``sort``, ``sha256sum`` and ``head``;
        busybox has them). Default: each writer's current image, already on
        the node, so nothing new is pulled.
    :param timeout_seconds: Bound for each wait (writers gone, helper pod
        running) and each copy.
    :param run_as_user: The helper's user; ``0`` (default) reads files of any
        owner and restores their ownership (it gets only the file
        capabilities ``CHOWN``, ``DAC_OVERRIDE``, ``DAC_READ_SEARCH``,
        ``FOWNER``, ``FSETID``).

    Example::

        pipeline = Pipeline(app, target, restore_points=RestorePoints())
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    directory: str | None = None
    image: str | None = None
    timeout_seconds: int = Field(default=600, ge=10, le=86400)
    run_as_user: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _shape(self) -> RestorePoints:
        if self.image is not None and not _PINNED.fullmatch(self.image):
            raise ValueError(
                "restore point helper image must be pinned by digest "
                "(repository@sha256:<64 hex>)"
            )
        return self

    def resolve_directory(self, state_dir: Path, base: Path | None) -> Path:
        """The archive directory for a pipeline."""
        if self.directory is None:
            return state_dir / "restore-points"
        path = Path(self.directory).expanduser()
        if not path.is_absolute() and base is not None:
            path = base / path
        return path

    def describe(self) -> dict[str, Any]:
        """A JSON-safe description; part of the combined hash."""
        return {
            "directory": self.directory,
            "image": self.image,
            "timeout_seconds": self.timeout_seconds,
            "run_as_user": self.run_as_user,
        }
