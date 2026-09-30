"""Query projections retain uncertainty and never expose private Kubernetes data."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from piceli.k8s.ops.provider_factory import ClusterIdentity, KubeconfigTarget
from piceli.services.authority import ScopePolicy, request_principal
from piceli.services.contracts import Principal
from piceli.services.query import KubernetesReader, QueryError, QueryService
from piceli.services.registration import Registration


def registration(name: str = "shop") -> Registration:
    return Registration(
        name,
        name,
        KubeconfigTarget(Path("/explicit/config"), "local", "shop"),
        kinds=(("v1", "Pod"), ("v1", "Secret")),
    )


class Reader:
    def __init__(self, _registration: Registration) -> None:
        self.closed = False

    def list(self, api_version: str, kind: str, namespace: str) -> list[dict[str, Any]]:
        if kind == "Secret":
            raise RuntimeError("private token must not escape")
        return [
            {
                "apiVersion": api_version,
                "kind": kind,
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "uid": name,
                    "annotations": {"private": "private token"},
                },
                "status": {
                    "phase": "Running",
                    "conditions": [
                        {"type": "Ready", "status": "False", "message": "private token"}
                    ],
                },
            }
            for name in ("api", "worker")
        ]

    def close(self) -> None:
        self.closed = True


def test_partial_inventory_redaction_and_snapshot_pagination() -> None:
    readers = []

    def factory(reg: Registration) -> Reader:
        reader = Reader(reg)
        readers.append(reader)
        return reader

    service = QueryService([registration()], reader_factory=factory, page_size=1)
    page = service.resources("shop")
    assert page.partial[0].scope == "v1/Secret"
    assert page.items[0].health == "progressing"
    assert page.items[0].relation == "unknown"
    assert "private token" not in page.model_dump_json()
    assert readers[0].closed
    assert page.next_page
    next_page = service.resources("shop", page.next_page)
    assert next_page.cursor == page.cursor
    assert next_page.items[0].identity.name == "worker"
    assert len(readers) == 1
    assert service.resource("shop", page.items[0].id).id == page.items[0].id


def test_scope_bound_snapshot_and_eviction() -> None:
    service = QueryService(
        [registration(), registration("other")],
        reader_factory=Reader,
        page_size=1,
        snapshot_limit=1,
    )
    page = service.resources("shop")
    assert page.next_page
    with pytest.raises(QueryError):
        service.resources("other", page.next_page)
    service.resources("shop")
    with pytest.raises(QueryError):
        service.resources("shop", page.next_page)


def test_internal_resource_rechecks_do_not_evict_browser_pagination() -> None:
    service = QueryService(
        [registration()], reader_factory=Reader, page_size=1, snapshot_limit=1
    )
    page = service.resources("shop")
    assert page.next_page
    for _ in range(5):
        assert service.resource("shop", page.items[0].id).id == page.items[0].id
    assert service.resources("shop", page.next_page).items[0].identity.name == "worker"


def test_cluster_principal_scopes_snapshots_and_revocation() -> None:
    alice = Principal(id="alice", name="Alice", kind="oidc")
    bob = Principal(id="bob", name="Bob", kind="oidc")
    policy = ScopePolicy(
        {
            "alice": {
                "shop": frozenset({"inspect"}),
                "other": frozenset({"inspect"}),
            },
            "bob": {"other": frozenset({"inspect"})},
        }
    )
    service = QueryService(
        [registration(), registration("other")],
        reader_factory=Reader,
        page_size=1,
        scope_policy=policy,
    )
    with pytest.raises(QueryError) as unauthenticated:
        service.applications()
    assert unauthenticated.value.status == 403
    with request_principal(alice):
        first = service.applications()
        assert first.items[0].id == "shop"
        assert first.next_page is not None
        assert len(service.capabilities().targets) == 1
        assert service.capabilities().principal == alice
        snapshot = service.resources("shop")
        assert snapshot.next_page is not None
    with request_principal(bob):
        assert [item.id for item in service.applications().items] == ["other"]
        with pytest.raises(QueryError):
            service.applications(first.next_page)
        with pytest.raises(QueryError):
            service.resources("shop", snapshot.next_page)
        with pytest.raises(QueryError):
            service.application("shop")
    policy.replace({"alice": {"other": frozenset({"inspect"})}})
    with request_principal(alice):
        with pytest.raises(QueryError):
            service.applications(first.next_page)
        with pytest.raises(QueryError):
            service.resources("shop", snapshot.next_page)
        with pytest.raises(QueryError):
            service.registration("other", action="deploy")
    service.close()


def test_application_does_not_claim_observation_or_deployment() -> None:
    service = QueryService([registration()], reader_factory=Reader)
    application = service.application("shop")
    assert application.health == application.relation == "unknown"
    assert application.freshness.state == "unavailable"
    assert not application.capabilities["deploy"].allowed
    assert service.saved_plans("shop") == []


def test_total_failure_is_unavailable_not_empty_success() -> None:
    def failed(_registration: Registration) -> Reader:
        raise RuntimeError("private credentials")

    page = QueryService([registration()], reader_factory=failed).resources("shop")
    assert not page.items
    assert page.freshness.state == "unavailable"
    assert page.partial[0].scope == "target"
    assert "private credentials" not in page.model_dump_json()


def test_observed_target_is_pinned_and_concurrent_retarget_refused() -> None:
    class IdentityReader(Reader):
        identity = ClusterIdentity("cluster-one", "namespace-one")

    service = QueryService([registration()], reader_factory=IdentityReader)
    service.resources("shop")
    target = service.application("shop").target
    assert target.cluster_uid == "cluster-one"
    assert target.namespace_uid == "namespace-one"
    IdentityReader.identity = ClusterIdentity("cluster-two", "namespace-one")
    page = service.resources("shop")
    assert page.items == []
    assert page.partial[0].scope == "target"
    assert service.application("shop").target == target


@pytest.mark.parametrize("constructor_fails", [False, True])
def test_dynamic_discovery_cache_is_removed_even_on_failure(
    monkeypatch: pytest.MonkeyPatch, constructor_fails: bool
) -> None:
    paths: list[Path] = []
    closed: list[bool] = []
    binding = SimpleNamespace(
        provider=SimpleNamespace(client=object()),
        identity=ClusterIdentity("cluster", "namespace"),
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(
        "piceli.services.query.build_provider", lambda *_args, **_kwargs: binding
    )

    def dynamic(_client: Any, *, cache_file: str) -> Any:
        path = Path(cache_file)
        path.write_text("private cached discovery")
        paths.append(path)
        if constructor_fails:
            raise ValueError("failed during discovery")
        return object()

    monkeypatch.setattr("kubernetes.dynamic.DynamicClient", dynamic)
    if constructor_fails:
        with pytest.raises(ValueError):
            KubernetesReader(registration())
    else:
        reader = KubernetesReader(registration())
        assert paths[0].is_file()
        reader.close()
    assert closed == [True]
    assert not paths[0].parent.exists()
