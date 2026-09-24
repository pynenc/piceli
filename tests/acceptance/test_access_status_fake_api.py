"""Acceptance: a composition that returns its App plans, applies, and reports status.

The release runner renders a returned App, and ``piceli status`` reads the
live Deployment and Pods through the real Kubernetes client (loopback fake
API) and the release state from the state directory.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import jsonschema
import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app
from tests.acceptance import fake_api
from tests.acceptance.fake_api import TARGET, serve

DIGEST = "sha256:" + "4" * 64
SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "schemas"
        / "piceli-status-v1.schema.json"
    ).read_text()
)

COMPOSITION = textwrap.dedent(
    """\
    from piceli import App


    def build(ctx):
        app = App("shop")
        app.deployment(
            "web",
            image=ctx.image("web"),
            ports=[80],
            replicas=2,
            access=app.access.forward(local=1, path="/login"),
        )
        return app
    """
)


def test_returned_app_applies_and_status_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(fake_api.TYPES, "pods", ("v1", "Pod", True))
    (tmp_path / "composition.py").write_text(COMPOSITION)
    runner = CliRunner()
    with serve() as (api, url):
        (tmp_path / "kubeconfig").write_text(
            textwrap.dedent(
                f"""
                apiVersion: v1
                kind: Config
                clusters: [{{name: fake, cluster: {{server: "{url}"}}}}]
                users: [{{name: nobody, user: {{}}}}]
                contexts: [{{name: fake, context: {{cluster: fake, user: nobody}}}}]
                """
            )
        )
        spec = tmp_path / "release.toml"
        spec.write_text(
            textwrap.dedent(
                f"""
                [target]
                kubeconfig = "kubeconfig"
                context = "fake"
                namespace = "{TARGET.namespace}"
                transport = "loopback-http"

                [release]
                name = "shop"
                owner = "shop"
                field_manager = "shop"
                composition = "composition.py:build"
                state_dir = "state"

                [execution]
                max_seconds = 30
                readiness_seconds = 1
                poll_seconds = 0.05

                [images]
                web = "registry.example/web@{DIGEST}"
                """
            )
        )
        planned = runner.invoke(app, ["release", "plan", "--spec", str(spec)])
        assert planned.exit_code == 0, planned.output
        plan_hash = json.loads(planned.stdout)["plan_hash"]
        applied = runner.invoke(
            app, ["release", "apply", "--spec", str(spec), "--approve", plan_hash]
        )
        assert applied.exit_code == 0, applied.output
        assert ("Deployment", "web") in api.objects
        for index in (1, 2):
            api.objects[("Pod", f"web-{index}")] = {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": f"web-{index}",
                    "namespace": TARGET.namespace,
                    "labels": {"app.kubernetes.io/name": "web"},
                },
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {
                            "name": "web",
                            "image": f"registry.example/web@{DIGEST}",
                            "ready": True,
                            "restartCount": 0,
                            "imageID": f"registry.example/web@{DIGEST}",
                        }
                    ],
                },
            }
        result = runner.invoke(app, ["status", str(spec), "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    jsonschema.validate(document, SCHEMA)
    assert document["state"] == "up"
    assert document["release"]["state"] == "ready"
    assert document["release"]["current"].startswith("shop-")
    (web,) = document["workloads"]
    assert web["health"] == "ready"
    assert web["replicas"]["ready"] == 2
    assert web["error"] is None
    assert web["images"][0] == {
        "container": "web",
        "image": f"registry.example/web@{DIGEST}",
        "digest": DIGEST,
        "pinned": True,
        "running": [DIGEST],
    }
    (forward,) = document["access"]["forwards"]
    assert forward["url"] == "http://127.0.0.1:1/login"
    assert forward["target"] == "deployment/web"
    assert forward["remote_port"] == 80
