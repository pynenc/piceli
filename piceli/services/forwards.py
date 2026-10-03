"""One Forwards workspace: every port forward or connection ticket of this session.

Starting and stopping go through the existing per-application paths
(:class:`~piceli.services.access.AccessService` on a local UI,
:class:`~piceli.services.remote_access.RemoteAccessService` tickets for
``piceli ui connect`` on the scoped cluster service), with their grants and
probes. This module lists them across scopes and recognises stale ones:

- ``reconnecting``: an owned forward lost its probe and is restarting;
- ``scope-removed``: its environment or profile scope is no longer registered;
- orphans: forwards a previous UI process left running, recorded in the
  same private registry ``piceli ui serve`` reaps at start
  (:class:`~piceli.k8s.owned_processes.OwnedProcessRegistry`). Stopping them
  signals only a verified recorded process whose owner is gone.

Importing this module is side-effect free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from piceli.services.contracts import (
    AccessSession,
    ForwardEntry,
    ForwardPage,
    StaleForwardResult,
)
from piceli.services.query import QueryError, QueryService

if TYPE_CHECKING:
    from piceli.k8s.owned_processes import OwnedProcessRegistry
    from piceli.services.access import AccessService
    from piceli.services.log_workspace import LogWorkspace
    from piceli.services.remote_access import RemoteAccessService

__all__ = ["ForwardWorkspace"]

_ACTIVE = frozenset({"pending", "connecting", "ready"})


class ForwardWorkspace:
    """List and tidy the session's forwards; never adopt a foreign process."""

    def __init__(
        self,
        query: QueryService,
        *,
        access: AccessService | None = None,
        remote: RemoteAccessService | None = None,
        registry: OwnedProcessRegistry | None = None,
        scopes: LogWorkspace | None = None,
    ) -> None:
        self.query = query
        self.access = access
        self.remote = remote
        self.registry = registry
        self.scopes = scopes

    @property
    def mode(self) -> str:
        if self.access is not None and self.access.tool is not None:
            return "local"
        if self.remote is not None:
            return "cluster"
        return "unavailable"

    def _sessions(self) -> tuple[AccessSession, ...]:
        if self.mode == "local":
            assert self.access is not None
            return self.access.sessions()
        if self.mode == "cluster":
            assert self.remote is not None
            return self.remote.sessions()
        return ()

    def _orphans(self) -> int:
        if self.registry is None or self.mode != "local":
            return 0
        try:
            return len(self.registry.orphans())
        except OSError:
            return 0

    def _entry(self, session: AccessSession) -> ForwardEntry | None:
        registration = self.query.registrations.get(session.application_id)
        if registration is not None and not self.query._allowed(
            registration.id, "access"
        ):
            return None
        stale_reason = None
        if registration is None and session.state in _ACTIVE:
            stale_reason = "scope-removed"
        elif session.state == "connecting" and session.reason == "forward-reconnecting":
            stale_reason = "reconnecting"
        url = (
            f"http://{session.endpoint}"
            if session.state == "ready" and session.endpoint
            else None
        )
        scope = None
        if registration is not None and self.scopes is not None:
            scope = self.scopes.scope(registration)
        return ForwardEntry(
            session=session,
            application_name=registration.name
            if registration is not None
            else "Removed scope",
            scope=scope,
            url=url,
            stale=stale_reason is not None,
            stale_reason=stale_reason,
        )

    def list(self) -> ForwardPage:
        self.query._principal()
        if self.mode == "unavailable":
            reason = (
                "kubectl-unavailable"
                if self.access is not None
                else "forwards-not-configured"
            )
            return ForwardPage(mode="unavailable", reason=reason, items=[])
        entries = [
            entry
            for entry in (self._entry(session) for session in self._sessions())
            if entry is not None
        ]
        order = {"ready": 0, "connecting": 1, "pending": 2}
        entries.sort(
            key=lambda item: (
                order.get(item.session.state, 3),
                item.application_name,
                item.session.resource.name,
            )
        )
        return ForwardPage(
            mode="local" if self.mode == "local" else "cluster",
            items=entries,
            orphans=self._orphans(),
        )

    def stale_count(self) -> int:
        page = self.list()
        return sum(item.stale for item in page.items) + page.orphans

    def stop_stale(self) -> StaleForwardResult:
        """Stop orphaned forwards and forwards of removed scopes; nothing else."""
        if self.mode != "local":
            raise QueryError("ui-operation-unavailable", 409)
        assert self.access is not None
        if not any(
            self.query._allowed(item, "access") for item in self.query.registrations
        ):
            raise QueryError("ui-request-rejected", 403)
        stopped = 0
        for session in self.access.sessions():
            if (
                session.application_id not in self.query.registrations
                and session.state in _ACTIVE
            ):
                self.access.stop_owned(session.id)
                stopped += 1
        if self.registry is not None:
            try:
                stopped += len(self.registry.reap_orphans())
            except OSError:
                pass
        return StaleForwardResult(stopped=stopped, orphans=self._orphans())
