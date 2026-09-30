"""The order of a deploy run's stages, with and without the optional ones.

``prerollout`` (pre-rollout checks) and ``backup`` (restore points) exist only
when declared. When both are, the checks run first: a new image is checked
before any writer is stopped, and the restore point is still taken before the
release plan (the executor compares objects against that plan).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from piceli import App, ClaimTemplate, Pipeline, RestorePoints, Target
from piceli.k8s.cli.deploy_pipeline import STAGE_NAMES
from piceli.pipeline import ALL_STAGES, STAGES

SCHEMAS = Path(__file__).resolve().parents[3] / "docs" / "schemas"
BOTH = ("inputs", "build", "deliver", "prerollout", "backup", "plan", "apply", "checks")


def pipeline(tmp_path: Path, *, check: bool, restore: bool) -> Pipeline:
    app = App("shop")
    db = app.stateful_set(
        "db",
        image="example/db:2",
        volumes={"/var/lib/db": ClaimTemplate("data", size="1Gi")},
    )
    if check:
        app.pre_rollout(db, ["db", "check-config"])
    target = Target.kubeconfig(
        str(tmp_path / "kubeconfig"), context="fake", namespace="shop"
    )
    return Pipeline(
        app,
        target,
        state_dir=tmp_path / "state",
        restore_points=RestorePoints() if restore else None,
    )


def test_checks_run_before_the_restore_point_and_the_restore_point_before_the_plan(
    tmp_path: Path,
) -> None:
    assert pipeline(tmp_path, check=True, restore=True).stages == BOTH
    assert ALL_STAGES == BOTH
    assert STAGE_NAMES == BOTH


@pytest.mark.parametrize(
    ("check", "restore", "expected"),
    [
        (False, False, STAGES),
        (True, False, tuple(n for n in BOTH if n != "backup")),
        (False, True, tuple(n for n in BOTH if n != "prerollout")),
    ],
)
def test_each_optional_stage_exists_only_when_declared(
    tmp_path: Path, check: bool, restore: bool, expected: tuple[str, ...]
) -> None:
    assert pipeline(tmp_path, check=check, restore=restore).stages == expected


@pytest.mark.parametrize(
    "name",
    [
        "piceli-deploy-event-v1.schema.json",
        "piceli-run-summary-v1.schema.json",
        "piceli-watch-event-v1.schema.json",
    ],
)
def test_the_schemas_list_the_stages_in_run_order(name: str) -> None:
    schema = json.loads((SCHEMAS / name).read_text())
    assert tuple(schema["$defs"]["stageName"]["enum"]) == BOTH
