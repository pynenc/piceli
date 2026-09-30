"""Pre-rollout checks: declared next to a workload, run before it changes.

A :class:`PreRollout` names a workload and an argv to run with the workload's
**new** image and its real pod settings (Secrets, ConfigMaps, environment,
security context, service account). An optional :class:`UpgradeCheck` runs
another argv with the workload's retained claims mounted **read-only**, so an
app can ask "can the new binary open the store the running one wrote".

These are declarations only: they render no object, so declaring them never
changes a release's plan hash. ``piceli deploy`` plans and runs them (see
:mod:`piceli.pipeline.prerollout`). Importing this module has no side effects.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from piceli.app.model import (
    AbsolutePath,
    ClaimTemplate,
    ExistingClaim,
    Name,
    Workload,
)

#: Default seconds a check Job may run (image pull, mount and command).
DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 3600


def _argv(value: Any) -> Any:
    if isinstance(value, str):
        raise ValueError(
            "command is an argv list such as ['app', 'check-config'], not a string"
        )
    return value


class UpgradeCheck(BaseModel):
    """Open the retained volumes read-only with the new image.

    The Job mounts the claims the running workload uses (a StatefulSet's
    ``ClaimTemplate`` claims per ordinal, an ``ExistingClaim`` as is) with
    ``readOnly: true`` at the same paths, so the command sees the store the
    running version wrote and cannot change it.

    :param command: argv run in the new image (entrypoint override).
    :param volumes: Mount paths to open; default: every retained claim the
        workload's main container mounts.
    :param timeout_seconds: Longest the Job may take, from creation.

    Example::

        UpgradeCheck(["store", "verify", "--read-only", "/var/lib/db"])
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: tuple[str, ...] = Field(min_length=1)
    volumes: tuple[AbsolutePath, ...] = ()
    timeout_seconds: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS, ge=10, le=MAX_TIMEOUT_SECONDS
    )

    def __init__(self, command: Sequence[str], /, **data: Any) -> None:
        super().__init__(command=command, **data)

    _check_command = field_validator("command", mode="before")(_argv)


class PreRollout(BaseModel):
    """What to verify before a workload changes (see ``App.pre_rollout``).

    :param workload: Name of a Deployment, StatefulSet or DaemonSet.
    :param command: argv run in the new image with the workload's real pod
        settings; ``None`` to run only the ``upgrade`` check.
    :param timeout_seconds: Longest the Job may take, from creation.
    :param upgrade: Optional read-only check of the retained volumes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workload: Name
    command: tuple[str, ...] | None = Field(default=None, min_length=1)
    timeout_seconds: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS, ge=10, le=MAX_TIMEOUT_SECONDS
    )
    upgrade: UpgradeCheck | None = None

    _check_command = field_validator("command", mode="before")(_argv)

    def describe(self) -> dict[str, Any]:
        """The declaration as plain data (the plan shows and hashes it)."""
        value: dict[str, Any] = {"workload": self.workload}
        if self.command is not None:
            value["check"] = {
                "command": list(self.command),
                "timeout_seconds": self.timeout_seconds,
            }
        if self.upgrade is not None:
            value["upgrade"] = {
                "command": list(self.upgrade.command),
                "volumes": list(self.upgrade.volumes),
                "timeout_seconds": self.upgrade.timeout_seconds,
            }
        return value


def retained_mounts(workload: Workload) -> dict[str, ClaimTemplate | ExistingClaim]:
    """Retained claims the main container mounts, by mount path.

    A ``ClaimTemplate`` (per-pod claim of a StatefulSet) or an
    ``ExistingClaim``; memory, Secret and ConfigMap volumes are not retained.
    """
    result: dict[str, ClaimTemplate | ExistingClaim] = {}
    main = workload.containers[0]
    for path, item in main.volumes.items():
        volume = getattr(item, "volume", item)
        if isinstance(volume, ClaimTemplate | ExistingClaim):
            result[path] = volume
    return result
