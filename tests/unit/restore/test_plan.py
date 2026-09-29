"""Which claims a release touches, and which writers stop (pure planner)."""

from __future__ import annotations

import pytest

from piceli import App, ClaimTemplate, ExistingClaim
from piceli.restore import Quiesce, RestorePointError, RestorePoints
from piceli.restore.plan import plan, writer_pods
from tests.unit.restore.conftest import (
    CACHE,
    NEW,
    OLD,
    claim,
    claims,
    stateful_app,
    workloads,
)


def test_a_statefulset_image_change_plans_one_restore_point_per_replica_claim() -> None:
    result = plan(workloads(stateful_app(NEW)), workloads(stateful_app(OLD)), claims())
    assert [(item["claim"], item["ordinal"]) for item in result.claims] == [
        ("data-db-0", 0),
        ("data-db-1", 1),
    ]
    assert all(item["workload"] == "StatefulSet/db" for item in result.claims)
    assert result.claims[0]["why"] == ["image of container db changes"]
    assert [item["workload"] for item in result.writers] == ["StatefulSet/db"]
    assert result.writers[0]["replicas"] == 2


def test_claims_of_replicas_scaled_away_are_included() -> None:
    result = plan(
        workloads(stateful_app(NEW, replicas=1)),
        workloads(stateful_app(OLD, replicas=1)),
        [*claims(), claim("data-db-2")],
    )
    assert [item["claim"] for item in result.claims] == [
        "data-db-0",
        "data-db-1",
        "data-db-2",
    ]


def test_an_unchanged_release_needs_no_restore_point() -> None:
    result = plan(workloads(stateful_app(NEW)), workloads(stateful_app(NEW)), claims())
    assert result.empty
    assert result.writers == []


def test_a_storage_setting_change_is_a_reason() -> None:
    result = plan(
        workloads(stateful_app(NEW, size="2Gi")),
        workloads(stateful_app(NEW, size="1Gi")),
        claims(),
    )
    assert {tuple(item["why"]) for item in result.claims} == {
        ("claim templates change",)
    }


def test_an_image_not_delivered_yet_counts_as_a_change() -> None:
    result = plan(
        workloads(stateful_app("pipeline.piceli.invalid/db:unresolved")),
        workloads(stateful_app(OLD)),
        claims(),
        pending=lambda image: image.startswith("pipeline.piceli.invalid/"),
    )
    assert result.claims[0]["why"] == ["image of container db is not delivered yet"]


def test_an_existing_claim_of_a_changed_deployment_is_covered() -> None:
    old, new = stateful_app(NEW), stateful_app(NEW)
    live = workloads(old)
    for item in live:
        if item["metadata"]["name"] == "cache":
            item["spec"]["template"]["spec"]["containers"][0]["image"] = OLD
    result = plan(workloads(new), live, claims())
    assert [item["claim"] for item in result.claims] == ["cache-state"]
    assert result.claims[0]["workload"] == "Deployment/cache"
    assert [item["workload"] for item in result.writers] == ["Deployment/cache"]


def test_unbound_and_missing_claims_hold_no_data() -> None:
    result = plan(
        workloads(stateful_app(NEW)),
        workloads(stateful_app(OLD)),
        [claim("data-db-0", phase="Pending")],
    )
    assert result.empty


def test_every_writer_of_a_touched_claim_stops_and_hooks_are_listed() -> None:
    app = stateful_app(NEW)
    reader = app.deployment(
        "reader", image=CACHE, volumes={"/data": ExistingClaim("cache-state")}
    )
    app.quiesce(reader, Quiesce.exec(["sync"]))
    live = workloads(app)
    for item in live:
        if item["metadata"]["name"] == "cache":
            item["spec"]["template"]["spec"]["containers"][0]["image"] = OLD
    result = plan(workloads(app), live, claims(), hooks=app.quiesce_hooks())
    assert [item["workload"] for item in result.writers] == [
        "Deployment/cache",
        "Deployment/reader",
    ]
    assert result.writers[1]["quiesce"] == [
        {"type": "exec", "command": ["sync"], "timeout_seconds": 30}
    ]


def test_a_read_only_mount_is_not_a_writer() -> None:
    app = App("shop")
    app.deployment(
        "cache",
        image=NEW,
        volumes={"/data": ExistingClaim("cache-state", read_only=True)},
    )
    live = workloads(app)
    live[0]["spec"]["template"]["spec"]["containers"][0]["image"] = OLD
    assert plan(workloads(app), live, claims()).empty


def test_a_writer_piceli_cannot_stop_is_refused() -> None:
    desired = workloads(stateful_app(NEW))
    live = workloads(stateful_app(OLD))
    foreign = {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {"name": "agent"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "agent",
                            "image": CACHE,
                            "volumeMounts": [{"name": "d", "mountPath": "/d"}],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "d",
                            "persistentVolumeClaim": {"claimName": "data-db-0"},
                        }
                    ],
                }
            }
        },
    }
    with pytest.raises(RestorePointError) as caught:
        plan(desired, [*live, foreign], claims())
    assert caught.value.code == "restore-point-writer-unsupported"


def test_identity_leaves_live_replica_counts_out() -> None:
    first = plan(workloads(stateful_app(NEW)), workloads(stateful_app(OLD)), claims())
    scaled = workloads(stateful_app(OLD))
    for item in scaled:
        if item["kind"] == "StatefulSet":
            item["spec"]["replicas"] = 5
    second = plan(workloads(stateful_app(NEW)), scaled, claims())
    assert first.identity() == second.identity()


def test_writer_pods_include_terminating_pods_and_skip_finished_or_read_only() -> None:
    def pod(name, *, phase="Running", read_only=False, deleting=False):
        metadata = {"name": name}
        if deleting:
            metadata["deletionTimestamp"] = "2026-01-01T00:00:00Z"
        return {
            "metadata": metadata,
            "status": {"phase": phase},
            "spec": {
                "containers": [
                    {
                        "name": "c",
                        "volumeMounts": [
                            {"name": "d", "mountPath": "/d", "readOnly": read_only}
                        ],
                    }
                ],
                "volumes": [
                    {"name": "d", "persistentVolumeClaim": {"claimName": "data-db-0"}}
                ],
            },
        }

    pods = [
        pod("db-0", deleting=True),
        pod("done", phase="Succeeded"),
        pod("backup", read_only=True),
    ]
    assert writer_pods(pods, ["data-db-0"]) == ["db-0"]
    assert writer_pods(pods, ["other"]) == []


def test_quiesce_hooks_are_typed() -> None:
    assert Quiesce.http("/flush", 8080).describe() == {
        "type": "http",
        "method": "POST",
        "path": "/flush",
        "port": 8080,
        "expect": 200,
        "timeout_seconds": 30,
    }
    with pytest.raises(ValueError):
        Quiesce.http("flush", 8080)
    with pytest.raises(ValueError):
        Quiesce.exec([])


def test_app_quiesce_takes_declared_deployments_and_statefulsets_only() -> None:
    app = stateful_app()
    job = app.job("migrate", image=CACHE)
    with pytest.raises(ValueError, match="Deployment or StatefulSet"):
        app.quiesce(job, Quiesce.exec(["true"]))
    other = App("other").deployment("x", image=CACHE)
    with pytest.raises(ValueError, match="declared on this app"):
        app.quiesce(other, Quiesce.exec(["true"]))
    db = next(item for item in app.objects if item.name == "db")
    app.quiesce(db, Quiesce.exec(["sync"]))
    env = app.for_environment(__import__("piceli").Environment("dev"))
    assert env.quiesce_hooks() == app.quiesce_hooks()


def test_restore_points_settings() -> None:
    with pytest.raises(ValueError, match="pinned by digest"):
        RestorePoints(image="busybox:latest")
    settings = RestorePoints(image="busybox@sha256:" + "a" * 64)
    assert settings.describe()["timeout_seconds"] == 600


def test_claim_templates_work_without_a_template_volume_name_collision() -> None:
    app = App("shop")
    app.stateful_set(
        "db",
        image=NEW,
        volumes={
            "/a": ClaimTemplate("data", size="1Gi"),
            "/b": ClaimTemplate("logs", size="1Gi"),
        },
    )
    live = workloads(app)
    live[0]["spec"]["template"]["spec"]["containers"][0]["image"] = OLD
    result = plan(
        workloads(app),
        live,
        [claim("data-db-0"), claim("logs-db-0"), claim("data-db2-0")],
    )
    assert [item["claim"] for item in result.claims] == ["data-db-0", "logs-db-0"]
