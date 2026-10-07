"""``Environment(restore_points=...)``: restore points per environment (0.17.0)."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Any

import pytest

from piceli.envs import EnvError, Environment
from piceli.infra import Source
from piceli.restore import RestorePoints

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "python_composition"


@pytest.fixture
def module(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    found: dict[str, Any] = {}
    exec((EXAMPLE / "infra" / "example_app.py").read_text(), found)
    return found


def _derived(tmp_path: Path, env: Any) -> Any:
    from piceli.infra.composition import load_composition
    from piceli.infra.pipelines import environment_pipeline

    composition = load_composition(EXAMPLE / "infra" / "infra.py")
    return environment_pipeline(
        composition,
        env,
        kubeconfig=tmp_path / "kubeconfig",
        context="in-cluster",
        state_dir=tmp_path / "state",
    )


def _main(policy: Any, pipeline: Any) -> Environment:
    source = Source("https://example.com/shop.git", name="shop")
    return Environment(
        "main",
        namespace="example-main",
        pipeline=pipeline,
        follow={source: "main"},
        restore_points=policy,
    )


@pytest.mark.parametrize(
    ("policy", "include"),
    [("off", None), ("touched", "touched"), ("all", "all")],
)
def test_a_policy_name_sets_the_environment_restore_points(
    tmp_path: Path, module: dict[str, Any], policy: str, include: str | None
) -> None:
    declared = module["pipeline"]
    declared.restore_points = RestorePoints(timeout_seconds=1800)
    derived = _derived(tmp_path, _main(policy, declared))
    if include is None:
        assert derived.restore_points is None
        assert "backup" not in derived.stages
    else:
        assert derived.restore_points.include == include
        # The pipeline's other settings stay.
        assert derived.restore_points.timeout_seconds == 1800
    assert declared.restore_points.include == "touched"  # the declaration is kept


def test_restore_points_of_their_own_and_the_pipeline_default(
    tmp_path: Path, module: dict[str, Any]
) -> None:
    declared = module["pipeline"]
    declared.restore_points = None
    own = RestorePoints(max_claim_bytes=10**9, over_limit="skip")
    assert _derived(tmp_path, _main(own, declared)).restore_points == own
    # "all" on a pipeline without restore points: the defaults, include="all".
    assert _derived(tmp_path, _main("all", declared)).restore_points == RestorePoints(
        include="all"
    )
    # Not set: the pipeline's (none here).
    assert _derived(tmp_path, _main(None, declared)).restore_points is None


def test_a_bad_policy_is_refused(module: dict[str, Any]) -> None:
    with pytest.raises(EnvError) as raised:
        _main("sometimes", module["pipeline"])
    assert raised.value.code == "env-config-invalid"
    source = Source("https://example.com/shop.git", name="shop")
    with pytest.raises(EnvError):  # contract components take no restore points
        Environment(
            "main", namespace="shop", follow={source: "main"}, restore_points="off"
        )


def test_the_policy_is_in_the_description_only_when_declared(
    module: dict[str, Any],
) -> None:
    assert "restore_points" not in _main(None, module["pipeline"]).describe()
    assert _main("off", module["pipeline"]).describe()["restore_points"] == "off"
    branches = Environment.per_branch(
        "wp-*",
        namespace="example-{branch}",
        pipeline=module["pipeline"],
        follow={Source("https://example.com/shop.git", name="shop"): "{branch}"},
        restore_points="off",
    )
    assert branches.describe()["restore_points"] == "off"
    assert (
        dataclasses.replace(branches, restore_points=None)
        .describe()
        .get("restore_points")
        is None
    )
