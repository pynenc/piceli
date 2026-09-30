"""Owned loopback forwards for the local UI over explicitly registered targets."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from piceli.artifacts.process import ToolPin
from piceli.k8s.observe import ForwardSupervisor
from piceli.k8s.owned_processes import OwnedProcessRegistry
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

_log = logging.getLogger("piceli.ui.access")
_ACTIVE = frozenset({"connecting", "ready"})
_TERMINAL = frozenset({"stopped", "expired", "failed"})


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
    _active_limit = 16
    _watch_interval = 1.0

    def __init__(
        self,
        query: QueryService,
        *,
        kubectl: Path | None,
        supervisor_factory: Callable[..., ForwardSupervisor] = ForwardSupervisor,
        registry: OwnedProcessRegistry | None = None,
    ) -> None:
        self.query = query
        self.tool = ToolPin.capture(kubectl) if kubectl is not None else None
        self.supervisor_factory = supervisor_factory
        self.registry = registry
        self._lock = threading.RLock()
        self._sessions: dict[str, _Owned] = {}
        # Local ports of forwards being started: counted as active, but not
        # published until their supervisor has registered the forward.
        self._starting: dict[str, int] = {}
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
                # Only ended sessions count; an active one is never evicted.
                if owned.closed_at is not None and owned.record.state in _TERMINAL
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
        if request.remote_port not in selected.ports:
            raise QueryError("ui-invalid-request", 422)
        return registration, selected

    def _recheck_resource(
        self,
        registration: Registration,
        selected: Resource,
        request: AccessStartRequest,
    ) -> Resource:
        """Recheck the selected kind after kubectl starts, without a full inventory scan."""
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
        return live

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
            active_ports = [
                owned.record.local_port
                for owned in self._sessions.values()
                if owned.record.state in {"connecting", "ready"}
            ] + list(self._starting.values())
            if len(active_ports) >= self._active_limit:
                raise QueryError("ui-operation-conflict", 409)
            if request.local_port in active_ports:
                raise QueryError("ui-access-port-conflict", 409)
            identity = uuid.uuid4().hex
            self._starting[identity] = request.local_port
        shortcut = UiShortcut(
            id="ui-" + identity,
            label="Local access",
            target=f"{resource.identity.kind.lower()}/{resource.identity.name}",
            namespace=resource.identity.namespace,
            local_port=request.local_port,
            remote_port=request.remote_port,
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
        supervisor: ForwardSupervisor | None = None
        try:
            supervisor = self.supervisor_factory(
                kubeconfig=registration.target.kubeconfig,
                context=registration.target.context,
                kubectl=str(self.tool.path),
                shortcuts=(shortcut,),
                namespace=registration.target.namespace,
                **({"registry": self.registry} if self.registry is not None else {}),
            )
            # Register (and start) the forward before the watcher can see the
            # record, so a published session always has a supervised status.
            supervisor.quick_start(shortcut.id, registration.target.namespace)
            with self._lock:
                self._starting.pop(identity, None)
                self._sessions[identity] = _Owned(
                    record, supervisor, request.resource_id
                )
                self._ensure_watcher_locked()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                with self._lock:
                    owned = self._sessions[identity]
                    if owned.record.state != "connecting":
                        return owned.record  # stopped, expired or already ready
                status = self._status(supervisor)
                if (
                    status is not None
                    and status.state == "running"
                    and status.reachable
                ):
                    # A name may be replaced while kubectl starts; do not claim
                    # the selected object if its UID no longer matches.
                    self._recheck_resource(registration, resource, request)
                    with self._lock:
                        # Compare and set: a Stop that landed during the check
                        # wins; a stopped session is never re-activated.
                        self._transition_locked(
                            owned,
                            "connecting",
                            {
                                "state": "ready",
                                "endpoint": f"127.0.0.1:{request.local_port}",
                            },
                        )
                        return owned.record
                if status is not None and (
                    status.state == "failed" or status.health == "conflict"
                ):
                    code = (
                        "ui-access-port-conflict"
                        if status.health == "conflict"
                        else "ui-access-failed"
                    )
                    raise QueryError(code, 409 if status.health == "conflict" else 503)
                time.sleep(0.1)
            raise QueryError("ui-access-failed", 503)
        except BaseException:
            if supervisor is not None:
                supervisor.close()
            with self._lock:
                self._starting.pop(identity, None)
                owned_now = self._sessions.get(identity)
                if owned_now is not None and owned_now.record.state in _ACTIVE:
                    del self._sessions[identity]
            raise

    @staticmethod
    def _transition_locked(
        owned: _Owned, expected: str, update: dict[str, Any]
    ) -> bool:
        """Move ``owned`` to ``update`` only from ``expected`` and while not ended."""
        if owned.record.state != expected or owned.closed_at is not None:
            return False
        owned.record = owned.record.model_copy(update=update)
        return True

    def _ensure_watcher_locked(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._watch, name="piceli-ui-access", daemon=True
            )
            self._thread.start()

    @staticmethod
    def _status(supervisor: ForwardSupervisor) -> Any:
        statuses = supervisor.statuses()
        return statuses[0] if statuses else None

    def _watch(self) -> None:
        while not self._stop.wait(self._watch_interval):
            try:
                self._tick()
            except Exception as error:  # never let supervision end silently
                _log.warning(
                    "access supervision step failed (%s)", type(error).__name__
                )

    def _tick(self) -> None:
        """One supervision pass; a failure in one session never skips the others."""
        with self._lock:
            self._prune_locked()
            ids = list(self._sessions)
        for id in ids:
            try:
                self._supervise(id)
            except Exception as error:
                # A session that cannot be supervised is ended rather than left
                # holding a tunnel with no lease enforcement.
                _log.warning(
                    "access session supervision failed (%s)", type(error).__name__
                )
                try:
                    self._end(id, "failed")
                except Exception as end_error:
                    _log.warning(
                        "access session could not be ended (%s)",
                        type(end_error).__name__,
                    )

    def _supervise(self, id: str) -> None:
        with self._lock:
            owned = self._sessions.get(id)
        if owned is None or owned.record.state not in _ACTIVE:
            return
        if datetime.fromisoformat(owned.record.expires_at) <= datetime.now(UTC):
            self._end(id, "expired")
            return
        status = self._status(owned.supervisor)
        if status is None:
            return  # registered but not yet reporting: still connecting
        if status.state == "failed" or status.health == "conflict":
            self._end(id, "failed")
        elif status.state != "running" or not status.reachable:
            with self._lock:
                self._transition_locked(
                    owned,
                    "ready",
                    {
                        "state": "connecting",
                        "endpoint": None,
                        "reason": "forward-reconnecting",
                    },
                )
        else:
            request = AccessStartRequest(
                resource_id=owned.resource_id,
                resource_uid=owned.record.resource.uid or "",
                local_port=owned.record.local_port or 1,
                remote_port=owned.record.remote_port or 1,
            )
            try:
                _, live = self._verified_resource(owned.record.application_id, request)
            except QueryError:
                self._end(id, "failed")
                return
            if live.identity.uid != owned.record.resource.uid:
                self._end(id, "failed")
                return
            with self._lock:
                self._transition_locked(
                    owned,
                    "connecting",
                    {
                        "state": "ready",
                        "endpoint": f"127.0.0.1:{request.local_port}",
                        "reason": None,
                    },
                )

    def _end(self, id: str, state: str) -> AccessSession:
        with self._lock:
            owned = self._sessions.get(id)
            if owned is None:
                raise QueryError("ui-not-found")
            if owned.record.state in _TERMINAL or owned.closed_at is not None:
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
