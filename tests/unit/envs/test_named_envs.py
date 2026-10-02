"""Named environments, stacks, placement, branch claims and idle stop (0.14)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from piceli import (
    App,
    Branch,
    ClaimTemplate,
    EnvConfig,
    ExistingClaim,
    Pipeline,
    Promote,
    RestorePoints,
    Stack,
    Tag,
    Target,
)
from piceli.envs import (
    EnvError,
    Environment,
    env_down,
    env_pipeline,
    env_stop,
    env_up,
    list_envs,
    seed_env,
)
from piceli.envs.model import ENV_NAME_LABEL, ENV_OF_LABEL
from piceli.envs.ops import plan_budget
from piceli.gitops.config import ControllerConfig
from piceli.k8s.ops.plan import ResourceRef
from piceli.pipeline.compose import model_fingerprint, offline_composition
from tests.unit.envs.test_ops import Cluster

WEB = "example/web@sha256:" + "1" * 64
DB = "example/db@sha256:" + "2" * 64
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _app() -> App:
    app = App("shop")
    app.deployment("web", image=WEB)
    app.stateful_set(
        "db", image=DB, volumes={"/data": ClaimTemplate("data", size="5Gi")}
    )
    app.deployment(
        "cache",
        image="example/cache@sha256:" + "3" * 64,
        volumes={"/state": ExistingClaim("cache-state")},
    )
    return app


def _pipeline(tmp_path: Path, app: App | None = None, **envs: Any) -> Pipeline:
    return Pipeline(
        app or _app(),
        Target(tmp_path / "kc", context="c", namespace="shop"),
        state_dir=tmp_path / "state",
        restore_points=RestorePoints(directory=str(tmp_path / "points")),
        envs=EnvConfig(prefix="shop-", max_envs=2, **envs),
    )


def _named() -> list[Environment]:
    return [
        Environment("main", namespace="shop-main", follow=Branch("main")),
        Environment(
            "rc",
            namespace="shop-rc",
            follow=[Tag("v*-rc*"), Promote()],
            quota={"pods": "20"},
            auto_approve=True,
        ),
    ]


def _objects(pipeline: Pipeline) -> dict[tuple[str, str], dict[str, Any]]:
    _, composition = offline_composition(pipeline)
    return {
        (r.ref.kind, r.ref.name): r.manifest
        for c in composition.components
        for r in c.resources
    }


def _pod(manifest: dict[str, Any]) -> dict[str, Any]:
    spec = manifest["spec"]["template"]["spec"]
    assert isinstance(spec, dict)
    return spec


# -------------------------------------------------------------- compat


def test_unused_new_fields_keep_0_13_hashes(tmp_path: Path) -> None:
    """Golden values computed with 0.13.0 for the same declarations."""
    app = App("shop")
    app.deployment("web", image=WEB)
    app.stateful_set(
        "db", image=DB, volumes={"/data": ClaimTemplate("data", size="5Gi")}
    )
    pipeline = Pipeline(
        app,
        Target(tmp_path / "kc", context="c", namespace="shop"),
        state_dir=tmp_path / "s",
        envs=EnvConfig(
            prefix="shop-", claim_sizes={"db": "1Gi"}, allow_egress=["10.0.0.0/8"]
        ),
    )
    assert model_fingerprint(pipeline) == (
        "sha256:1136125b0af18778a356dc2fce418ce2c95ca633fe2e956fda271c1ed49b1914"
    )
    assert model_fingerprint(env_pipeline(pipeline, "wp-login")) == (
        "sha256:46ae7df75b9cc5c19941f9257c1924750fd72d9095683fb60a1feb9d7802a3aa"
    )
    up = env_up(pipeline, "wp-new", plan_only=True, cluster=Cluster())
    assert up["env_hash"] == (
        "sha256:979112503378eeb0d27eb062a1761c584246842e3f63abfd8aa5b3c33a105859"
    )
    assert "environment" not in up

    def digest(value: Any) -> str:
        text = json.dumps(value, sort_keys=True).encode()
        return hashlib.sha256(text).hexdigest()

    assert pipeline.envs is not None
    assert digest(pipeline.envs.describe()) == (
        "1cebfad54d76e991ad5b5dddc35e4429cb302ff181e55c5a61ec34a90b06ad2c"
    )
    config = ControllerConfig(
        pipeline="deploy/app.py:pipeline",
        repo="https://example.com/r.git",
        branches=("main", "wp-*"),
    )
    assert digest(config.to_dict()) == (
        "07c1565420e0968670483c81ab60cd85276889fa29562897dbc8a2aa78bf80ce"
    )


# --------------------------------------------------------------- model


def test_environment_and_stack_validation() -> None:
    env = Environment("rc", namespace="shop-rc", follow=[Tag("v*-rc*"), Promote()])
    assert env.tags == ("v*-rc*",) and env.promote and env.branches == ()
    assert Environment("x", namespace="x").follow == (Promote(),)
    for bad in (
        lambda: Branch("wp-*"),
        lambda: Environment("Bad Name", namespace="x"),
        lambda: Environment("x", namespace="x", follow="main"),
        lambda: Environment("x", namespace="x", on_nodes="node-a"),
        lambda: Stack("min", workloads=[]),
        lambda: EnvConfig(prefix="shop-", idle_stop="soon"),
        lambda: EnvConfig(prefix="shop-", idle_stop="10s"),
        lambda: EnvConfig(
            prefix="shop-",
            environments=[
                Environment("a", namespace="same"),
                Environment("b", namespace="same"),
            ],
        ),
    ):
        with pytest.raises(EnvError) as error:
            bad()
        assert error.value.code == "env-config-invalid"
    config = EnvConfig(prefix="shop-", environments=_named(), idle_stop="2d")
    assert config.idle_stop_seconds == 172800
    assert config.namespace_for("rc") == "shop-rc"
    assert config.namespace_for("main") == "shop-main"
    assert config.is_fixed("rc") and not config.is_fixed("wp-1")
    # A branch whose namespace is a named environment's is refused.
    clash = EnvConfig(
        prefix="shop-", environments=[Environment("rc", namespace="shop-wp-x")]
    )
    with pytest.raises(EnvError) as error:
        clash.namespace_for("wp-x")
    assert error.value.code == "env-namespace-collision"


# ------------------------------------------------------------- render


def test_named_environment_renders_its_stack_nodes_and_quota(tmp_path: Path) -> None:
    named = [
        Environment(
            "rc",
            namespace="shop-rc",
            stack=Stack("core", workloads=["web", "db"]),
            on_nodes=["node-a", "node-b"],
            quota={"pods": "20"},
        )
    ]
    pipeline = _pipeline(tmp_path, environments=named)
    derived = env_pipeline(pipeline, "rc")
    assert derived.target.namespace == "shop-rc"
    assert derived.state_dir == tmp_path / "state" / "environments" / "rc"
    assert derived.restore_point_directory == pipeline.restore_point_directory
    assert pipeline.target.namespace == "shop"
    objects = _objects(derived)
    assert ("Deployment", "cache") not in objects
    assert objects[("ResourceQuota", "piceli-env-isolation")]["spec"] == {
        "hard": {"pods": "20"}
    }
    assert ("NetworkPolicy", "piceli-env-isolation") not in objects
    terms = _pod(objects[("Deployment", "web")])["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"]
    assert terms == [
        {
            "matchExpressions": [
                {
                    "key": "kubernetes.io/hostname",
                    "operator": "In",
                    "values": ["node-a", "node-b"],
                }
            ]
        }
    ]
    # Named environments keep ExistingClaim semantics: no claim is rendered.
    full = _objects(env_pipeline(_pipeline(tmp_path, environments=_named()), "main"))
    assert ("Deployment", "cache") in full
    assert ("PersistentVolumeClaim", "cache-state") not in full


def test_branch_stack_nodes_and_owned_claims(tmp_path: Path) -> None:
    pipeline = _pipeline(
        tmp_path,
        branch_stack=Stack("small", workloads=["web", "cache"]),
        branch_nodes={"example.com/pool": "branches"},
        claim_sizes={"cache-state": "1Gi"},
    )
    objects = _objects(env_pipeline(pipeline, "wp-login"))
    assert ("StatefulSet", "db") not in objects
    assert _pod(objects[("Deployment", "web")])["nodeSelector"] == {
        "example.com/pool": "branches"
    }
    claim = objects[("PersistentVolumeClaim", "cache-state")]
    assert claim["metadata"]["namespace"] == "shop-wp-login"
    assert claim["spec"]["resources"]["requests"]["storage"] == "1Gi"
    _, composition = offline_composition(env_pipeline(pipeline, "wp-login"))
    cache = next(
        r for c in composition.components for r in c.resources if r.ref.name == "cache"
    )
    assert (
        ResourceRef("v1", "PersistentVolumeClaim", "shop-wp-login", "cache-state")
        in cache.dependencies
    )
    # Main keeps the ExistingClaim as is.
    assert ("PersistentVolumeClaim", "cache-state") not in _objects(pipeline)
    # An unsized ExistingClaim stays an existing claim in a branch (as in 0.13).
    plain = _objects(env_pipeline(_pipeline(tmp_path), "wp-login"))
    assert ("PersistentVolumeClaim", "cache-state") not in plain


def test_stack_refusals_and_node_conflicts(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, branch_stack=Stack("x", workloads=["nope"]))
    with pytest.raises(EnvError) as error:
        offline_composition(env_pipeline(pipeline, "wp-1"))
    assert error.value.code == "env-stack-unknown"
    app = _app()
    app.depends("web", on="db")
    pipeline = _pipeline(tmp_path, app=app, branch_stack=Stack("x", workloads=["web"]))
    with pytest.raises(EnvError) as error:
        offline_composition(env_pipeline(pipeline, "wp-1"))
    assert error.value.code == "env-stack-incomplete"
    app = App("shop")
    app.deployment("web", image=WEB, node="db-node")
    pinned = Pipeline(
        app,
        Target(
            tmp_path / "kc",
            context="c",
            namespace="shop",
            nodes={"db-node": "node-z"},
        ),
        state_dir=tmp_path / "state",
        envs=EnvConfig(prefix="shop-", branch_nodes=["node-a"]),
    )
    with pytest.raises(EnvError) as error:
        offline_composition(env_pipeline(pinned, "wp-1"))
    assert error.value.code == "env-nodes-conflict"


# ---------------------------------------------------------------- ops


def test_named_env_up_creates_its_namespace_and_is_protected(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, environments=_named())
    cluster = Cluster()
    up = env_up(pipeline, "rc", plan_only=True, cluster=cluster)
    assert up["environment"] == "rc" and up["main"] is False
    assert up["namespace"] == "shop-rc" and up["create_namespace"] is True
    assert up["stop"] == []
    # Named environments are never budget-stopped, deleted, stopped or seeded.
    cluster.ns["shop-rc"] = {
        "metadata": {"name": "shop-rc", "labels": {ENV_NAME_LABEL: "rc"}}
    }
    assert plan_budget(cluster, pipeline, "shop-wp-x") == []
    for call in (
        lambda: env_down(pipeline, "rc", cluster=cluster),
        lambda: env_stop(pipeline, "rc", cluster=cluster),
        lambda: seed_env(pipeline, "rc", cluster=cluster),
        lambda: env_down(pipeline, "main", cluster=cluster),
    ):
        with pytest.raises(EnvError) as error:
            call()
        assert error.value.code == "env-main-protected"


def test_envs_lists_named_environments(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, environments=_named())
    cluster = Cluster()
    cluster.ns["shop-main"] = {"metadata": {"name": "shop-main"}}
    cluster.add("shop-wp-a", "wp-a", "2026-10-01T09:00:00Z")
    rows = [item.to_dict() for item in list_envs(pipeline, cluster=cluster, now=NOW)]
    assert [(r["branch"], r["namespace"], r["fixed"], r["main"]) for r in rows] == [
        ("main", "shop-main", True, True),
        ("rc", "shop-rc", True, False),
        ("wp-a", "shop-wp-a", False, False),
    ]
    assert rows[1]["state"] == "absent"
    # Without named environments the rows are 0.13's plus fixed=false.
    plain = list_envs(_pipeline(tmp_path), cluster=cluster, now=NOW)
    assert [item.branch for item in plain] == ["main", "wp-a"]
    assert not any(item.fixed for item in plain)


def test_env_stop_is_planned_and_allowed_by_idle_stop(tmp_path: Path) -> None:
    cluster = Cluster()
    cluster.add("shop-wp-a", "wp-a", "2026-10-01T09:00:00Z")
    plain = _pipeline(tmp_path)
    planned = env_stop(plain, "wp-a", approve_if_policy=True, cluster=cluster)
    assert planned["state"] == "approval-required"
    assert planned["stop"] == ["Deployment/web"] and cluster.calls == []
    idle = _pipeline(tmp_path, idle_stop="24h")
    done = env_stop(idle, "wp-a", approve_if_policy=True, cluster=cluster, now=NOW)
    assert done["state"] == "stopped"
    assert cluster.calls == ["scale shop-wp-a Deployment/web 0"]
    record = cluster.records["shop-wp-a"]
    assert record["state"] == "stopped" and record["stop_reason"] == "idle"
    again = env_stop(idle, "wp-a", approve_if_policy=True, cluster=cluster)
    assert again["state"] == "stopped" and again["stopped"] == []
    cluster.ns["shop-wp-b"] = {
        "metadata": {"name": "shop-wp-b", "labels": {ENV_OF_LABEL: "other"}}
    }
    with pytest.raises(EnvError) as error:
        env_stop(idle, "wp-b", approve_if_policy=True, cluster=cluster)
    assert error.value.code == "env-namespace-not-managed"


def test_checks_are_scoped_to_the_branch_stack(tmp_path: Path) -> None:
    from piceli import Checks
    from piceli.pipeline.compose import release_checks, scoped_checks

    app = App("shop")
    web = app.service(app.deployment("web", image=WEB, ports=[8080]), port=80)
    db = app.stateful_set("db", image=DB, ports=[5432])
    checks = [
        Checks.http(web, "/"),
        Checks.exec(db, ["true"]),
        Checks.http("service/db", "/"),
        Checks.exec("pod/db-0", ["true"]),
        Checks.python("checks.py:smoke"),
    ]
    pipeline = Pipeline(
        app,
        Target(tmp_path / "kc", context="c", namespace="shop"),
        state_dir=tmp_path / "state",
        checks=checks,
        envs=EnvConfig(prefix="shop-", branch_stack=Stack("small", workloads=["web"])),
    )
    # Main (no stack) keeps every check.
    assert scoped_checks(pipeline) == (tuple(checks), [])
    kept, skipped = scoped_checks(env_pipeline(pipeline, "wp-login"))
    assert kept == (checks[0], checks[3], checks[4])
    assert [(item["target"], item["why"]) for item in skipped] == [
        ("statefulset/db", "not-in-stack"),
        ("service/db", "not-in-stack"),
    ]
    assert release_checks(env_pipeline(pipeline, "wp-login")) == kept
