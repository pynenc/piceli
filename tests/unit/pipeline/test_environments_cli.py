"""Environments through the CLI and pipelines: ``--env``, ``--diff-env``, targets."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from piceli import App
from piceli.k8s.cli import app as cli
from piceli.pipeline import Pipeline, PipelineError, PipelineRunner, Target

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "environments" / "app.py"
IMAGE = "registry.example/shop/api@sha256:" + "1" * 64


def _render(*args: str) -> Any:
    return CliRunner().invoke(cli, ["render", *args])


def _documents(text: str) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(text)
        if doc
    }


def test_example_renders_three_environments() -> None:
    rendered = {}
    for env in ("dev", "staging", "prod"):
        result = _render(f"{EXAMPLE}:app", "--env", env)
        assert result.exit_code == 0, result.output
        rendered[env] = _documents(result.stdout)
    for env, documents in rendered.items():
        assert {d["metadata"].get("namespace") for d in documents.values()} == {
            f"shop-{env}"
        }
    assert ("ServiceMonitor", "api") not in rendered["dev"]  # disabled
    assert ("ServiceMonitor", "api") in rendered["prod"]
    replicas = {
        env: docs[("Deployment", "api")]["spec"]["replicas"]
        for env, docs in rendered.items()
    }
    assert replicas == {"dev": 1, "staging": 2, "prod": 3}
    hosts = {
        env: docs[("Certificate", "shop-tls")]["spec"]["dnsNames"]
        for env, docs in rendered.items()
    }
    assert hosts == {
        "dev": ["shop.dev.example.com"],
        "staging": ["shop.staging.example.com"],
        "prod": ["shop.example.com", "www.shop.example.com"],
    }
    assert rendered["prod"][("ConfigMap", "settings")]["data"]["LOG_LEVEL"] == "warning"
    # The pipeline renders each environment with its own target.
    result = _render(f"{EXAMPLE}:pipeline", "--env", "prod", "--format", "json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["namespace"] == "shop-prod" and payload["environment"] == "prod"


def test_diff_env_prints_a_typed_diff_as_text_and_json() -> None:
    result = _render(f"{EXAMPLE}:app", "--env", "dev", "--diff-env", "prod")
    assert result.exit_code == 0, result.output
    text = result.stdout
    assert text.startswith("environments: dev (a) -> prod (b)\n")
    assert "namespace: shop-dev -> shop-prod" in text
    assert "    replicas.api: (absent) -> 3" in text
    assert "~ Deployment/api (component api):" in text
    assert "+ ServiceMonitor/api (component monitoring): only in prod" in text
    result = _render(
        f"{EXAMPLE}:pipeline",
        "--env",
        "staging",
        "--diff-env",
        "prod",
        "--format",
        "json",
    )
    assert result.exit_code == 0, result.output
    diff = json.loads(result.stdout)
    assert diff["state"] == "diffed"
    assert diff["environments"] == {"a": "staging", "b": "prod"}
    assert diff["namespaces"] == {"a": "shop-staging", "b": "shop-prod"}
    changed = {(o["kind"], o["name"]): o for o in diff["objects"]}
    assert changed[("Deployment", "api")]["fields"][0] == {
        "path": "spec.replicas",
        "a": 2,
        "b": 3,
    }
    assert changed[("Certificate", "shop-tls")]["fields"] == [
        {
            "path": "spec.dnsNames",
            "a": ["shop.staging.example.com"],
            "b": ["shop.example.com", "www.shop.example.com"],
        }
    ]
    assert {"path": "replicas.api", "a": 2, "b": 3} in diff["values"]


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (["{app}", "--diff-env", "prod"], "environment-required"),
        (["{app}", "--env", "qa"], "environment-unknown"),
        (["{pipeline}"], "environment-required"),
        (["{pipeline}", "--env", "qa"], "environment-unknown"),
        (["{composition}", "--env", "prod"], "environment-unsupported"),
        (["{invalid}", "--env", "prod"], "environment-invalid"),
    ],
)
def test_render_environment_rejections(tmp_path: Path, args, reason) -> None:
    module = tmp_path / "extra.py"
    module.write_text(
        textwrap.dedent(
            """
            from piceli import App
            from piceli.app.environment import Environment
            from piceli.k8s.ops.plan import DeploymentComposition

            composition = DeploymentComposition(())
            invalid = App("x")
            invalid._environments["prod"] = Environment("prod", replicas={"api": 2})
            """
        )
    )
    names = {
        "app": f"{EXAMPLE}:app",
        "pipeline": f"{EXAMPLE}:pipeline",
        "composition": f"{module}:composition",
        "invalid": f"{module}:invalid",
    }
    argv = [item.format(**names) for item in args]
    result = _render(*argv, "--format", "json")
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["reason"] == reason


# ------------------------------------------------------------------ pipelines


def _app() -> App:
    app = App("shop")
    app.deployment("api", image=IMAGE)
    app.environment("dev", replicas={"api": 1})
    app.environment("prod", replicas={"api": 3})
    return app


def _target(namespace: str, context: str = "main") -> Target:
    return Target(
        "cluster.kubeconfig", context=context, namespace=namespace, base=Path("/work")
    )


def test_pipeline_targets_per_environment(tmp_path: Path) -> None:
    pipeline = Pipeline(
        _app(),
        {"dev": _target("shop-dev"), "prod": _target("shop-prod")},
        state_dir=tmp_path / "state",
    )
    assert pipeline.needs_environment
    with pytest.raises(PipelineError, match="pass --env NAME") as error:
        pipeline.target  # noqa: B018
    assert error.value.code == "environment-required"
    assert "targets=['dev', 'prod']" in repr(pipeline)
    prod = pipeline.for_environment("prod")
    assert not prod.needs_environment
    assert prod.target.namespace == "shop-prod"
    assert prod.state_dir == tmp_path / "state" / "environments" / "prod"
    assert prod.environment is not None and prod.environment.name == "prod"
    assert prod.app.selected_environment is prod.environment
    assert pipeline.environment is None  # the declaring pipeline is unchanged
    with pytest.raises(PipelineError, match="no target for environment 'qa'") as error:
        pipeline.for_environment("qa")
    assert error.value.code == "environment-unknown"
    with pytest.raises(PipelineError, match="already environment 'prod'"):
        prod.for_environment("prod")


def test_single_target_pipelines_are_unchanged_and_take_no_env() -> None:
    pipeline = Pipeline(_app(), _target("shop"))
    assert not pipeline.needs_environment and pipeline.targets == {}
    assert pipeline.environment is None
    with pytest.raises(PipelineError) as error:
        pipeline.for_environment("prod")
    assert error.value.code == "environment-unknown"


@pytest.mark.parametrize(
    ("targets", "message"),
    [
        ({}, "Target or a mapping"),
        ({"qa": "not-a-target"}, "must be a Target"),
        ({"qa": None}, "must be a Target"),
        ({"stage": "x"}, "must be a Target"),
    ],
)
def test_pipeline_target_mapping_is_validated(targets, message) -> None:
    with pytest.raises(PipelineError, match=message) as error:
        Pipeline(_app(), targets)
    assert error.value.code == "pipeline-invalid"


def test_pipeline_targets_name_declared_environments_with_distinct_namespaces() -> None:
    with pytest.raises(PipelineError, match="environment 'qa', which the app"):
        Pipeline(_app(), {"qa": _target("shop-qa")})
    with pytest.raises(PipelineError, match="same context and namespace"):
        Pipeline(_app(), {"dev": _target("shop"), "prod": _target("shop")})
    # The same namespace name in two contexts (two clusters) is fine.
    Pipeline(_app(), {"dev": _target("shop", "dev"), "prod": _target("shop", "prod")})


def test_combined_plan_identity_covers_the_environment(tmp_path: Path) -> None:
    targets = {"dev": _target("shop-dev"), "prod": _target("shop-prod")}
    pipeline = Pipeline(_app(), targets, state_dir=tmp_path)
    identity = PipelineRunner(pipeline.for_environment("prod")).identity()
    assert identity["environment"] == {
        "name": "prod",
        "values": {"replicas": {"api": 3}},
    }
    # Other values, same name: another identity (so another combined hash).
    other = _app()
    other._environments["prod"] = other._environments["prod"].model_copy(
        update={"replicas": {"api": 4}}
    )
    changed = PipelineRunner(
        Pipeline(other, targets, state_dir=tmp_path).for_environment("prod")
    ).identity()
    assert changed["environment"]["values"] == {"replicas": {"api": 4}}
    assert changed != identity
    # Without environments nothing is added: earlier plans keep their hashes.
    single = PipelineRunner(Pipeline(_app(), _target("shop"), state_dir=tmp_path))
    assert "environment" not in single.identity()


def test_deploy_and_release_take_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["deploy", f"{EXAMPLE}:pipeline", "--plan"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "environment-required"
    result = runner.invoke(
        cli, ["deploy", f"{EXAMPLE}:pipeline", "--env", "qa", "--plan"]
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "environment-unknown"
    result = runner.invoke(cli, ["release", "status", "--spec", f"{EXAMPLE}:pipeline"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "environment-required"
    spec = ROOT / "examples" / "release" / "release.toml"
    result = runner.invoke(
        cli, ["release", "status", "--spec", str(spec), "--env", "prod"]
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "environment-unsupported"


def test_status_refuses_an_unselected_multi_environment_pipeline() -> None:
    result = CliRunner().invoke(cli, ["status", f"{EXAMPLE}:pipeline"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "environment-required"
