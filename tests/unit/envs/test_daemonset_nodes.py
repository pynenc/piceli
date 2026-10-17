"""0.20 (IH 10.5): a DaemonSet is not narrowed to the environment's nodes.

A node agent (the kernel-faults reader) belongs on every node; the
environment's ``on_nodes`` pins its other workloads only. The agent's own
``node_selector`` still applies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from piceli import App, Branch, EnvConfig, Pipeline, Target
from piceli.envs import Environment, env_pipeline
from piceli.pipeline.compose import offline_composition

IMAGE = "example/agent@sha256:" + "1" * 64


def _objects(app: App) -> dict[tuple[str, str], dict[str, Any]]:
    pipeline = Pipeline(
        app,
        Target(Path("/nonexistent/kc"), context="c", namespace="ops"),
        state_dir=Path("/nonexistent/state"),
        envs=EnvConfig(
            prefix="ops-",
            environments=[
                Environment("main", namespace="ops-main", follow=Branch("main"),
                            on_nodes=["pi00"])
            ],
        ),
    )  # fmt: skip
    _, composition = offline_composition(env_pipeline(pipeline, "main"))
    return {
        (r.ref.kind, r.ref.name): dict(r.manifest)
        for c in composition.components
        for r in c.resources
    }


def test_a_node_agent_runs_on_every_node_and_the_rest_stay_pinned() -> None:
    app = App("ops")
    app.daemon_set("kernel-faults", image=IMAGE)
    app.daemon_set(
        "arm-only", image=IMAGE, node_selector={"kubernetes.io/arch": "arm64"}
    )
    app.deployment("api", image=IMAGE)
    found = _objects(app)
    agent = found[("DaemonSet", "kernel-faults")]["spec"]["template"]["spec"]
    assert "affinity" not in agent and "nodeSelector" not in agent
    arm = found[("DaemonSet", "arm-only")]["spec"]["template"]["spec"]
    assert arm["nodeSelector"] == {"kubernetes.io/arch": "arm64"}
    api = found[("Deployment", "api")]["spec"]["template"]["spec"]
    terms = api["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]
    assert terms["nodeSelectorTerms"][0]["matchExpressions"][0]["values"] == ["pi00"]
