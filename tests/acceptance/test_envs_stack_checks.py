"""Acceptance: a branch environment with a Stack runs only the checks of its stack.

The pipeline declares checks for two workloads; the branch stack deploys one.
The check of the left-out workload is skipped (``not-in-stack``) instead of
failing the branch, and a check that targets no workload still runs.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from piceli.testing import TYPES, FakeAPI, serve, write_kubeconfig

BRANCH_NS = "shop-wp-login"
DIGEST = "sha256:" + "4" * 64
TYPES_WITH_QUOTA = {**TYPES, "resourcequotas": ("v1", "ResourceQuota", True)}

PIPELINE = """
from piceli import (
    App, Build, Checks, EnvConfig, NodeLoopbackRegistry, Pipeline, Stack, Target,
)

images = Build.spec("build.toml")
app = App("shop")
api = app.deployment("api", image=images["api"], ports=[8080])
worker = app.deployment("worker", image="example/worker@sha256:" + "5" * 64)
CALLS = []


def smoke(ctx):
    CALLS.append(ctx.release)
    return True


pipeline = Pipeline(
    app,
    Target.kubeconfig(
        "kubeconfig", context="fake", namespace="shop", transport="loopback-http",
        nodes={"primary": "node-a"},
    ),
    build=images,
    deliver=NodeLoopbackRegistry(),
    checks=[
        Checks.exec(worker, ["worker", "ping"], retries=0),
        Checks.python(smoke, retries=0),
    ],
    state_dir="state",
    execution={"max_seconds": 30, "readiness_seconds": 1, "poll_seconds": 0.05},
    envs=EnvConfig(
        prefix="shop-", branches=["wp-*"], max_envs=2,
        branch_stack=Stack("small", workloads=["api"]),
    ),
)
"""


def _invoke(*args: str) -> tuple[int, dict[str, Any], Any]:
    result = CliRunner().invoke(cli, list(args))
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return result.exit_code, (lines[-1] if lines else {}), result


def _checks_output(tmp_path: Path) -> dict[str, Any]:
    runs = sorted((tmp_path / "state" / "branches" / BRANCH_NS / "runs").glob("*.json"))
    record = json.loads(runs[-1].read_text())
    return dict(record["stages"]["checks"]["output"])


def test_a_branch_stack_skips_checks_of_workloads_it_does_not_deploy(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text(textwrap.dedent(PIPELINE))
    api = FakeAPI(types=TYPES_WITH_QUOTA, namespace=BRANCH_NS)
    del api.objects[("Namespace", BRANCH_NS)]
    api.add_node("node-a")
    entry = f"{tmp_path / 'app.py'}:pipeline"
    with serve(api) as (api, url):
        write_kubeconfig(url, tmp_path / "kubeconfig", context="fake")
        base = ["--pipeline", entry, "--digest", f"api={DIGEST}"]
        code, body, result = _invoke("env", "up", "wp-login", *base)
        assert code == 3, result.output
        code, done, result = _invoke(
            "env", "up", "wp-login", *base, "--approve", body["env_hash"]
        )
        assert code == 0, result.output
        assert done["state"] == "ready"
        assert ("Deployment", "worker") not in api.objects
        output = _checks_output(tmp_path)
        assert output["passed"] is True
        assert [item["name"] for item in output["results"]] == ["python-smoke"]
        assert output["skipped"] == [
            {
                "check": "exec-deployment-worker-worker",
                "target": "deployment/worker",
                "why": "not-in-stack",
            }
        ]
