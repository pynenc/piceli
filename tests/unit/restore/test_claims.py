"""Grow or move a retained claim: the pure planner and the run steps."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from piceli import App, ClaimTemplate, ExistingClaim
from piceli.restore import RestorePointError, RestorePoints, RestoreVerify
from piceli.restore.claims import (
    MIGRATED_FROM,
    MIGRATED_TO,
    MIGRATION_LABEL,
    grow,
    plan_claims,
)
from piceli.restore.plan import plan
from piceli.restore.runner import take
from tests.unit.restore.conftest import CACHE, NEW, workloads
from tests.unit.restore.fake_cluster import FakeCluster, FakeJobs

needs_tools = pytest.mark.skipif(
    shutil.which("sha256sum") is None, reason="needs sha256sum"
)


def pvc(
    name: str,
    size: str = "1Gi",
    storage_class: str | None = "local-path",
    annotations: dict[str, str] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name, "namespace": "shop"}
    if annotations:
        metadata["annotations"] = dict(annotations)
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": metadata,
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": storage_class,
            "resources": {"requests": {"storage": size}},
            "volumeName": f"pv-{name}",
        },
        "status": {"phase": "Bound", "capacity": {"storage": size}},
    }


def db_app(template: ClaimTemplate, replicas: int = 2) -> App:
    app = App("shop")
    app.stateful_set(
        "db", image=NEW, replicas=replicas, volumes={"/var/lib/db": template}
    )
    return app


def cache_app(volume: ExistingClaim) -> App:
    app = App("shop")
    app.deployment("cache", image=CACHE, volumes={"/data": volume})
    return app


def expandable(name: str | None) -> bool:
    return name == "expandable"


# ------------------------------------------------------------------ planner


def test_a_larger_template_on_an_expandable_class_grows_every_replica_claim() -> None:
    old = db_app(ClaimTemplate("data", size="1Gi", storage_class="expandable"))
    new = db_app(ClaimTemplate("data", size="2Gi", storage_class="expandable"))
    result = plan_claims(
        workloads(new),
        workloads(old),
        [
            pvc("data-db-0", storage_class="expandable"),
            pvc("data-db-1", storage_class="expandable"),
        ],
        declared=new.claim_growth(),
        expandable=expandable,
    )
    assert [(s["action"], s["claim"], s["from"], s["to"]) for s in result.steps] == [
        ("expand", "data-db-0", "1Gi", "2Gi"),
        ("expand", "data-db-1", "1Gi", "2Gi"),
    ]
    assert result.touched()["data-db-0"]["why"] == "claim grows from 1Gi to 2Gi"


def test_a_larger_template_without_expansion_asks_for_a_migration() -> None:
    old = db_app(ClaimTemplate("data", size="512Mi"))
    new = db_app(ClaimTemplate("data", size="8Gi"))
    with pytest.raises(RestorePointError) as caught:
        plan_claims(
            workloads(new),
            workloads(old),
            [pvc("data-db-0", "512Mi"), pvc("data-db-1", "512Mi")],
            declared=new.claim_growth(),
            expandable=expandable,
        )
    assert caught.value.code == "claim-migration-required"
    assert 'migrate_from="data"' in str(caught.value)


@pytest.mark.parametrize(
    ("old", "new", "claims"),
    [
        (
            db_app(ClaimTemplate("data", size="2Gi", storage_class="expandable")),
            db_app(ClaimTemplate("data", size="1Gi", storage_class="expandable")),
            [pvc("data-db-0", "2Gi", "expandable")],
        ),
        (
            cache_app(ExistingClaim("cache-state")),
            cache_app(ExistingClaim("cache-state", size="1Gi")),
            [pvc("cache-state", "2Gi", "expandable")],
        ),
        (
            cache_app(ExistingClaim("cache-state")),
            cache_app(ExistingClaim("cache-2", size="1Gi", migrate_from="cache-state")),
            [pvc("cache-state", "2Gi")],
        ),
    ],
    ids=["template", "existing-claim", "move"],
)
def test_a_smaller_claim_is_refused(old: App, new: App, claims: list[Any]) -> None:
    with pytest.raises(RestorePointError) as caught:
        plan_claims(
            workloads(new),
            workloads(old),
            claims,
            declared=new.claim_growth(),
            expandable=expandable,
        )
    assert caught.value.code == "claim-shrink-refused"


def test_a_template_move_plans_one_new_claim_per_replica() -> None:
    old = db_app(ClaimTemplate("data", size="512Mi"))
    new = db_app(ClaimTemplate("data-2", size="8Gi", migrate_from="data"))
    result = plan_claims(
        workloads(new),
        workloads(old),
        [pvc("data-db-0", "512Mi"), pvc("data-db-1", "512Mi"), pvc("other")],
        declared=new.claim_growth(),
        expandable=expandable,
    )
    assert [
        (
            s["action"],
            s["claim"],
            s["target"],
            s["ordinal"],
            s["to"],
            s["storage_class"],
        )
        for s in result.steps
    ] == [
        ("migrate", "data-db-0", "data-2-db-0", 0, "8Gi", "local-path"),
        ("migrate", "data-db-1", "data-2-db-1", 1, "8Gi", "local-path"),
    ]
    # Once the StatefulSet mounts the new template, nothing is left to do.
    done = plan_claims(
        workloads(new),
        workloads(new),
        [pvc("data-db-0", "512Mi"), pvc("data-2-db-0", "8Gi")],
        declared=new.claim_growth(),
    )
    assert done.empty
    assert done.notes == [
        "StatefulSet/db already mounts template data-2: nothing to move"
    ]


def test_an_existing_claim_move_is_covered_by_the_restore_point() -> None:
    old = cache_app(ExistingClaim("cache-state"))
    new = cache_app(
        ExistingClaim("cache-state-2", size="8Gi", migrate_from="cache-state")
    )
    claims = [pvc("cache-state", "512Mi")]
    steps = plan_claims(
        workloads(new), workloads(old), claims, declared=new.claim_growth()
    )
    assert steps.identity() == [
        {
            "action": "migrate",
            "claim": "cache-state",
            "target": "cache-state-2",
            "workload": "Deployment/cache",
            "ordinal": None,
            "from": "512Mi",
            "to": "8Gi",
            "storage_class": "local-path",
            "access_modes": ["ReadWriteOnce"],
        }
    ]
    point = plan(workloads(new), workloads(old), claims, also=steps.touched())
    assert [c["claim"] for c in point.claims] == ["cache-state"]
    assert "claim moves to cache-state-2 (8Gi)" in point.claims[0]["why"]
    assert [w["workload"] for w in point.writers] == ["Deployment/cache"]


def test_an_existing_claim_that_grows_in_place_stops_its_unchanged_writer() -> None:
    old = cache_app(ExistingClaim("cache-state"))
    new = cache_app(ExistingClaim("cache-state", size="4Gi"))
    claims = [pvc("cache-state", "1Gi", "expandable")]
    steps = plan_claims(
        workloads(new),
        workloads(old),
        claims,
        declared=new.claim_growth(),
        expandable=expandable,
    )
    assert [(s["action"], s["claim"], s["to"]) for s in steps.steps] == [
        ("expand", "cache-state", "4Gi")
    ]
    # The Deployment itself does not change: only the growth touches it.
    assert plan(workloads(new), workloads(old), claims).empty
    point = plan(workloads(new), workloads(old), claims, also=steps.touched())
    assert [c["claim"] for c in point.claims] == ["cache-state"]


def test_a_claim_named_like_the_new_claim_is_never_taken_over() -> None:
    old = cache_app(ExistingClaim("cache-state"))
    new = cache_app(ExistingClaim("cache-2", size="8Gi", migrate_from="cache-state"))
    with pytest.raises(RestorePointError) as caught:
        plan_claims(
            workloads(new),
            workloads(old),
            [pvc("cache-state"), pvc("cache-2", "8Gi")],
            declared=new.claim_growth(),
        )
    assert caught.value.code == "claim-migration-target-exists"
    # A new claim Piceli made for this move (an earlier, failed run) resumes.
    resumed = plan_claims(
        workloads(new),
        workloads(old),
        [
            pvc("cache-state"),
            pvc("cache-2", "8Gi", annotations={MIGRATED_FROM: "cache-state"}),
        ],
        declared=new.claim_growth(),
    )
    assert resumed.steps[0]["resume"] is True
    assert "resume" not in resumed.identity()[0]


def test_an_app_that_grows_nothing_plans_nothing() -> None:
    app = db_app(ClaimTemplate("data", size="1Gi"))
    result = plan_claims(
        workloads(app),
        workloads(app),
        [pvc("data-db-0"), pvc("data-db-1")],
        declared=app.claim_growth(),
    )
    assert result.empty and result.notes == []
    assert app.claim_growth() == {}


def test_claim_growth_declarations_are_validated() -> None:
    with pytest.raises(ValueError, match="needs size"):
        ExistingClaim("b", migrate_from="a")
    with pytest.raises(ValueError, match="only goes with migrate_from"):
        ExistingClaim("a", size="1Gi", storage_class="fast")
    with pytest.raises(ValueError, match="another"):
        ClaimTemplate("data", size="1Gi", migrate_from="data")
    with pytest.raises(ValueError, match="writably"):
        ExistingClaim("b", size="1Gi", migrate_from="a", read_only=True)


# ------------------------------------------------------------------ run


def _point(
    tmp_path: Path, fake: FakeCluster, app_before: App, app_after: App
) -> tuple[Any, dict[str, Any]]:
    steps = plan_claims(
        workloads(app_after),
        workloads(app_before),
        list(fake.pvcs.values()),
        declared=app_after.claim_growth(),
        expandable=fake.expandable,
    )
    point = plan(
        workloads(app_after),
        workloads(app_before),
        list(fake.pvcs.values()),
        also=steps.touched(),
    )
    record = take(
        fake,  # type: ignore[arg-type]
        point,
        RestorePoints(),
        tmp_path / "points",
        context={"app": "shop", "namespace": "shop"},
        keep_stopped={w["workload"] for w in point.writers},
    )
    return steps, record


def _grow(
    tmp_path: Path, fake: FakeCluster, steps: Any, record: Any, app: App
) -> tuple[list[dict[str, Any]], FakeJobs]:
    jobs = FakeJobs(fake)
    result = grow(
        fake,
        steps.steps,
        record=record,
        directory=tmp_path / "points" / record["id"],
        workloads=dict(fake.live),
        verifies=app.restore_verifies(),
        jobs=jobs,
        app="shop",
        image=None,
        timeout_seconds=30,
    )
    return result, jobs


def test_an_expansion_patches_the_claim_and_waits_until_it_grew(tmp_path) -> None:
    before = cache_app(ExistingClaim("cache-state"))
    after = cache_app(ExistingClaim("cache-state", size="4Gi"))
    fake = FakeCluster(tmp_path, workloads(before))
    fake.pvcs["cache-state"] = pvc("cache-state", "1Gi", "expandable")
    fake.claim_names = ["cache-state"]
    fake.resize_polls = 3
    steps = plan_claims(
        workloads(after),
        workloads(before),
        list(fake.pvcs.values()),
        declared=after.claim_growth(),
        expandable=fake.expandable,
    )
    result = grow(
        fake,
        steps.steps,
        record={"id": "rp-1-abcdef", "claims": []},
        directory=tmp_path,
        workloads=dict(fake.live),
        verifies={},
        jobs=FakeJobs(fake),
        app="shop",
        image=None,
        timeout_seconds=30,
    )
    assert fake.calls == ["expand cache-state 4Gi"]
    assert result == [
        {"action": "expand", "claim": "cache-state", "to": "4Gi", "state": "done"}
    ]
    assert fake.pvcs["cache-state"]["status"]["capacity"]["storage"] == "4Gi"


def test_an_infeasible_expansion_fails_with_a_code(tmp_path) -> None:
    fake = FakeCluster(tmp_path, [])
    fake.pvcs["cache-state"] = pvc("cache-state", "1Gi", "expandable")
    fake.resize_polls = 0
    fake.pvcs["cache-state"]["status"]["allocatedResourceStatuses"] = {
        "storage": "ControllerResizeInfeasible"
    }
    step = {
        "action": "expand",
        "claim": "cache-state",
        "workload": "Deployment/cache",
        "from": "1Gi",
        "to": "4Gi",
    }
    with pytest.raises(RestorePointError) as caught:
        grow(
            fake,
            [step],
            record={"id": "rp-1-abcdef", "claims": []},
            directory=tmp_path,
            workloads={},
            verifies={},
            jobs=FakeJobs(fake),
            app="shop",
            image=None,
            timeout_seconds=30,
        )
    assert caught.value.code == "claim-expansion-failed"


def _move_fixture(
    tmp_path: Path, verify: list[str] | None = None
) -> tuple[FakeCluster, App, App]:
    before = cache_app(ExistingClaim("cache-state"))
    after = cache_app(
        ExistingClaim("cache-state-2", size="8Gi", migrate_from="cache-state")
    )
    if verify is not None:
        (cache,) = [w for w in after._objects if getattr(w, "name", "") == "cache"]
        after.restore_verify(cache, RestoreVerify(verify))
    fake = FakeCluster(tmp_path, workloads(before))
    fake.live["Deployment/cache"]["spec"]["replicas"] = 1
    fake.pvcs["cache-state"] = pvc("cache-state", "512Mi")
    fake.claim_names = ["cache-state"]
    (fake.claim_dir("cache-state") / "marker").write_text("before the move\n")
    (fake.claim_dir("cache-state") / "nested").mkdir()
    (fake.claim_dir("cache-state") / "nested" / "file").write_text("x" * 1000)
    return fake, before, after


@needs_tools
def test_a_move_copies_verifies_and_keeps_the_old_claim(tmp_path) -> None:
    fake, before, after = _move_fixture(
        tmp_path, verify=["sh", "-c", "test -s /data/marker"]
    )
    steps, record = _point(tmp_path, fake, before, after)
    result, jobs = _grow(tmp_path, fake, steps, record, after)
    (moved,) = result
    assert (moved["claim"], moved["target"], moved["verify"]) == (
        "cache-state",
        "cache-state-2",
        "passed",
    )
    target = fake.claim_dir("cache-state-2")
    assert (target / "marker").read_text() == "before the move\n"
    assert (target / "nested" / "file").read_text() == "x" * 1000
    assert moved["content_sha256"] == record["claims"][0]["content_sha256"]
    # The new claim is labelled as a move target; the old one is kept and
    # annotated, never deleted.
    new = fake.pvcs["cache-state-2"]
    assert new["metadata"]["labels"][MIGRATION_LABEL] == "target"
    assert new["metadata"]["annotations"][MIGRATED_FROM] == "cache-state"
    assert new["spec"]["resources"]["requests"]["storage"] == "8Gi"
    assert new["spec"]["storageClassName"] == "local-path"
    old = fake.pvcs["cache-state"]
    assert old["metadata"]["annotations"][MIGRATED_TO] == "cache-state-2"
    assert old["metadata"]["labels"][MIGRATION_LABEL] == "source"
    assert (fake.claim_dir("cache-state") / "marker").exists()
    assert not any(call.startswith("delete claim") for call in fake.calls)
    # The verify Job mounted the new claim, read-only, at the app's path.
    (job,) = jobs.jobs
    volumes = job["spec"]["template"]["spec"]["volumes"]
    assert [
        v["persistentVolumeClaim"]["claimName"]
        for v in volumes
        if "persistentVolumeClaim" in v
    ] == ["cache-state-2"]
    # Order: stop, copy the old claim (read-only), fill the new one.
    assert fake.calls.index("scale Deployment/cache 0") < fake.calls.index(
        "create claim cache-state-2"
    )


@needs_tools
def test_a_copy_that_does_not_match_is_never_switched(tmp_path) -> None:
    fake, before, after = _move_fixture(tmp_path)
    steps, record = _point(tmp_path, fake, before, after)
    record["claims"][0]["content_sha256"] = "0" * 64  # as if the copy differed
    with pytest.raises(RestorePointError) as caught:
        _grow(tmp_path, fake, steps, record, after)
    assert caught.value.code == "claim-migration-mismatch"
    assert "annotate cache-state" not in fake.calls


@needs_tools
def test_a_failing_verify_command_stops_the_move(tmp_path) -> None:
    fake, before, after = _move_fixture(
        tmp_path, verify=["sh", "-c", "test -e /data/missing"]
    )
    steps, record = _point(tmp_path, fake, before, after)
    with pytest.raises(RestorePointError) as caught:
        _grow(tmp_path, fake, steps, record, after)
    assert caught.value.code == "claim-migration-verify-failed"
    assert "annotate cache-state" not in fake.calls
    assert "before the move" not in str(caught.value)
