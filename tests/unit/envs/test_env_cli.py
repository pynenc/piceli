"""``piceli env``, ``piceli envs``, ``piceli logs`` and ``piceli access BRANCH`` (no cluster)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from piceli.envs import EnvStatus
from piceli.k8s.cli import app as cli

PIPELINE = """
from piceli import App, EnvConfig, Pipeline, Target

app = App("shop")
web = app.deployment("web", image="example/web@sha256:" + "1" * 64, ports=[8080])
app.service(web, port=80, access=app.access.forward(local=18080))
pipeline = Pipeline(
    app,
    Target.kubeconfig("kubeconfig", context="c", namespace="shop"),
    envs=EnvConfig(prefix="shop-", branches=["wp-*"]),
)
"""


def _entry(tmp_path: Path) -> str:
    (tmp_path / "app.py").write_text(textwrap.dedent(PIPELINE))
    (tmp_path / "kubeconfig").write_text("{}\n")
    return f"{tmp_path / 'app.py'}:pipeline"


def _run(*args: str, env: dict[str, str] | None = None) -> tuple[int, Any, Any]:
    result = CliRunner().invoke(cli, list(args), env=env)
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return result.exit_code, (lines[-1] if lines else None), result


def test_commands_need_a_pipeline() -> None:
    code, body, _ = _run("envs", env={"PICELI_PIPELINE": ""})
    assert code == 2 and body["reason"] == "env-pipeline-missing"


def test_envs_lists_as_json(tmp_path: Path) -> None:
    rows = [EnvStatus("main", "shop", True, "running", "healthy", commit="abc")]
    with patch("piceli.envs.ops.list_envs", return_value=rows):
        code, body, result = _run(
            "envs", "--json", env={"PICELI_PIPELINE": _entry(tmp_path)}
        )
    assert code == 0, result.output
    assert body["schema"] == "piceli.envs.v1"
    assert body["envs"][0]["namespace"] == "shop" and body["envs"][0]["commit"] == "abc"
    assert "healthy" in result.stderr


def test_env_up_prints_the_approval_and_exits_3(tmp_path: Path) -> None:
    planned = {
        "schema": "piceli.env-result.v1",
        "state": "approval-required",
        "branch": "wp-a",
        "namespace": "shop-wp-a",
        "create_namespace": True,
        "stop": ["shop-wp-old"],
        "env_hash": "sha256:" + "a" * 64,
    }
    with patch("piceli.envs.ops.env_up", return_value=planned) as up:
        code, body, result = _run(
            "env",
            "up",
            "wp-a",
            "--pipeline",
            _entry(tmp_path),
            "--digest",
            "web=example/web@sha256:" + "2" * 64,
        )
    assert code == 3, result.output
    assert body["env_hash"] == planned["env_hash"]
    assert up.call_args.kwargs["digests"] == {"web": "example/web@sha256:" + "2" * 64}
    assert "stops shop-wp-old" in result.stderr
    assert f"--approve {planned['env_hash']}" in result.stderr


def test_env_up_rejects_a_bad_digest_flag(tmp_path: Path) -> None:
    code, body, _ = _run(
        "env", "up", "wp-a", "--pipeline", _entry(tmp_path), "--digest", "web"
    )
    assert code == 2 and body["reason"] == "env-image-invalid"


def test_env_up_refusal_carries_the_code(tmp_path: Path) -> None:
    code, body, _ = _run("env", "up", "feature-x", "--pipeline", _entry(tmp_path))
    assert code == 2 and body["reason"] == "env-branch-not-allowed"


def test_logs_refuses_an_unknown_workload(tmp_path: Path) -> None:
    code, body, _ = _run("logs", "wp-a", "nope", "--pipeline", _entry(tmp_path))
    assert code == 2 and body["reason"] == "env-workload-unknown"
    assert body["workloads"] == ["web"]


def test_logs_runs_kubectl_in_the_branch_namespace(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    (tmp_path / "kubeconfig").write_text(
        textwrap.dedent(
            """
            apiVersion: v1
            kind: Config
            clusters: [{name: k, cluster: {server: "https://k.example"}}]
            users: [{name: u, user: {}}]
            contexts: [{name: c, context: {cluster: k, user: u}}]
            """
        )
    )
    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("piceli.k8s.observe.run_logs", return_value=0) as run,
    ):
        code, _, result = _run(
            "logs", "wp-a", "web", "-f", "--previous", "--pipeline", entry
        )
    assert code == 0, result.output
    command = run.call_args.args[0]
    assert command[command.index("--namespace") + 1] == "shop-wp-a"
    assert (
        "deployment/web" in command
        and "--follow" in command
        and "--previous" in command
    )


def test_access_target_of_a_branch_uses_free_ports(tmp_path: Path) -> None:
    from piceli.envs.ops import env_access_target
    from piceli.k8s.cli.env import load

    pipeline = load(_entry(tmp_path))
    main = env_access_target(pipeline, "main", port=lambda: 40000)
    assert main.namespace == "shop" and main.shortcuts[0].local_port == 18080
    branch = env_access_target(pipeline, "wp-a", port=lambda: 40000)
    assert branch.namespace == "shop-wp-a"
    assert branch.shortcuts[0].local_port == 40000
    assert branch.shortcuts[0].namespace in {None, "shop-wp-a"}
