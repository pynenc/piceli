"""Environment operations against an in-memory cluster: budget, teardown, list, seed."""

from __future__ import annotations

import copy
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from piceli import App, EnvConfig, Pipeline, RestorePoints, Target
from piceli.envs import EnvError, env_down, env_pipeline, env_up, list_envs, seed_env
from piceli.envs.model import ENV_OF_LABEL
from piceli.envs.ops import plan_budget, receipt_images, stop_env
from piceli.pipeline.compose import model_fingerprint

IMAGE = "example/web@sha256:" + "1" * 64
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _pipeline(tmp_path: Path, **envs: Any) -> Pipeline:
    app = App("shop")
    app.deployment("web", image=IMAGE)
    return Pipeline(
        app,
        Target(tmp_path / "kc", context="c", namespace="shop"),
        state_dir=tmp_path / "state",
        restore_points=RestorePoints(directory=str(tmp_path / "points")),
        envs=EnvConfig(prefix="shop-", max_envs=2, **envs),
    )


class Cluster:
    """The EnvCluster methods, in memory."""

    def __init__(self) -> None:
        self.ns: dict[str, dict[str, Any]] = {"shop": {"metadata": {"name": "shop"}}}
        self.records: dict[str, dict[str, Any]] = {}
        self.live: dict[str, list[dict[str, Any]]] = {}
        self.pvcs: dict[str, list[str]] = {}
        self.pvs: list[dict[str, Any]] = []
        self.calls: list[str] = []

    def add(self, name: str, branch: str, pushed: str, *, app: str = "shop") -> None:
        self.ns[name] = {
            "metadata": {
                "name": name,
                "uid": f"uid-{name}",
                "creationTimestamp": "2026-09-30T10:00:00Z",
                "labels": {ENV_OF_LABEL: app},
                "annotations": {"piceli.io/env-branch": branch},
            }
        }
        self.records[name] = {"branch": branch, "pushed_at": pushed, "state": "running"}
        self.live[name] = [
            {
                "kind": "Deployment",
                "metadata": {"name": "web"},
                "spec": {"replicas": 2},
                "status": {"readyReplicas": 2},
            }
        ]

    def namespaces(self, app: str) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(item)
            for item in self.ns.values()
            if item["metadata"].get("labels", {}).get(ENV_OF_LABEL) == app
        ]

    def namespace(self, name: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.ns.get(name))

    def record(self, namespace: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.records.get(namespace))

    def write_record(self, namespace: str, record: dict[str, Any]) -> None:
        self.records[namespace] = copy.deepcopy(dict(record))

    def workloads(self, namespace: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self.live.get(namespace, []))

    def scale(self, namespace: str, kind: str, name: str, replicas: int) -> None:
        self.calls.append(f"scale {namespace} {kind}/{name} {replicas}")
        for item in self.live[namespace]:
            if item["metadata"]["name"] == name:
                item["spec"]["replicas"] = replicas

    def claims(self, namespace: str) -> list[dict[str, Any]]:
        return [{"metadata": {"name": n}} for n in self.pvcs.get(namespace, [])]

    def volumes(self) -> list[dict[str, Any]]:
        return self.pvs

    def delete_claim(self, namespace: str, name: str) -> None:
        self.calls.append(f"delete claim {namespace}/{name}")

    def delete_namespace(self, name: str) -> bool:
        self.calls.append(f"delete namespace {name}")
        return self.ns.pop(name, None) is not None

    def delete_volume(self, name: str) -> None:
        self.calls.append(f"delete volume {name}")

    def pushed(self, namespace: str, branch: str) -> None:
        return None


def test_declaring_envs_keeps_main_rendering(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path)
    plain = copy.copy(pipeline)
    plain.envs = None
    assert model_fingerprint(pipeline) == model_fingerprint(plain)
    assert env_pipeline(pipeline, "main") is pipeline
    branch = env_pipeline(pipeline, "wp-login")
    assert branch.target.namespace == "shop-wp-login"
    assert branch.state_dir == tmp_path / "state" / "branches" / "shop-wp-login"
    assert branch.restore_points is None and branch.builds == ()
    assert pipeline.target.namespace == "shop"
    assert model_fingerprint(branch) != model_fingerprint(pipeline)


def test_branch_patterns_are_enforced(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, branches=["wp-*"])
    with pytest.raises(EnvError) as error:
        env_pipeline(pipeline, "feature-x")
    assert error.value.code == "env-branch-not-allowed"


def test_budget_stops_the_least_recently_pushed(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path)
    cluster = Cluster()
    cluster.add("shop-wp-a", "wp-a", "2026-09-30T09:00:00Z")
    cluster.add("shop-wp-b", "wp-b", "2026-09-30T08:00:00Z")
    assert [
        item["namespace"] for item in plan_budget(cluster, pipeline, "shop-wp-c")
    ] == ["shop-wp-b"]
    # A running environment redeploys without stopping another.
    assert plan_budget(cluster, pipeline, "shop-wp-a") == []
    stopped = stop_env(cluster, "shop-wp-b", now=NOW)
    assert stopped == [
        {
            "workload": "Deployment/web",
            "kind": "Deployment",
            "name": "web",
            "replicas": 2,
        }
    ]
    assert cluster.records["shop-wp-b"]["state"] == "stopped"
    assert "scale shop-wp-b Deployment/web 0" in cluster.calls
    assert plan_budget(cluster, pipeline, "shop-wp-c") == []


def test_wait_refuses_when_the_budget_is_full(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path)
    cluster = Cluster()
    cluster.add("shop-wp-a", "wp-a", "2026-09-30T09:00:00Z")
    cluster.add("shop-wp-b", "wp-b", "2026-09-30T08:00:00Z")
    with pytest.raises(EnvError) as error:
        env_up(pipeline, "wp-c", wait=True, cluster=cluster)
    assert error.value.code == "env-budget-full"
    assert error.value.details["would_stop"][0]["namespace"] == "shop-wp-b"
    assert not error.value.failed and cluster.calls == []


def test_up_refuses_foreign_or_colliding_namespaces(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path)
    cluster = Cluster()
    cluster.add("shop-wp-a", "wp-a", "2026-09-30T09:00:00Z", app="other")
    with pytest.raises(EnvError) as error:
        env_up(pipeline, "wp-a", cluster=cluster)
    assert error.value.code == "env-namespace-not-managed"
    cluster.add("shop-wp-b", "wp/b", "2026-09-30T09:00:00Z")
    with pytest.raises(EnvError) as error:
        env_up(pipeline, "wp-b", cluster=cluster)
    assert error.value.code == "env-namespace-collision"
    del cluster.ns["shop"]
    with pytest.raises(EnvError) as error:
        env_up(pipeline, "main", cluster=cluster)
    assert error.value.code == "env-main-namespace-missing"


def test_down_is_planned_guarded_and_never_touches_main(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path)
    cluster = Cluster()
    cluster.add("shop-wp-a", "wp-a", "2026-09-30T09:00:00Z")
    cluster.pvcs["shop-wp-a"] = ["data-db-0"]
    cluster.pvs = [
        {
            "metadata": {"name": "pv-1"},
            "spec": {"claimRef": {"namespace": "shop-wp-a"}},
        },
        {"metadata": {"name": "pv-main"}, "spec": {"claimRef": {"namespace": "shop"}}},
    ]
    for branch in ("main",):
        with pytest.raises(EnvError) as error:
            env_down(pipeline, branch, cluster=cluster)
        assert error.value.code == "env-main-protected"
    plan = env_down(pipeline, "wp-a", cluster=cluster)
    assert plan["state"] == "approval-required"
    assert plan["delete"] == {"claims": ["data-db-0"], "volumes": ["pv-1"]}
    assert cluster.calls == []
    with pytest.raises(EnvError) as error:
        env_down(pipeline, "wp-a", cluster=cluster, approve="sha256:bad")
    assert error.value.code == "env-plan-changed"
    # EnvConfig(auto_approve=False): the policy flag alone does not run it.
    assert (
        env_down(pipeline, "wp-a", cluster=cluster, approve_if_policy=True)["state"]
        == "approval-required"
    )
    state = tmp_path / "state" / "branches" / "shop-wp-a"
    state.mkdir(parents=True)
    done = env_down(pipeline, "wp-a", cluster=cluster, approve=plan["env_hash"])
    assert done["state"] == "removed"
    assert cluster.calls == [
        "delete claim shop-wp-a/data-db-0",
        "delete namespace shop-wp-a",
        "delete volume pv-1",
    ]
    assert not state.exists()
    assert env_down(pipeline, "wp-a", cluster=cluster)["state"] == "absent"


def test_down_refuses_a_namespace_of_another_app(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, auto_approve=True)
    cluster = Cluster()
    cluster.add("shop-wp-a", "wp-a", "2026-09-30T09:00:00Z", app="other")
    with pytest.raises(EnvError) as error:
        env_down(pipeline, "wp-a", cluster=cluster, approve_if_policy=True)
    assert error.value.code == "env-namespace-not-managed"
    assert cluster.calls == []


def test_down_by_policy_when_the_owner_allows_branches(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, auto_approve=True)
    cluster = Cluster()
    cluster.add("shop-wp-a", "wp-a", "2026-09-30T09:00:00Z")
    done = env_down(pipeline, "wp-a", cluster=cluster, approve_if_policy=True)
    assert done["state"] == "removed"


def test_list_shows_main_and_branches(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path)
    cluster = Cluster()
    cluster.add("shop-wp-a", "wp-a", "2026-09-30T09:00:00Z")
    cluster.records["shop-wp-a"].update({"commit": "abc", "deploy": "ready"})
    cluster.add("shop-wp-b", "wp-b", "2026-09-30T08:00:00Z")
    stop_env(cluster, "shop-wp-b", now=NOW)
    rows = [item.to_dict() for item in list_envs(pipeline, cluster=cluster, now=NOW)]
    assert [(r["branch"], r["namespace"], r["main"]) for r in rows] == [
        ("main", "shop", True),
        ("wp-a", "shop-wp-a", False),
        ("wp-b", "shop-wp-b", False),
    ]
    assert rows[1]["health"] == "healthy" and rows[1]["commit"] == "abc"
    assert rows[1]["age_seconds"] == 7200
    assert rows[2]["state"] == "stopped" and rows[2]["health"] == "stopped"
    json.dumps(rows)


def test_receipt_images_accepts_every_receipt_shape(tmp_path: Path) -> None:
    ref = "127.0.0.1:5000/shop/web@sha256:" + "2" * 64
    assert receipt_images({"images": {"web": ref}}) == {"web": ref}
    assert receipt_images(
        {
            "platforms": ["linux/arm64"],
            "delivered": {"linux/arm64": {"web": {"pull_ref": ref}}},
        }
    ) == {"web": {"pull_ref": ref}}
    assert receipt_images(
        {"images": json.dumps({"web": {"digest": "sha256:" + "2" * 64}})}
    )
    with pytest.raises(EnvError):
        receipt_images({})


def test_seed_refuses_main_and_needs_a_restore_point(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path)
    cluster = Cluster()
    with pytest.raises(EnvError) as error:
        seed_env(pipeline, "main", cluster=cluster)
    assert error.value.code == "env-main-protected"
    with pytest.raises(EnvError) as error:
        seed_env(pipeline, "wp-a", cluster=cluster)
    assert error.value.code == "env-seed-no-restore-point"


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="needs sha256sum")
def test_seed_restores_mains_latest_point_into_the_branch(tmp_path: Path) -> None:
    from piceli.restore.runner import take
    from tests.unit.restore.conftest import NEW, stateful_app, workloads
    from tests.unit.restore.fake_cluster import FakeCluster
    from tests.unit.restore.test_take import _setup

    main, result = _setup(tmp_path / "main")
    record = take(
        main,  # type: ignore[arg-type]
        result,
        RestorePoints(),
        tmp_path / "points",
        context={"app": "shop", "namespace": "shop"},
    )
    branch = FakeCluster(tmp_path / "branch", workloads(stateful_app(NEW)), "shop-wp-a")
    branch.claim_names = ["data-db-0", "data-db-1"]
    pipeline = _pipeline(tmp_path)
    cluster = Cluster()
    cluster.add("shop-wp-a", "wp-a", "2026-09-30T09:00:00Z")
    plan = seed_env(pipeline, "wp-a", cluster=cluster, restore_cluster=branch)
    assert plan["state"] == "approval-required"
    assert plan["source"] == {
        "source": "main",
        "namespace": "shop",
        "point": record["id"],
    }
    assert not (branch.claim_dir("data-db-0") / "marker").exists()
    done = seed_env(
        pipeline,
        "wp-a",
        cluster=cluster,
        restore_cluster=branch,
        approve=plan["env_hash"],
        now=NOW,
    )
    assert done["state"] == "seeded"
    assert (branch.claim_dir("data-db-0") / "marker").read_text() == "replica 0\n"
    assert cluster.records["shop-wp-a"]["seeded_from"]["point"] == record["id"]
