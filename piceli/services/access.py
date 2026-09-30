"""Owned loopback forwards for the local UI over explicitly registered targets."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from piceli.artifacts.process import ToolPin
from piceli.k8s.observe import ForwardSupervisor
from piceli.k8s.ui_config import UiShortcut
from piceli.services.contracts import (
    AccessPage,
    AccessSession,
    AccessStartRequest,
    Capability,
    Resource,
)
from piceli.services.query import KubernetesReader, QueryError, QueryService, _resource
from piceli.services.registration import Registration


@dataclass
class _Owned:
    record: AccessSession
    supervisor: ForwardSupervisor
    resource_id: str
    closed_at: float | None = None


class AccessService:
    """Supervise only forwards this service started; never adopt ambient ports."""

    _history_limit = 128
    _history_seconds = 600

    def __init__(
        self,
        query: QueryService,
        *,
        kubectl: Path | None,
        supervisor_factory: Callable[..., ForwardSupervisor] = ForwardSupervisor,
    ) -> None:
        self.query = query
        self.tool = ToolPin.capture(kubectl) if kubectl is not None else None
        self.supervisor_factory = supervisor_factory
        self._lock = threading.RLock()
        self._sessions: dict[str, _Owned] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        query.access_capability = self.capability

    def _prune_locked(self) -> None:
        """Keep recent terminal outcomes without retaining old supervisors forever."""
        cutoff = time.monotonic() - self._history_seconds
        ended = sorted(
            (
                (identity, owned.closed_at)
                for identity, owned in self._sessions.items()
                if owned.closed_at is not None
            ),
            key=lambda item: item[1],
        )
        excess = max(0, len(ended) - self._history_limit)
        for index, (identity, closed_at) in enumerate(ended):
            if index < excess or closed_at < cutoff:
                del self._sessions[identity]

    def capability(
        self, _registration: Registration, resource: Resource | None
    ) -> Capability:
        if self.tool is None:
            return Capability(allowed=False, reason="kubectl-unavailable")
        if resource is None:
            return Capability(allowed=True)
        allowed = (
            resource.identity.kind in {"Service", "Pod", "Deployment"}
            and bool(resource.ports)
            and resource.identity.uid is not None
        )
        return Capability(
            allowed=allowed, reason=None if allowed else "no-forwardable-port"
        )

    def _verified_resource(
        self, application_id: str, request: AccessStartRequest
    ) -> tuple[Registration, Resource]:
        self.query.registration(application_id, action="access")
        selected = self.query.resource(application_id, request.resource_id)
        if selected.identity.uid != request.resource_uid:
            raise QueryError("ui-observation-unavailable", 409)
        registration = self.query.registration(application_id)
        if not self.capability(registration, selected).allowed:
            raise QueryError("ui-operation-unavailable", 409)
        reader = KubernetesReader(registration)
        try:
            self.query._pin_target(registration, reader)
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
        except Exception:
            raise QueryError("ui-observation-unavailable", 503) from None
        finally:
            reader.close()
        if raw is None or raw.get("metadata", {}).get("uid") != selected.identity.uid:
            raise QueryError("ui-observation-unavailable", 409)
        live = _resource(registration, raw)
        if request.remote_port not in live.ports:
            raise QueryError("ui-invalid-request", 422)
        return registration, live

    def start(self, application_id: str, request: AccessStartRequest) -> AccessSession:
        if self.tool is None:
            raise QueryError("ui-operation-unavailable", 409)
        registration, resource = self._verified_resource(application_id, request)
        try:
            self.tool.verify()
        except (OSError, ValueError):
            raise QueryError("ui-operation-unavailable", 409) from None
        with self._lock:
            self._prune_locked()
            active = [
                owned.record
                for owned in self._sessions.values()
                if owned.record.state in {"connecting", "ready"}
            ]
            if len(active) >= 16:
                raise QueryError("ui-operation-conflict", 409)
            if any(item.local_port == request.local_port for item in active):
                raise QueryError("ui-access-port-conflict", 409)
            identity = uuid.uuid4().hex
            shortcut = UiShortcut(
                id="ui-" + identity,
                label="Local access",
                target=f"{resource.identity.kind.lower()}/{resource.identity.name}",
                namespace=resource.identity.namespace,
                local_port=request.local_port,
                remote_port=request.remote_port,
            )
            supervisor = self.supervisor_factory(
                kubeconfig=registration.target.kubeconfig,
                context=registration.target.context,
                kubectl=str(self.tool.path),
                shortcuts=(shortcut,),
                namespace=registration.target.namespace,
            )
            record = AccessSession(
                id=identity,
                application_id=application_id,
                resource=resource.identity,
                principal_id=self.query._principal().id,
                binding_location="server",
                state="connecting",
                expires_at=(
                    datetime.now(UTC) + timedelta(seconds=request.duration_seconds)
                ).isoformat(),
                local_port=request.local_port,
                remote_port=request.remote_port,
            )
            self._sessions[identity] = _Owned(record, supervisor, request.resource_id)
            if self._thread is None:
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._watch, name="piceli-ui-access", daemon=True
                )
                self._thread.start()
        try:
            supervisor.quick_start(shortcut.id, registration.target.namespace)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                status = supervisor.statuses()[0]
                if status.state == "running" and status.reachable:
                    # A name may be replaced while kubectl starts; do not claim
                    # the selected object if its UID no longer matches.
                    self._verified_resource(application_id, request)
                    ready = record.model_copy(
                        update={
                            "state": "ready",
                            "endpoint": f"127.0.0.1:{request.local_port}",
                        }
                    )
                    with self._lock:
                        self._sessions[identity].record = ready
                    return ready
                if status.state == "failed" or status.health == "conflict":
                    code = (
                        "ui-access-port-conflict"
                        if status.health == "conflict"
                        else "ui-access-failed"
                    )
                    raise QueryError(code, 409 if status.health == "conflict" else 503)
                time.sleep(0.1)
            raise QueryError("ui-access-failed", 503)
        except BaseException:
            supervisor.close()
            with self._lock:
                self._sessions.pop(identity, None)
            raise

    def _watch(self) -> None:
        while not self._stop.wait(1):
            with self._lock:
                self._prune_locked()
                ids = list(self._sessions)
            for id in ids:
                with self._lock:
                    owned = self._sessions.get(id)
                if owned is None or owned.record.state not in {"connecting", "ready"}:
                    continue
                if datetime.fromisoformat(owned.record.expires_at) <= datetime.now(UTC):
                    self._end(id, "expired")
                    continue
                status = owned.supervisor.statuses()[0]
                if status.state == "failed" or status.health == "conflict":
                    self._end(id, "failed")
                elif status.state != "running" or not status.reachable:
                    with self._lock:
                        if owned.record.state == "ready":
                            owned.record = owned.record.model_copy(
                                update={
                                    "state": "connecting",
                                    "endpoint": None,
                                    "reason": "forward-reconnecting",
                                }
                            )
                else:
                    request = AccessStartRequest(
                        resource_id=owned.resource_id,
                        resource_uid=owned.record.resource.uid or "",
                        local_port=owned.record.local_port or 1,
                        remote_port=owned.record.remote_port or 1,
                    )
                    try:
                        _, live = self._verified_resource(
                            owned.record.application_id, request
                        )
                    except QueryError:
                        self._end(id, "failed")
                        continue
                    if live.identity.uid != owned.record.resource.uid:
                        self._end(id, "failed")
                        continue
                    if owned.record.state == "connecting":
                        with self._lock:
                            owned.record = owned.record.model_copy(
                                update={
                                    "state": "ready",
                                    "endpoint": f"127.0.0.1:{request.local_port}",
                                    "reason": None,
                                }
                            )

    def _end(self, id: str, state: str) -> AccessSession:
        with self._lock:
            owned = self._sessions[id]
            if owned.record.state in {"stopped", "expired", "failed"}:
                return owned.record
            owned.record = owned.record.model_copy(
                update={"state": state, "endpoint": None}
            )
            owned.closed_at = time.monotonic()
        owned.supervisor.close()
        with self._lock:
            self._prune_locked()
        return owned.record

    def get(self, application_id: str, id: str) -> AccessSession:
        self.query.registration(application_id, action="access")
        with self._lock:
            self._prune_locked()
            owned = self._sessions.get(id)
            if (
                owned is None
                or owned.record.application_id != application_id
                or owned.record.principal_id != self.query._principal().id
            ):
                raise QueryError("ui-not-found")
            return owned.record

    def list(self, application_id: str) -> AccessPage:
        self.query.registration(application_id, action="access")
        with self._lock:
            self._prune_locked()
            return AccessPage(
                items=[
                    owned.record
                    for owned in self._sessions.values()
                    if owned.record.application_id == application_id
                    and owned.record.principal_id == self.query._principal().id
                ]
            )

    def stop(self, application_id: str, id: str) -> AccessSession:
        self.get(application_id, id)
        return self._end(id, "stopped")

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            ids = [
                id
                for id, owned in self._sessions.items()
                if owned.record.state in {"connecting", "ready"}
            ]
        for id in ids:
            self._end(id, "stopped")
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
