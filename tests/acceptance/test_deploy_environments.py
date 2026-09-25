"""Acceptance: one pipeline, one target per environment, ``piceli deploy --env``.

Two fake API servers stand for two clusters (the ``dev`` and ``prod``
contexts of one kubeconfig). The combined hash covers the environment's name
and resolved values, each environment keeps its own state, and a deploy to
one never touches the other.
"""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from piceli.testing import TARGET, TYPES, FakeAPI, serve

ROOT = Path(__file__).resolve().parents[2]
TYPES_WITH_CRDS = {
    **TYPES,
    "certificates": ("cert-manager.io/v1", "Certificate", True),
}

PIPELINE = f"""
from crds import certificate

from piceli import App, Pipeline, Target

IMAGE = "registry.example/shop/api@sha256:" + "7" * 64

app = App("shop")
settings = app.config("settings", {{"LOG_LEVEL": "debug"}})
api = app.deployment("api", image=IMAGE, env={{"LOG_LEVEL": settings.key("LOG_LEVEL")}})
app.resource(
    certificate.API_VERSION,
    certificate.KIND,
    "shop-tls",
    certificate.CertificateSpec(
        secret_name="shop-tls",
        dns_names=["shop.dev.example.com"],
        issuer_ref={{"name": "letsencrypt"}},
    ),
)
app.environment("dev")
app.environment(
    "prod",
    replicas={{"api": 3}},
    namespace=NAMESPACE,
    config={{"settings": {{"LOG_LEVEL": "warning"}}}},
    specs={{
        "shop-tls": {{
            "secretName": "shop-tls",
            "dnsNames": ["shop.example.com"],
            "issuerRef": {{"name": "letsencrypt"}},
        }}
    }},
)


def target(context):
    return Target.kubeconfig(
        "kubeconfig", context=context, namespace="{TARGET.namespace}",
        transport="loopback-http",
    )


pipeline = Pipeline(
    app,
    {{"dev": target("dev"), "prod": target("prod")}},
    state_dir="state",
    execution={{"max_seconds": 30, "readiness_seconds": 1, "poll_seconds": 0.05}},
)
"""


def _write(directory: Path, name: str = "app.py", namespace: str | None = None) -> None:
    (directory / name).write_text(PIPELINE.replace("NAMESPACE", repr(namespace)))


def _deploy(
    directory: Path, *args: str, module: str = "app.py"
) -> tuple[int, list[dict[str, Any]], Any]:
    result = CliRunner().invoke(
        cli, ["deploy", str(directory / module) + ":pipeline", *args]
    )
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return result.exit_code, lines, result


def _replicas(api: FakeAPI) -> int | None:
    deployment = api.objects.get(("Deployment", "api"))
    return None if deployment is None else deployment["spec"]["replicas"]


def test_deploy_one_environment_at_a_time(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "examples" / "environments" / "crds", tmp_path / "crds")
    _write(tmp_path)
    dev, prod = FakeAPI(types=TYPES_WITH_CRDS), FakeAPI(types=TYPES_WITH_CRDS)
    with serve(dev) as (dev, dev_url), serve(prod) as (prod, prod_url):
        (tmp_path / "kubeconfig").write_text(
            textwrap.dedent(
                f"""
                apiVersion: v1
                kind: Config
                current-context: must-not-be-used
                clusters:
                - {{name: dev, cluster: {{server: "{dev_url}"}}}}
                - {{name: prod, cluster: {{server: "{prod_url}"}}}}
                users: [{{name: nobody, user: {{}}}}]
                contexts:
                - {{name: dev, context: {{cluster: dev, user: nobody}}}}
                - {{name: prod, context: {{cluster: prod, user: nobody}}}}
                - {{name: must-not-be-used, context: {{cluster: dev, user: nobody}}}}
                """
            )
        )
        code, lines, result = _deploy(tmp_path, "--plan", "--json")
        assert code == 2, result.output
        assert lines[-1]["reason"] == "environment-required"

        code, lines, result = _deploy(tmp_path, "--env", "prod", "--plan", "--json")
        assert code == 0, result.output
        planned = lines[-1]
        assert planned["state"] == "planned"
        assert planned["environment"]["name"] == "prod"
        assert planned["environment"]["values"]["replicas"] == {"api": 3}
        prod_hash = planned["combined_hash"]
        assert f"--env prod --approve {prod_hash}" in result.stderr

        code, lines, result = _deploy(tmp_path, "--env", "dev", "--plan", "--json")
        assert code == 0, result.output
        dev_hash = lines[-1]["combined_hash"]
        assert dev_hash != prod_hash

        # Approving one environment's hash for another is refused.
        code, lines, result = _deploy(tmp_path, "--env", "prod", "--approve", dev_hash)
        assert code == 2, result.output
        assert lines[-1]["reason"] == "pipeline-plan-changed"

        # Other resolved values mean another hash, even with the same manifests
        # (a pipeline deploys to its target's namespace, not the declared one).
        _write(tmp_path, "other.py", namespace="shop-prod")
        code, lines, result = _deploy(
            tmp_path, "--env", "prod", "--plan", "--json", module="other.py"
        )
        assert code == 0, result.output
        assert lines[-1]["environment"]["values"]["namespace"] == "shop-prod"
        assert (
            lines[-1]["stages"]["plan"]["changes"]
            == planned["stages"]["plan"]["changes"]
        )
        assert lines[-1]["combined_hash"] != prod_hash

        code, lines, result = _deploy(
            tmp_path, "--env", "prod", "--approve", prod_hash, "--json"
        )
        assert code == 0, result.output
        assert lines[-1]["state"] == "ready", lines[-1]
        assert _replicas(prod) == 3 and _replicas(dev) is None
        assert prod.objects[("ConfigMap", "settings")]["data"] == {
            "LOG_LEVEL": "warning"
        }
        assert prod.objects[("Certificate", "shop-tls")]["spec"]["dnsNames"] == [
            "shop.example.com"
        ]
        assert (tmp_path / "state" / "environments" / "prod").is_dir()

        code, lines, result = _deploy(tmp_path, "--env", "dev", "--auto-approve")
        assert code == 0, result.output
        assert _replicas(dev) == 1 and _replicas(prod) == 3
        assert dev.objects[("ConfigMap", "settings")]["data"] == {"LOG_LEVEL": "debug"}

        # The release commands operate on one environment's release.
        runner = CliRunner()
        spec = str(tmp_path / "app.py:pipeline")
        result = runner.invoke(
            cli, ["release", "status", "--spec", spec, "--env", "prod"]
        )
        assert result.exit_code == 0, result.output
        status = json.loads(result.stdout)
        assert status["deployed"] is not None
        result = runner.invoke(
            cli, ["release", "plan", "--spec", spec, "--env", "prod"]
        )
        assert result.exit_code == 0, result.output
        assert set(json.loads(result.stdout)["summary"]) == {"no-op"}
        assert "--env prod --approve" in result.stderr


def test_a_plan_file_keeps_its_environment(tmp_path: Path) -> None:
    """``--plan --out`` records ``--env``; ``--apply`` deploys that environment."""
    shutil.copytree(ROOT / "examples" / "environments" / "crds", tmp_path / "crds")
    _write(tmp_path)
    dev, prod = FakeAPI(types=TYPES_WITH_CRDS), FakeAPI(types=TYPES_WITH_CRDS)
    with serve(dev) as (dev, dev_url), serve(prod) as (prod, prod_url):
        (tmp_path / "kubeconfig").write_text(
            textwrap.dedent(
                f"""
                apiVersion: v1
                kind: Config
                clusters:
                - {{name: dev, cluster: {{server: "{dev_url}"}}}}
                - {{name: prod, cluster: {{server: "{prod_url}"}}}}
                users: [{{name: nobody, user: {{}}}}]
                contexts:
                - {{name: dev, context: {{cluster: dev, user: nobody}}}}
                - {{name: prod, context: {{cluster: prod, user: nobody}}}}
                """
            )
        )
        plan_file = tmp_path / "deploy-plan.json"
        code, lines, result = _deploy(
            tmp_path, "--env", "prod", "--plan", "--out", str(plan_file), "--json"
        )
        assert code == 0, result.output
        combined = lines[-1]["combined_hash"]
        assert json.loads(plan_file.read_text())["environment"] == "prod"

        code, lines, result = _deploy(
            tmp_path, "--apply", str(plan_file), "--approve", combined, "--env", "dev"
        )
        assert code == 2, result.output
        assert lines[-1]["reason"] == "deploy-plan-file-mismatch"
        assert _replicas(dev) is None and _replicas(prod) is None

        result = CliRunner().invoke(
            cli,
            ["deploy", "--apply", str(plan_file), "--approve", combined, "--json"],
        )
        assert result.exit_code == 0, result.output
        lines = [json.loads(x) for x in result.stdout.splitlines() if x.strip()]
        assert lines[-1]["state"] == "ready", lines[-1]
        assert _replicas(prod) == 3 and _replicas(dev) is None
