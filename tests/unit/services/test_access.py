"""Local access admits only a live selected resource and owned ready probe."""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.services.access import AccessService
from piceli.services.contracts import AccessStartRequest
from piceli.services.query import QueryError, QueryService
from piceli.services.registration import Registration
from piceli.testing import fake_cluster, manifest


class Supervisor:
    instances: list[Supervisor] = []
    conflict = False

    def __init__(self, **_kwargs: object) -> None:
        self.started = False
        self.closed = False
        self.instances.append(self)

    def quick_start(self, _id: str, _namespace: str) -> None:
        self.started = True

    def statuses(self) -> tuple[SimpleNamespace, ...]:
        return (
            SimpleNamespace(
                state="failed" if self.conflict else "running",
                health="conflict" if self.conflict else "healthy",
                reachable=not self.conflict,
            ),
        )

    def close(self) -> None:
        self.closed = True


def test_live_scoped_service_forward_requires_owned_ready_probe(tmp_path: Path) -> None:
    Supervisor.instances.clear()
    Supervisor.conflict = False
    with fake_cluster() as cluster:
        service = manifest("Service", "api")
        service["spec"] = {
            "selector": {"app": "api"},
            "ports": [{"name": "http", "port": 8080, "targetPort": 8080}],
        }
        cluster.api.put(service)
        query = QueryService(
            [
                Registration(
                    id="shop",
                    name="Shop",
                    target=KubeconfigTarget(
                        kubeconfig=cluster.kubeconfig(tmp_path / "kubeconfig"),
                        context="fake",
                        namespace=cluster.namespace,
                        transport="loopback-http",
                    ),
                )
            ]
        )
        access = AccessService(
            query,
            kubectl=Path(sys.executable),
            supervisor_factory=Supervisor,  # type: ignore[arg-type]
        )
        try:
            selected = next(
                item
                for item in query.resources("shop").items
                if item.identity.kind == "Service"
            )
            assert selected.ports == [8080]
            assert selected.capabilities["access"].allowed
            body = AccessStartRequest(
                resource_id=selected.id,
                resource_uid=selected.identity.uid or "",
                local_port=18080,
                remote_port=8080,
            )
            with pytest.raises(QueryError, match="ui-invalid-request"):
                access.start("shop", body.model_copy(update={"remote_port": 9090}))
            with pytest.raises(QueryError, match="ui-observation-unavailable"):
                access.start("shop", body.model_copy(update={"resource_uid": "stale"}))
            ready = access.start("shop", body)
            assert ready.state == "ready"
            assert ready.binding_location == "server"
            assert ready.endpoint == "127.0.0.1:18080"
            assert Supervisor.instances[-1].started
            with pytest.raises(QueryError, match="ui-access-port-conflict"):
                access.start("shop", body)
            assert access.list("shop").items == [ready]
            assert access.stop("shop", ready.id).state == "stopped"
            assert Supervisor.instances[-1].closed
            with pytest.raises(QueryError, match="ui-not-found"):
                access.get("another", ready.id)
            Supervisor.conflict = True
            with pytest.raises(QueryError, match="ui-access-port-conflict"):
                access.start("shop", body)
            assert Supervisor.instances[-1].closed
            assert len(access.list("shop").items) == 1
            Supervisor.conflict = False
            second = access.start("shop", body)
            cluster.api.put(service, uid="replacement-uid")
            deadline = time.monotonic() + 3
            while (
                access.get("shop", second.id).state != "failed"
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            assert access.get("shop", second.id).state == "failed"
            assert Supervisor.instances[-1].closed
        finally:
            access.close()


def test_occupied_loopback_port_is_never_reported_ready(tmp_path: Path) -> None:
    with fake_cluster() as cluster, socket.socket() as occupied:
        service = manifest("Service", "api")
        service["spec"] = {"ports": [{"port": 8080, "targetPort": 8080}]}
        cluster.api.put(service)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        query = QueryService(
            [
                Registration(
                    id="shop",
                    name="Shop",
                    target=KubeconfigTarget(
                        kubeconfig=cluster.kubeconfig(tmp_path / "kubeconfig"),
                        context="fake",
                        namespace=cluster.namespace,
                        transport="loopback-http",
                    ),
                )
            ]
        )
        access = AccessService(query, kubectl=Path(sys.executable))
        try:
            selected = next(
                item
                for item in query.resources("shop").items
                if item.identity.kind == "Service"
            )
            with pytest.raises(QueryError, match="ui-access-port-conflict"):
                access.start(
                    "shop",
                    AccessStartRequest(
                        resource_id=selected.id,
                        resource_uid=selected.identity.uid or "",
                        local_port=port,
                        remote_port=8080,
                    ),
                )
            assert access.list("shop").items == []
        finally:
            access.close()


def test_ended_access_history_is_bounded_and_old_supervisors_close(
    tmp_path: Path,
) -> None:
    Supervisor.instances.clear()
    Supervisor.conflict = False
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
        access = AccessService(
            query,
            kubectl=Path(sys.executable),
            supervisor_factory=Supervisor,  # type: ignore[arg-type]
        )
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
            first_id = ""
            last_id = ""
            for index in range(130):
                session = access.start("shop", request)
                if index == 0:
                    first_id = session.id
                last_id = session.id
                assert access.stop("shop", session.id).state == "stopped"
            assert len(access.list("shop").items) == 128
            assert all(supervisor.closed for supervisor in Supervisor.instances)
            with pytest.raises(QueryError, match="ui-not-found"):
                access.get("shop", first_id)
            with access._lock:
                access._sessions[last_id].closed_at = (
                    time.monotonic() - access._history_seconds - 1
                )
            assert len(access.list("shop").items) == 127
            with pytest.raises(QueryError, match="ui-not-found"):
                access.get("shop", last_id)
        finally:
            access.close()
            query.close()
