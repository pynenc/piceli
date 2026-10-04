"""Replicas per environment (0.15): ``Environment(replicas=...)``, branch rules too."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from piceli import (
    App,
    Branch,
    ClaimTemplate,
    EnvConfig,
    Pipeline,
    Resources,
    Target,
)
from piceli.envs import EnvError, Environment, env_pipeline
from piceli.pipeline.compose import offline_composition

DB = "example/db@sha256:" + "2" * 64
WEB = "example/web@sha256:" + "1" * 64


def _app(*, autoscaled: bool = False) -> App:
    app = App("shop")
    web = app.deployment("web", image=WEB, resources=Resources(cpu="100m"))
    app.stateful_set(
        "db",
        image=DB,
        replicas=3,
        volumes={"/data": ClaimTemplate("data", size="1Gi")},
    )
    if autoscaled:
        app.autoscaler(web, min_replicas=1, max_replicas=3, cpu=70)
    return app


def _pipeline(tmp_path: Path, app: App | None = None, **envs: Any) -> Pipeline:
    return Pipeline(
        app or _app(),
        Target(tmp_path / "kc", context="c", namespace="shop"),
        state_dir=tmp_path / "state",
        envs=EnvConfig(prefix="shop-", **envs),
    )


def _replicas(pipeline: Pipeline) -> dict[tuple[str, str], Any]:
    _, composition = offline_composition(pipeline)
    return {
        (r.ref.kind, r.ref.name): r.manifest["spec"].get("replicas")
        for c in composition.components
        for r in c.resources
        if r.ref.kind in {"Deployment", "StatefulSet"}
    }


def test_a_branch_environment_runs_one_member_while_main_keeps_three(
    tmp_path: Path,
) -> None:
    pipeline = _pipeline(
        tmp_path,
        environments=[
            Environment("main", namespace="shop-main", follow=Branch("main"))
        ],
        branch_replicas={"db": 1},
    )
    main = _replicas(env_pipeline(pipeline, "main"))
    branch = _replicas(env_pipeline(pipeline, "wp-login"))
    assert main[("StatefulSet", "db")] == 3
    assert branch[("StatefulSet", "db")] == 1
    # A named environment sets its own count.
    named = _pipeline(
        tmp_path,
        environments=[
            Environment(
                "main", namespace="shop-main", follow=Branch("main"), replicas={"db": 5}
            )
        ],
    )
    assert _replicas(env_pipeline(named, "main"))[("StatefulSet", "db")] == 5


def test_replicas_refusals(tmp_path: Path) -> None:
    with pytest.raises(EnvError):
        Environment("main", namespace="shop-main", replicas={"db": -1})
    with pytest.raises(EnvError):
        Environment("main", namespace="shop-main", replicas={"db": True})
    with pytest.raises(EnvError) as error:
        offline_composition(
            env_pipeline(_pipeline(tmp_path, branch_replicas={"nope": 1}), "wp-1")
        )
    assert "nope" in str(error.value)
    autoscaled = _pipeline(
        tmp_path, app=_app(autoscaled=True), branch_replicas={"web": 2}
    )
    with pytest.raises(EnvError) as error:
        offline_composition(env_pipeline(autoscaled, "wp-1"))
    assert "HorizontalPodAutoscaler" in str(error.value)


def test_replicas_unset_keeps_the_description(tmp_path: Path) -> None:
    plain = Environment("main", namespace="shop-main")
    assert "replicas" not in plain.describe()
    assert "branch_replicas" not in EnvConfig(prefix="shop-").describe()
    assert Environment("main", namespace="shop-main", replicas={"db": 1}).describe()[
        "replicas"
    ] == {"db": 1}


def test_a_composition_carries_replicas_through_its_config() -> None:
    import dataclasses

    from piceli.infra.composition import Composition, load_composition
    from piceli.infra.render import env_config

    example = Path(__file__).resolve().parents[3] / "examples" / "composition"
    base = load_composition(example / "infra.py")
    rule = base.branch_rule
    main = base.environment("main")
    assert rule is not None and isinstance(main, Environment)
    scaled = dataclasses.replace(main, replicas={"web": 3})
    object.__setattr__(scaled, "sources", main.sources)  # replace() keeps no sources
    changed = Composition(
        name=base.name,
        environments=(
            scaled,
            *[e for e in base.environments if e.name not in {"main", rule.name}],
            dataclasses.replace(rule, replicas={"web": 1}),
        ),
        components=base.components,
        cluster=base.cluster,
    )
    data = changed.to_dict()
    assert data["environments"][0]["replicas"] == {"web": 3}
    assert data["environments"][-1]["replicas"] == {"web": 1}
    again = Composition.from_dict(data)
    assert again.branch_rule is not None
    assert again.branch_rule.env_config().branch_replicas == {"web": 1}
    named = env_config(again, again.environment("main"))  # type: ignore[arg-type]
    assert named.environments[0].replicas == {"web": 3}
