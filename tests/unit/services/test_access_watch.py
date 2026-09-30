"""The lease watcher survives failures and never undoes a stop."""

from __future__ import annotations

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
from piceli.services.contracts import AccessSession, AccessStartRequest
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
        owned.record = owned.record.model_copy(
            update={
                "expires_at": (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
            }
        )


@contextmanager
def _service(
    tmp_path: Path, factory: type[AccessService] = AccessService
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
        self.calls = 0
        self.stopped: list[AccessSession] = []

    def _verified_resource(self, application_id: str, request: AccessStartRequest):  # type: ignore[no-untyped-def]
        result = super()._verified_resource(application_id, request)
        if threading.current_thread() is self.starter:
            self.calls += 1
            if self.calls == 2:  # the recheck after the forward became ready
                (pending,) = [
                    item
                    for item in self.list(application_id).items
                    if item.state == "connecting"
                ]
                stopper = threading.Thread(
                    target=lambda: self.stopped.append(
                        self.stop(application_id, pending.id)
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
