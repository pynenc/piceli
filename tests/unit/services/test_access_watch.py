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
