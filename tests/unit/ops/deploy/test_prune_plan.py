"""A prune plan (0.14.7): what it deletes, what it keeps, how it deletes.

Pure planning: a pruned workload is deleted ``Foreground`` (its pods go
first), a StatefulSet that would delete its claims is kept, and the claims a
removed StatefulSet created from its templates are kept and listed. Only an
object this release's owner wrote (``Ownership.MANAGED``) is ever deleted.
"""

from __future__ import annotations

import pytest

from piceli.k8s.ops.plan import (
    Ownership,
    PlanAuthorization,
    PlanOperation,
    ResourceIntent,
    ResourceRef,
    deletes_claims,
    prune_propagation,
)
from tests.unit.ops.deploy.test_pure_plan import (
    TARGET,
    build_plan,
    composition,
    manifest,
    observed,
    plan_for,
)


def _apps(kind: str, name: str, **spec: object) -> dict:
    found = manifest(kind, name, **spec)
    found["apiVersion"] = "apps/v1"
    return found


def _sts(name: str, *, when_deleted: str = "Retain") -> dict:
    return _apps(
        "StatefulSet",
        name,
        volumeClaimTemplates=[{"metadata": {"name": "data"}}],
        persistentVolumeClaimRetentionPolicy={
            "whenDeleted": when_deleted,
            "whenScaled": "Retain",
        },
    )


def _deleted(plan) -> set[tuple[str, str]]:
    return {
        (a.resource.ref.kind, a.resource.ref.name)
        for a in plan.actions
        if a.operation is PlanOperation.DELETE
    }


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("Deployment", "Foreground"),
        ("StatefulSet", "Foreground"),
        ("DaemonSet", "Foreground"),
        ("Job", "Foreground"),
        ("CronJob", "Foreground"),
        ("ReplicaSet", "Foreground"),
        ("Service", "Orphan"),
        ("ConfigMap", "Orphan"),
        ("NetworkPolicy", "Orphan"),
    ],
)
def test_a_pruned_workload_takes_its_pods_with_it(kind: str, expected: str) -> None:
    assert prune_propagation(kind) == expected


def test_only_objects_of_this_owner_are_pruned() -> None:
    """The ownership filter: managed objects go, every other object stays."""
    mine = observed(_apps("Deployment", "old"), uid="mine")
    foreign = observed(
        _apps("Deployment", "foreign"), uid="foreign", ownership=Ownership.UNMANAGED
    )
    service = observed(
        manifest("Service", "foreign-svc"), uid="svc", ownership=Ownership.UNMANAGED
    )
    plan = plan_for([], (mine, foreign, service), prune=True)
    assert _deleted(plan) == {("Deployment", "old")}
    assert not plan.protected_resources
    # Without the prune nothing is deleted at all.
    assert not _deleted(plan_for([], (mine, foreign, service)))


def test_a_statefulset_whose_policy_deletes_claims_is_kept() -> None:
    keeps = observed(_sts("keeps"), uid="keeps")
    deletes = observed(_sts("deletes", when_deleted="Delete"), uid="deletes")
    assert deletes_claims(deletes.intent.manifest)
    assert not deletes_claims(keeps.intent.manifest)
    plan = plan_for([], (keeps, deletes), prune=True)
    assert _deleted(plan) == {("StatefulSet", "keeps")}
    assert ResourceRef("apps/v1", "StatefulSet", "app-test", "deletes") in (
        plan.protected_resources
    )


def test_a_removed_statefulsets_template_claims_are_kept_and_listed() -> None:
    db = observed(_sts("db"), uid="db")
    claims = [
        observed(
            manifest("PersistentVolumeClaim", name),
            uid=name,
            ownership=Ownership.UNMANAGED,
        )
        for name in ("data-db-0", "data-db-2", "data-db-extra", "data-dbx-0", "other")
    ]
    plan = plan_for([], (db, *claims), prune=True)
    assert _deleted(plan) == {("StatefulSet", "db")}
    kept = {ref.name for ref in plan.protected_resources}
    assert kept == {"data-db-0", "data-db-2"}


def test_claims_of_a_statefulset_removed_earlier_stay_listed() -> None:
    """Once the StatefulSet is gone, the earlier release's declaration names them."""
    claim = observed(
        manifest("PersistentVolumeClaim", "data-db-0"),
        uid="claim",
        ownership=Ownership.UNMANAGED,
    )
    snapshot_plan = plan_for([], (claim,), prune=True)
    assert not snapshot_plan.protected_resources  # nothing says it was db's
    from piceli.k8s.ops.plan import ObservedSnapshot

    previous = (ResourceIntent.from_manifest(_sts("db")),)
    plan = build_plan(
        composition([]),
        ObservedSnapshot(TARGET, snapshot_plan_coverage(claim), (claim,), ()),
        PlanAuthorization(TARGET, prune_managed=True, previous=previous),
    )
    assert [ref.name for ref in plan.protected_resources] == ["data-db-0"]
    assert not plan.actions


def snapshot_plan_coverage(*items):
    from piceli.k8s.ops.discovery import (
        ApiResource,
        DiscoveryCoverage,
        ResourceScope,
        ResourceType,
    )

    types = {
        ResourceType(item.intent.ref.api_version, item.intent.ref.kind)
        for item in items
    }
    return DiscoveryCoverage(
        "unit-fake",
        "capture-1",
        "defaults-v1",
        tuple(types),
        tuple(types),
        tuple(
            ApiResource(item, ResourceScope.NAMESPACED, item.kind.lower() + "s")
            for item in types
        ),
    )


def test_a_rename_creates_the_new_kind_and_deletes_the_old_after_it() -> None:
    """Deployment ``api`` becomes StatefulSet ``api``; the Service stays."""
    service = manifest("Service", "api", selector={"app": "api"})
    old = observed(_apps("Deployment", "api"), uid="old")
    kept = observed(service, uid="svc")
    plan = plan_for([_apps("StatefulSet", "api"), service], (old, kept), prune=True)
    operations = [
        (a.operation, a.resource.ref.kind, a.resource.ref.name) for a in plan.actions
    ]
    assert operations[-1] == (PlanOperation.DELETE, "Deployment", "api")
    assert (PlanOperation.CREATE, "StatefulSet", "api") in operations
    assert _deleted(plan) == {("Deployment", "api")}
