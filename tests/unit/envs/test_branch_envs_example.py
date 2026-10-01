"""``examples/branch_envs``: main, rc and branch environments render as documented."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from piceli.app.render import load_target
from piceli.envs import env_pipeline
from piceli.pipeline.compose import offline_composition

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "branch_envs"


def _objects(pipeline: Any) -> dict[tuple[str, str], dict[str, Any]]:
    _, composition = offline_composition(pipeline)
    return {
        (r.ref.kind, r.ref.name): r.manifest
        for c in composition.components
        for r in c.resources
    }


def _hosts(manifest: dict[str, Any]) -> list[str]:
    pod = manifest["spec"]["template"]["spec"]
    terms = pod["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"]
    return list(terms[0]["matchExpressions"][0]["values"])


def test_example_environments() -> None:
    pipeline = load_target("app.py:pipeline", EXAMPLE)
    main = _objects(env_pipeline(pipeline, "main"))
    assert {name for kind, name in main if kind == "Deployment"} == {
        "web",
        "worker",
        "db",
    }
    assert ("PersistentVolumeClaim", "db-data") not in main
    rc = env_pipeline(pipeline, "rc")
    assert rc.target.namespace == "shop-rc"
    assert _hosts(_objects(rc)[("Deployment", "web")]) == ["node-a", "node-b"]
    branch = env_pipeline(pipeline, "wp-login")
    objects = _objects(branch)
    assert branch.target.namespace == "shop-wp-login"
    assert ("Deployment", "worker") not in objects
    assert _hosts(objects[("Deployment", "db")]) == ["node-c"]
    claim = objects[("PersistentVolumeClaim", "db-data")]
    assert claim["spec"]["resources"]["requests"]["storage"] == "1Gi"
    assert pipeline.envs.idle_stop_seconds == 86400
