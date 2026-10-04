"""The Machines view of the UI: a declared Infrastructure's status (read-only).

The UI owner starts ``piceli ui serve --infra MODULE:ATTR``; the page shows
what ``piceli infra status`` shows (servers, addresses, installs, cluster
registrations, monthly cost) from the state directory's records. It never
runs OpenTofu, reads no credential and changes nothing; plans and applies
stay on the command line with their approvals.

Importing this module is side-effect free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from piceli.infra import Infrastructure

__all__ = ["MachinesControl"]


class MachinesControl:
    """Serves ``GET /api/v1/machines`` for one declared ``Infrastructure``."""

    def __init__(self, infra: Infrastructure, ref: str) -> None:
        self.infra = infra
        self.ref = ref

    def status(self) -> dict[str, Any]:
        from piceli.infra.machines.status import summarize

        body = summarize(self.infra)
        # A local path is not the page's business.
        body.pop("state_dir", None)
        return {
            **body,
            "ref": self.ref,
            "commands": {
                "plan": f"piceli infra plan {self.ref}",
                "status": f"piceli infra status {self.ref}",
            },
        }
