"""The lease watcher survives failures and never undoes a stop."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.services.access import AccessService
from piceli.services.contracts import AccessSession, AccessStartRequest, Resource
from piceli.services.query import QueryService
from piceli.services.registration import Registration
from piceli.testing import fake_cluster, manifest


class Supervisor:
    """A forward double: optional slow start and status failures on demand."""

    instances: list[Supervisor] = []
    gate: threading.Event | None = None
    entered: threading.Event | None = None

    def __init__(self, **_kwargs: object) -> None:
        self.started = False
        self.closed = False
        self.explode = False
        self.owner_resolver = _kwargs.get("owner_resolver")
        self.instances.append(self)

    def quick_start(self, _id: str, _namespace: str) -> None:
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10)
        self.started = True

    def statuses(self) -> tuple[SimpleNamespace, ...]:
        if self.explode:
            raise RuntimeError("status read failed")
        if not self.started:
            return ()
        return (SimpleNamespace(state="running", health="healthy", reachable=True),)

    def close(self) -> None:
        self.closed = True


class _CountingStop(threading.Event):
    """The watcher's stop event, ticking fast and counting its iterations."""

    def __init__(self) -> None:
        super().__init__()
        self.ticks = 0

    def wait(self, timeout: float | None = None) -> bool:
        self.ticks += 1
        return super().wait(0.01)


def _wait(condition: Callable[[], bool], seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


def _expire(access: AccessService, session_id: str) -> None:
    with access._lock:
        owned = access._sessions[session_id]
        owned.deadline = time.monotonic() - 1
        owned.record = owned.record.model_copy(
            update={
                "expires_at": (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
            }
        )


@contextmanager
def _service(
    tmp_path: Path,
    factory: type[AccessService] = AccessService,
    *,
    reader_factory: Any = None,
) -> Iterator[tuple[Any, AccessStartRequest, _CountingStop]]:
    Supervisor.instances.clear()
    Supervisor.gate = None
    Supervisor.entered = None
    with fake_cluster() as cluster:
        service = manifest("Service", "api")
        service["spec"] = {"ports": [{"port": 8080, "targetPort": 8080}]}
        cluster.api.put(service)
        query = QueryService(
            [
                Registration(
                    "shop",
                    "Shop",
                    KubeconfigTarget(
                        cluster.kubeconfig(tmp_path / "kubeconfig"),
                        "fake",
                        cluster.namespace,
                        transport="loopback-http",
                    ),
                )
            ]
        )
        access = factory(
            query,
            kubectl=Path(sys.executable),
            supervisor_factory=Supervisor,  # type: ignore[arg-type]
            **(
                {"workload_reader_factory": reader_factory}
                if reader_factory is not None
                else {}
            ),
        )
        stop = _CountingStop()
        access._stop = stop
        try:
            selected = next(
                item
                for item in query.resources("shop").items
                if item.identity.kind == "Service"
            )
            request = AccessStartRequest(
                resource_id=selected.id,
                resource_uid=selected.identity.uid or "",
                local_port=18080,
                remote_port=8080,
            )
            yield access, request, stop
        finally:
            access.close()
            query.close()


def test_service_forward_follows_ready_pod_and_releases_reader(tmp_path: Path) -> None:
    class Reader:
        instances: list[Reader] = []

        def __init__(self, **_kwargs: Any) -> None:
            self.closed = False
            self.instances.append(self)

        def workload(
            self, kind: str, _namespace: str, _name: str
        ) -> dict[str, Any] | None:
            return {"spec": {"selector": {"app": "api"}}} if kind == "Service" else None

        def pods(self, _namespace: str, _selector: Any) -> list[dict[str, Any]]:
            return [
                {
                    "metadata": {
                        "name": "pod-old",
                        "creationTimestamp": "2026-01-01T00:00:00Z",
                    },
                    "status": {
                        "phase": "Running",
                        "conditions": [{"type": "Ready", "status": "True"}],
                    },
                },
                {
                    "metadata": {
                        "name": "pod-new",
                        "creationTimestamp": "2026-01-02T00:00:00Z",
                    },
                    "status": {
                        "phase": "Running",
                        "conditions": [{"type": "Ready", "status": "True"}],
                    },
                },
            ]

        def close(self) -> None:
            self.closed = True

    Reader.instances.clear()
    with _service(tmp_path, reader_factory=Reader) as (access, request, _stop):
        session = access.start("shop", request)
        resolver = Supervisor.instances[-1].owner_resolver
        assert callable(resolver)
        assert resolver("default", "service/api") == ["pod-new", "pod-old"]
        access.stop("shop", session.id)
        assert Reader.instances[-1].closed


def test_close_has_one_shared_teardown_deadline(tmp_path: Path) -> None:
    with _service(tmp_path) as (access, request, _stop):
        first = access.start("shop", request)
        second = access.start("shop", request.model_copy(update={"local_port": 18081}))
        gate = threading.Event()
        original = [instance.close for instance in Supervisor.instances]
        for instance, close in zip(Supervisor.instances, original, strict=True):
            instance.close = lambda close=close: (gate.wait(5), close())  # type: ignore[method-assign]
        access._close_timeout = 0.1
        started = time.monotonic()
        access.close()
        # Each blocked close waits up to 5 s on the gate; returning well
        # before that proves one shared 0.1 s deadline (wide for slow runners).
        assert time.monotonic() - started < 3
        assert access.get("shop", first.id).state == "stopped"
        assert access.get("shop", second.id).state == "stopped"
        gate.set()
        _wait(lambda: all(item.closed for item in Supervisor.instances))


def test_mixed_churn_keeps_watcher_live_and_bounded(tmp_path: Path) -> None:
    """Parallel leases, slow starts, failures and expiry share one watcher."""
    cycles = int(os.environ.get("PICELI_UI_MIXED_CHURN_CYCLES", "30"))
    assert 1 <= cycles <= 5000
    with _service(tmp_path) as (access, request, stop):
        ports = (18080, 18081, 18082)
        active = {
            port: access.start("shop", request.model_copy(update={"local_port": port}))
            for port in ports
        }
        for index in range(cycles):
            port = ports[index % len(ports)]
            session = active[port]
            if index % 3 == 0:
                access.stop("shop", session.id)
                expected = "stopped"
            elif index % 3 == 1:
                _expire(access, session.id)
                expected = "expired"
            else:
                with access._lock:
                    access._sessions[session.id].supervisor.explode = True  # type: ignore[attr-defined]
                expected = "failed"
            _wait(
                lambda session_id=session.id, state=expected: (
                    access.get("shop", session_id).state == state
                )
            )
            before = stop.ticks
            if index % 10 == 0:
                Supervisor.gate = threading.Event()
                Supervisor.entered = threading.Event()
                result: list[AccessSession] = []
                worker = threading.Thread(
                    target=lambda target_port=port, sink=result: sink.append(
                        access.start(
                            "shop",
                            request.model_copy(update={"local_port": target_port}),
                        )
                    )
                )
                worker.start()
                assert Supervisor.entered.wait(5)
                _wait(lambda prior=before: stop.ticks >= prior + 3)
                Supervisor.gate.set()
                worker.join(10)
                Supervisor.gate = None
                Supervisor.entered = None
                assert result and result[0].state == "ready"
                active[port] = result[0]
            else:
                active[port] = access.start(
                    "shop", request.model_copy(update={"local_port": port})
                )
            assert access._thread is not None and access._thread.is_alive()
            assert len(access._sessions) <= access._history_limit + len(ports)
        for session in active.values():
            access.stop("shop", session.id)
        assert all(
            owned.record.state in {"stopped", "expired", "failed"}
            for owned in access._sessions.values()
        )


def test_a_slow_forward_start_does_not_kill_the_lease_watcher(tmp_path: Path) -> None:
    with _service(tmp_path) as (access, request, stop):
        # A first session keeps the watcher running during the slow start.
        first = access.start("shop", request.model_copy(update={"local_port": 18081}))
        Supervisor.gate = threading.Event()
        Supervisor.entered = threading.Event()
        started: list[AccessSession] = []
        worker = threading.Thread(
            target=lambda: started.append(access.start("shop", request))
        )
        worker.start()
        assert Supervisor.entered.wait(5)
        # The watcher runs several times while kubectl is still starting.
        before = stop.ticks
        _wait(lambda: stop.ticks >= before + 5)
        Supervisor.gate.set()
        worker.join(10)
        assert started and started[0].state == "ready"
        assert access._thread is not None and access._thread.is_alive()
        _expire(access, started[0].id)
        _wait(lambda: access.get("shop", started[0].id).state == "expired")
        assert Supervisor.instances[-1].closed
        assert access.get("shop", first.id).state == "ready"


def test_one_failing_session_does_not_stop_expiry_of_the_others(
    tmp_path: Path,
) -> None:
    with _service(tmp_path) as (access, request, _stop):
        broken = access.start("shop", request)
        healthy = access.start("shop", request.model_copy(update={"local_port": 18081}))
        Supervisor.instances[0].explode = True
        _expire(access, healthy.id)
        _wait(lambda: access.get("shop", healthy.id).state == "expired")
        # A session that can no longer be supervised is ended, not kept open.
        _wait(lambda: access.get("shop", broken.id).state == "failed")
        assert Supervisor.instances[0].closed
        assert access._thread is not None and access._thread.is_alive()
        later = access.start("shop", request.model_copy(update={"local_port": 18082}))
        _expire(access, later.id)
        _wait(lambda: access.get("shop", later.id).state == "expired")


class _StopDuringRecheck(AccessService):
    """Stop the session from another thread while start() re-verifies its UID."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.starter: threading.Thread | None = None
        self.stopped: list[AccessSession] = []

    def _recheck_resource(
        self,
        registration: Registration,
        selected: Resource,
        request: AccessStartRequest,
    ) -> Resource:
        result = super()._recheck_resource(registration, selected, request)
        if threading.current_thread() is self.starter:
            # The watcher may mark it ready before the starter finishes its
            # UID recheck. Stop either live state at this point; the starter
            # must not resurrect it afterward.
            (pending,) = [
                item
                for item in self.list(registration.id).items
                if item.state in {"connecting", "ready"}
            ]
            stopper = threading.Thread(
                target=lambda: self.stopped.append(
                    self.stop(registration.id, pending.id)
                )
            )
            stopper.start()
            stopper.join(10)
        return result


def test_a_stop_during_the_uid_recheck_is_not_overwritten(tmp_path: Path) -> None:
    with _service(tmp_path, _StopDuringRecheck) as (access, request, stop):
        access.starter = threading.current_thread()
        result = access.start("shop", request)
        assert [item.state for item in access.stopped] == ["stopped"]
        assert result.state == "stopped"
        assert result.endpoint is None
        assert access.get("shop", result.id).state == "stopped"
        assert Supervisor.instances[-1].closed
        before = stop.ticks
        _wait(lambda: stop.ticks >= before + 5)
        assert access.get("shop", result.id).state == "stopped"
        # The stopped session holds neither the port nor an active slot.
        access.starter = None
        again = access.start("shop", request)
        assert again.state == "ready"


def test_the_history_bound_never_evicts_an_active_session(tmp_path: Path) -> None:
    with _service(tmp_path) as (access, request, _stop):
        active = access.start("shop", request)
        with access._lock:
            # Even with a stale closing mark, a non-terminal record stays.
            access._sessions[active.id].closed_at = (
                time.monotonic() - access._history_seconds - 1
            )
        assert access.get("shop", active.id).state == "ready"
