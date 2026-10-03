"""Forwards across scopes: stale detection and tidying never touch foreign forwards."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.services.access import AccessService
from piceli.services.contracts import AccessStartRequest
from piceli.services.forwards import ForwardWorkspace
from piceli.services.navigation import NavigationService, registry_warnings
from piceli.services.query import QueryError, QueryService
from piceli.services.registration import Registration
from piceli.testing import fake_cluster, manifest


class Supervisor:
    reachable = True

    def __init__(self, **_kwargs: object) -> None:
        self.closed = False

    def quick_start(self, _id: str, _namespace: str) -> None:
        pass

    def statuses(self) -> tuple[SimpleNamespace, ...]:
        return (
            SimpleNamespace(
                state="running", health="healthy", reachable=self.reachable
            ),
        )

    def close(self) -> None:
        self.closed = True


def test_removed_scope_forward_is_stale_and_only_it_is_stopped(tmp_path: Path) -> None:
    with fake_cluster() as cluster:
        service = manifest("Service", "api")
        service["spec"] = {"ports": [{"port": 8080}]}
        cluster.api.put(service)
        target = KubeconfigTarget(
            cluster.kubeconfig(tmp_path / "kubeconfig"),
            "fake",
            cluster.namespace,
            transport="loopback-http",
        )
        query = QueryService(
            [Registration("shop", "Shop", target), Registration("env-a", "a", target)]
        )
        access = AccessService(
            query,
            kubectl=Path(sys.executable),
            supervisor_factory=Supervisor,  # type: ignore[arg-type]
        )
        forwards = ForwardWorkspace(query, access=access)
        try:
            for scope, port in (("shop", 18081), ("env-a", 18082)):
                selected = next(
                    item
                    for item in query.resources(scope).items
                    if item.identity.kind == "Service"
                )
                access.start(
                    scope,
                    AccessStartRequest(
                        resource_id=selected.id,
                        resource_uid=selected.identity.uid or "",
                        local_port=port,
                        remote_port=8080,
                    ),
                )
            page = forwards.list()
            assert [item.stale for item in page.items] == [False, False]
            query.remove_local("env-a")
            page = forwards.list()
            stale = [item for item in page.items if item.stale]
            assert [(item.application_name, item.stale_reason) for item in stale] == [
                ("Removed scope", "scope-removed")
            ]
            assert forwards.stale_count() == 1
            assert forwards.stop_stale().stopped == 1
            states = {
                item.session.application_id: item.session.state
                for item in forwards.list().items
            }
            assert states == {"shop": "ready", "env-a": "stopped"}
        finally:
            access.close()


def test_unconfigured_forwards_and_navigation_counts() -> None:
    query = QueryService([])
    forwards = ForwardWorkspace(query)
    assert forwards.list().mode == "unavailable"
    with pytest.raises(QueryError):
        forwards.stop_stale()
    summary = NavigationService(query, forwards=forwards).summary()
    assert summary.stale_forwards is None and summary.approvals is None
    assert registry_warnings({"registry": {"state": "ready"}, "nodes": []}) == 0
    assert (
        registry_warnings(
            {
                "registry": {"state": "degraded"},
                "nodes": [
                    {"mirror": {"state": "needs-restart"}},
                    {"mirror": {"state": "ready"}},
                ],
            }
        )
        == 2
    )
    assert registry_warnings({"nodes": [{"mirror": {"state": "missing"}}]}) == 0
