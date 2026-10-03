"""Short-lived tickets for a port forward owned by an authenticated local client.

The cluster service never binds the user's laptop port. A browser authorizes a
resource-scoped pending ticket; a client claims it once, verifies the target
with its own explicit credentials, and reports its independently probed
loopback listener. Tickets and leases are ephemeral and never contain a
Kubernetes credential.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from piceli.services.authority import request_principal
from piceli.services.contracts import (
    AccessPage,
    AccessSession,
    Capability,
    Principal,
    RemoteAccessClaimRequest,
    RemoteAccessHeartbeatRequest,
    RemoteAccessLease,
    RemoteAccessReleaseRequest,
    RemoteAccessStartRequest,
    RemoteAccessTicket,
    Resource,
)
from piceli.services.query import QueryError, QueryService, _resource
from piceli.services.registration import Registration

_ACTIVE = frozenset({"pending", "connecting", "ready"})


def _secret_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass
class _Ticket:
    session: AccessSession
    resource_id: str
    pairing_digest: str | None
    claim_deadline: float
    lease_digest: str | None = None
    last_heartbeat: float | None = None
    ended_at: float | None = None


class RemoteAccessService:
    """A bounded, in-memory access lease; the local client owns all processes."""

    active_limit = 32
    history_limit = 128
    claim_seconds = 120
    heartbeat_seconds = 15
    history_seconds = 600

    def __init__(self, query: QueryService) -> None:
        if query.scope_policy is None:
            raise ValueError("remote access requires a scoped cluster service")
        self.query = query
        self._lock = threading.RLock()
        self._tickets: dict[str, _Ticket] = {}
        query.access_capability = self.capability

    @staticmethod
    def capability(
        _registration: Registration, resource: Resource | None
    ) -> Capability:
        if resource is None:
            return Capability(allowed=True)
        allowed = (
            resource.identity.kind in {"Service", "Pod", "Deployment"}
            and resource.identity.uid is not None
            and bool(resource.ports)
        )
        return Capability(
            allowed=allowed, reason=None if allowed else "no-forwardable-port"
        )

    def _verified_resource(
        self, application_id: str, resource_id: str, resource_uid: str, remote_port: int
    ) -> tuple[Registration, Resource]:
        registration = self.query.registration(application_id, action="access")
        selected = self.query.resource(application_id, resource_id)
        if (
            selected.identity.uid != resource_uid
            or not self.capability(registration, selected).allowed
            or remote_port not in selected.ports
        ):
            raise QueryError("ui-observation-unavailable", 409)
        reader = self.query.reader_factory(registration)
        try:
            registration = self.query._pin_target(registration, reader)
            raw = next(
                (
                    item
                    for item in reader.list(
                        selected.identity.api_version,
                        selected.identity.kind,
                        registration.target.namespace,
                    )
                    if item.get("metadata", {}).get("name") == selected.identity.name
                ),
                None,
            )
        except QueryError:
            raise
        except Exception:
            raise QueryError("ui-observation-unavailable", 503) from None
        finally:
            reader.close()
        if raw is None or raw.get("metadata", {}).get("uid") != resource_uid:
            raise QueryError("ui-observation-unavailable", 409)
        live = _resource(registration, raw)
        if remote_port not in live.ports:
            raise QueryError("ui-observation-unavailable", 409)
        return registration, live

    def _end(self, ticket: _Ticket, state: str, reason: str) -> None:
        if ticket.session.state not in _ACTIVE:
            return
        ticket.session = ticket.session.model_copy(
            update={"state": state, "endpoint": None, "reason": reason}
        )
        ticket.pairing_digest = None
        ticket.lease_digest = None
        ticket.ended_at = time.monotonic()

    def _sweep(self) -> None:
        now = time.monotonic()
        for ticket in self._tickets.values():
            if ticket.session.state not in _ACTIVE:
                continue
            if datetime.fromisoformat(ticket.session.expires_at) <= datetime.now(UTC):
                self._end(ticket, "expired", "session-expired")
            elif ticket.session.state == "pending" and now >= ticket.claim_deadline:
                self._end(ticket, "expired", "pairing-expired")
            elif (
                ticket.last_heartbeat is not None
                and now - ticket.last_heartbeat > self.heartbeat_seconds
            ):
                self._end(ticket, "failed", "client-heartbeat-lost")
        terminal = [
            (id, item.ended_at)
            for id, item in self._tickets.items()
            if item.ended_at is not None
        ]
        terminal.sort(key=lambda value: value[1])
        excess = max(0, len(terminal) - self.history_limit)
        for index, (id, ended) in enumerate(terminal):
            if index < excess or (
                ended is not None and now - ended >= self.history_seconds
            ):
                del self._tickets[id]

    def issue(
        self, application_id: str, request: RemoteAccessStartRequest
    ) -> RemoteAccessTicket:
        registration, live = self._verified_resource(
            application_id,
            request.resource_id,
            request.resource_uid,
            request.remote_port,
        )
        if not registration.target.cluster_uid or not registration.target.namespace_uid:
            raise QueryError("ui-observation-unavailable", 503)
        principal_id = self.query._principal().id
        with self._lock:
            self._sweep()
            if (
                sum(item.session.state in _ACTIVE for item in self._tickets.values())
                >= self.active_limit
            ):
                raise QueryError("ui-operation-conflict", 409)
            ticket_id = uuid.uuid4().hex
            pairing_secret = secrets.token_urlsafe(32)
            session = AccessSession(
                id=ticket_id,
                application_id=application_id,
                resource=live.identity,
                principal_id=principal_id,
                binding_location="local_client",
                state="pending",
                expires_at=(
                    datetime.now(UTC) + timedelta(seconds=request.duration_seconds)
                ).isoformat(),
                remote_port=request.remote_port,
            )
            self._tickets[ticket_id] = _Ticket(
                session=session,
                resource_id=request.resource_id,
                pairing_digest=_secret_digest(pairing_secret),
                claim_deadline=time.monotonic() + self.claim_seconds,
            )
        return RemoteAccessTicket(session=session, pairing_secret=pairing_secret)

    def _browser_ticket(self, application_id: str, id: str) -> _Ticket:
        self.query.registration(application_id, action="access")
        ticket = self._tickets.get(id)
        if (
            ticket is None
            or ticket.session.application_id != application_id
            or ticket.session.principal_id != self.query._principal().id
        ):
            raise QueryError("ui-not-found")
        return ticket

    def list(self, application_id: str) -> AccessPage:
        self.query.registration(application_id, action="access")
        principal_id = self.query._principal().id
        with self._lock:
            self._sweep()
            return AccessPage(
                items=[
                    item.session
                    for item in self._tickets.values()
                    if item.session.application_id == application_id
                    and item.session.principal_id == principal_id
                ]
            )

    def sessions(self) -> tuple[AccessSession, ...]:
        """This principal's tickets in every scope it may still access."""
        principal_id = self.query._principal().id
        with self._lock:
            self._sweep()
            return tuple(
                item.session
                for item in self._tickets.values()
                if item.session.principal_id == principal_id
                and self.query._allowed(item.session.application_id, "access")
            )

    def get(self, application_id: str, id: str) -> AccessSession:
        with self._lock:
            self._sweep()
            return self._browser_ticket(application_id, id).session

    def stop(self, application_id: str, id: str) -> AccessSession:
        with self._lock:
            self._sweep()
            ticket = self._browser_ticket(application_id, id)
            self._end(ticket, "stopped", "operator-stopped")
            return ticket.session

    def _client_ticket(self, id: str, secret: str, *, claim: bool) -> _Ticket:
        self._sweep()
        ticket = self._tickets.get(id)
        if ticket is None or ticket.session.state not in _ACTIVE:
            raise QueryError("ui-not-found")
        expected = ticket.pairing_digest if claim else ticket.lease_digest
        if expected is None or not hmac.compare_digest(
            expected, _secret_digest(secret)
        ):
            raise QueryError("ui-not-found")
        policy = self.query.scope_policy
        assert policy is not None
        if not policy.allows(
            ticket.session.principal_id, ticket.session.application_id, "access"
        ):
            self._end(ticket, "stopped", "scope-revoked")
            raise QueryError("ui-not-found")
        return ticket

    def _recheck_ticket(self, ticket: _Ticket) -> Registration:
        # A claimed ticket carries an actor that was authorized at admission.
        # The live policy is checked before entering this temporary context.
        principal = Principal(
            id=ticket.session.principal_id, name="Local client", kind="oidc"
        )
        with request_principal(principal):
            registration, _ = self._verified_resource(
                ticket.session.application_id,
                ticket.resource_id,
                ticket.session.resource.uid or "",
                ticket.session.remote_port or 0,
            )
        return registration

    def claim(self, id: str, request: RemoteAccessClaimRequest) -> RemoteAccessLease:
        with self._lock:
            ticket = self._client_ticket(id, request.pairing_secret, claim=True)
            if ticket.session.state != "pending":
                raise QueryError("ui-not-found")
            registration = self._recheck_ticket(ticket)
            target = registration.public_target()
            if not target.cluster_uid or not target.namespace_uid:
                raise QueryError("ui-observation-unavailable", 503)
            lease_secret = secrets.token_urlsafe(32)
            ticket.pairing_digest = None
            ticket.lease_digest = _secret_digest(lease_secret)
            ticket.last_heartbeat = time.monotonic()
            ticket.session = ticket.session.model_copy(update={"state": "connecting"})
            return RemoteAccessLease(
                session=ticket.session, target=target, lease_secret=lease_secret
            )

    def heartbeat(
        self, id: str, request: RemoteAccessHeartbeatRequest
    ) -> AccessSession:
        with self._lock:
            ticket = self._client_ticket(id, request.lease_secret, claim=False)
            if ticket.session.state not in {"connecting", "ready"}:
                raise QueryError("ui-not-found")
            if request.state == "ready" and request.local_port is None:
                raise QueryError("ui-invalid-request", 422)
            self._recheck_ticket(ticket)
            ticket.last_heartbeat = time.monotonic()
            if request.state == "failed":
                self._end(ticket, "failed", "client-reported-failure")
            elif request.state == "connecting":
                ticket.session = ticket.session.model_copy(
                    update={
                        "state": "connecting",
                        "endpoint": None,
                        "reason": None,
                    }
                )
            else:
                ticket.session = ticket.session.model_copy(
                    update={
                        "state": "ready",
                        "local_port": request.local_port,
                        "endpoint": f"127.0.0.1:{request.local_port}",
                        "reason": None,
                    }
                )
            return ticket.session

    def release(self, id: str, request: RemoteAccessReleaseRequest) -> AccessSession:
        with self._lock:
            ticket = self._client_ticket(id, request.lease_secret, claim=False)
            self._end(ticket, "stopped", "client-released")
            return ticket.session

    def close(self) -> None:
        with self._lock:
            for ticket in self._tickets.values():
                self._end(ticket, "stopped", "service-stopped")
            self._tickets.clear()
