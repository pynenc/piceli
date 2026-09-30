"""Acceptance: ``env up`` / ``envs`` / ``env down`` of a branch against the fake API.

The fake server holds one namespace (the branch's); the namespace object is
removed first so ``env up`` creates it, labelled, then deploys the branch
isolated (NetworkPolicy, ResourceQuota, qualified cluster names) with the
digest given for the build image.
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
from piceli import App, Build, EnvConfig, NodeLoopbackRegistry, Pipeline, Rule, Target

images = Build.spec("build.toml")
app = App("shop")
reader = app.service_account(
    "reader", cluster_rules=[Rule(resources=["nodes"], verbs=["get"])]
)
app.deployment("api", image=images["api"], ports=[8080], service_account=reader)

pipeline = Pipeline(
    app,
    Target.kubeconfig(
        "kubeconfig", context="fake", namespace="shop", transport="loopback-http",
        nodes={"primary": "node-a"},
    ),
    build=images,
    deliver=NodeLoopbackRegistry(),
    state_dir="state",
    execution={"max_seconds": 30, "readiness_seconds": 1, "poll_seconds": 0.05},
    envs=EnvConfig(prefix="shop-", branches=["wp-*"], max_envs=2),
)
"""


def _invoke(*args: str) -> tuple[int, dict[str, Any], Any]:
    result = CliRunner().invoke(cli, list(args))
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return result.exit_code, (lines[-1] if lines else {}), result


def test_branch_env_up_list_and_down(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(textwrap.dedent(PIPELINE))
    api = FakeAPI(types=TYPES_WITH_QUOTA, namespace=BRANCH_NS)
    del api.objects[("Namespace", BRANCH_NS)]
    api.add_node("node-a")
    entry = f"{tmp_path / 'app.py'}:pipeline"
    with serve(api) as (api, url):
        write_kubeconfig(url, tmp_path / "kubeconfig", context="fake")
        base = ["--pipeline", entry, "--digest", f"api={DIGEST}"]

        # Plan: a new namespace, nothing changed yet.
        code, body, result = _invoke("env", "up", "wp-login", *base)
        assert code == 3, result.output
        assert body["state"] == "approval-required"
        assert body["namespace"] == BRANCH_NS and body["create_namespace"] is True
        assert ("Namespace", BRANCH_NS) not in api.objects
        assert "--approve " + body["env_hash"] in result.stderr

        # A wrong hash is refused before anything changes.
        code, body2, _ = _invoke(
            "env", "up", "wp-login", *base, "--approve", "sha256:0"
        )
        assert code == 2 and body2["reason"] == "env-plan-changed"

        code, done, result = _invoke(
            "env",
            "up",
            "wp-login",
            *base,
            "--commit",
            "abc123",
            "--approve",
            body["env_hash"],
        )
        assert code == 0, result.output
        assert done["state"] == "ready"
        namespace = api.objects[("Namespace", BRANCH_NS)]
        assert namespace["metadata"]["labels"]["piceli.io/env-of"] == "shop"
        deployment = api.objects[("Deployment", "api")]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == f"127.0.0.1:5000/shop/api@{DIGEST}"
        assert deployment["spec"]["template"]["spec"]["nodeSelector"] == {
            "kubernetes.io/hostname": "node-a"
        }
        assert ("NetworkPolicy", "piceli-env-isolation") in api.objects
        assert ("ResourceQuota", "piceli-env-isolation") in api.objects
        # Cluster-wide RBAC is named after the release namespace already.
        assert ("ClusterRole", f"{BRANCH_NS}:shop:reader") in api.objects
        record = json.loads(api.objects[("ConfigMap", "piceli-env")]["data"]["record"])
        assert record["commit"] == "abc123" and record["state"] == "running"

        code, listing, result = _invoke("envs", "--pipeline", entry, "--json")
        assert code == 0, result.output
        rows = {row["namespace"]: row for row in listing["envs"]}
        assert rows[BRANCH_NS]["branch"] == "wp-login"
        assert rows[BRANCH_NS]["commit"] == "abc123"
        assert rows[BRANCH_NS]["health"] == "healthy"
        assert rows["shop"]["main"] is True

        # Teardown plans first, then deletes the namespace.
        code, down, result = _invoke("env", "down", "wp-login", "--pipeline", entry)
        assert code == 3, result.output
        assert ("Namespace", BRANCH_NS) in api.objects
        code, gone, result = _invoke(
            "env",
            "down",
            "wp-login",
            "--pipeline",
            entry,
            "--approve",
            down["env_hash"],
        )
        assert code == 0, result.output
        assert gone["state"] == "removed"
        assert ("Namespace", BRANCH_NS) not in api.objects

        # Main is never torn down.
        code, refused, _ = _invoke("env", "down", "main", "--pipeline", entry)
        assert code == 2 and refused["reason"] == "env-main-protected"
