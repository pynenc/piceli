"""Stored plans remain discoverable independently of execution and expiration."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.services.authority import ScopePolicy, request_principal
from piceli.services.contracts import PlanRecord, Principal
from piceli.services.evaluation import DockerEvaluator
from piceli.services.operations import OperationService
from piceli.services.query import QueryError, QueryService
from piceli.services.registration import Registration
from piceli.services.store import Store


def archive_service(directory: Path, *, scoped: bool = False) -> OperationService:
    policy = (
        ScopePolicy(
            {
                "viewer": {
                    "shop": frozenset({"activity"}),
                    "other": frozenset({"activity"}),
                },
                "another": {"shop": frozenset({"activity"})},
                "inspector": {"shop": frozenset({"inspect", "plan"})},
            }
        )
        if scoped
        else None
    )
    query = QueryService(
        [
            Registration(
                name, name, KubeconfigTarget(directory / "unused", "test", name)
            )
            for name in ("shop", "other")
        ],
        scope_policy=policy,
    )
    evaluator = Mock(spec=DockerEvaluator)
    evaluator.available.return_value = False
    return OperationService(
        query,
        Store(directory / "control.sqlite"),
        evaluator,
        {},
        principal=None if scoped else Principal(id="local", name="Local operator"),
    )


def stored_plan(
    service: OperationService,
    id: str,
    created: str,
    *,
    application: str = "shop",
) -> PlanRecord:
    plan = PlanRecord(
        id=id,
        application_id=application,
        digest="a" * 64,
        target=service.query.registrations[application].public_target(),
        source=None,
        intent="deploy",
        release="archived-release",
        expires_at="2020-01-01T00:00:00Z",
        summary={"update": 1},
        diffs=[],
        actions=[{"manifest": {"unlisted": "manifest-body"}}],
    )
    service.store.put(
        "plan",
        plan.model_dump(mode="json"),
        private={"frozen": {"token": "private-frozen-value"}},
    )
    # Creation is store metadata; immutable older PlanRecord JSON has no date.
    with service.store.transaction() as connection:
        connection.execute(
            "UPDATE records SET created=? WHERE kind='plan' AND id=?", (created, id)
        )
    return plan


def test_archive_lists_expired_and_unused_plans_without_full_records(
    tmp_path: Path,
) -> None:
    service = archive_service(tmp_path)
    old = stored_plan(service, "unused-expired", "2020-01-01T00:00:00Z")
    stored_plan(service, "newer", "2026-10-01T10:00:00Z")
    stored_plan(service, "foreign", "2026-10-02T10:00:00Z", application="other")
    before = service.store.cursor()
    page = service.plans("shop")
    assert [item.id for item in page.items] == ["newer", "unused-expired"]
    assert page.next_page is None
    archived = page.items[-1]
    assert archived.digest == old.digest and archived.expires_at == old.expires_at
    assert archived.created_at == "2020-01-01T00:00:00Z"
    assert archived.source is None and archived.plan_kind == "release"
    assert archived.desired_resources_complete is False
    assert service.store.records("operation") == []
    public = page.model_dump_json()
    for unavailable in (
        "private-frozen-value",
        "manifest-body",
        '"diffs"',
        '"actions"',
        '"desired_resources"',
    ):
        assert unavailable not in public
    assert service.store.cursor() == before
    assert service.store.get("plan", old.id)[0] == old.model_dump(mode="json")


def test_archive_keyset_is_stable_at_equal_times_and_during_new_insertions(
    tmp_path: Path,
) -> None:
    service = archive_service(tmp_path)
    for id, created in (
        ("old", "2020"),
        ("a-tie", "2025"),
        ("z-tie", "2025"),
        ("new", "2026"),
    ):
        stored_plan(service, id, created)
    first = service.plans("shop", limit=2)
    assert [item.id for item in first.items] == ["new", "z-tie"]
    assert first.next_page
    assert service.plans("shop", limit=2) == first
    stored_plan(service, "inserted-newer", "2027")
    stored_plan(service, "inserted-older", "2021")
    second = service.plans("shop", page=first.next_page, limit=2)
    assert [item.id for item in second.items] == ["a-tie", "old"]
    assert second.next_page is None and second.cursor == first.cursor
    assert service.plans("shop", page=first.next_page, limit=2) == second
    assert len(service.plans("shop").items) == 6


def test_archive_cursors_and_permissions_are_bound_to_app_principal_and_grants(
    tmp_path: Path,
) -> None:
    service = archive_service(tmp_path, scoped=True)
    for id in ("a", "b"):
        stored_plan(service, id, "2026")
    with request_principal(Principal(id="viewer", name="Viewer", kind="oidc")):
        first = service.plans("shop", limit=1)
        assert first.next_page
        with pytest.raises(QueryError, match="ui-invalid-request"):
            service.plans("other", page=first.next_page, limit=1)
        for invalid in ("not-a-cursor", first.next_page + "x", "x" * 4097):
            with pytest.raises(QueryError, match="ui-invalid-request"):
                service.plans("shop", page=invalid, limit=1)
        with pytest.raises(QueryError, match="ui-invalid-request"):
            service.plans("shop", page=first.next_page, limit=2)
        for invalid_limit in (0, 101, -1):
            with pytest.raises(QueryError, match="ui-invalid-request"):
                service.plans("shop", limit=invalid_limit)
    with request_principal(Principal(id="another", name="Another", kind="oidc")):
        with pytest.raises(QueryError, match="ui-invalid-request"):
            service.plans("shop", page=first.next_page, limit=1)
    with request_principal(Principal(id="inspector", name="Inspector", kind="oidc")):
        with pytest.raises(QueryError, match="ui-not-found"):
            service.plans("shop")
    with request_principal(Principal(id="viewer", name="Viewer", kind="oidc")):
        assert service.query.scope_policy is not None
        service.query.scope_policy.replace(
            {"viewer": {"shop": frozenset({"activity"})}}
        )
        with pytest.raises(QueryError, match="ui-invalid-request"):
            service.plans("shop", page=first.next_page, limit=1)
        service.query.scope_policy.replace({})
        with pytest.raises(QueryError, match="ui-not-found"):
            service.plans("shop", page=first.next_page, limit=1)


def test_archive_page_limit_is_bounded_and_old_missing_optional_fields_are_compatible(
    tmp_path: Path,
) -> None:
    service = archive_service(tmp_path)
    for index in range(102):
        stored_plan(service, f"plan-{index:03d}", "2026")
    with service.store.transaction() as connection:
        connection.execute(
            "UPDATE records SET data=json_remove(data, '$.plan_kind', '$.desired_resources_complete', '$.source') WHERE kind='plan'"
        )
    assert len(service.plans("shop").items) == 25
    first = service.plans("shop", limit=100)
    assert len(first.items) == 100 and first.next_page
    assert len(service.plans("shop", page=first.next_page, limit=100).items) == 2
    assert all(
        item.source is None and item.plan_kind == "release" for item in first.items
    )
