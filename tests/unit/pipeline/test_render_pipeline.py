"""``piceli render MODULE:pipeline`` renders a Pipeline offline with its target."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli

PINNED = "docker.io/library/redis:7.2.3@sha256:" + "d" * 64

PIPELINE = f"""
from piceli import App, Build, NodeLoopbackRegistry, Pipeline, Random, Secrets, Target

target = Target.kubeconfig(
    "missing.kubeconfig", context="kind-shop", namespace="shop",
    nodes={{"edge": "shop-worker", "core": "shop-control-plane"}},
)
app = App("shop")
secrets = Secrets(password=Random(24))
images = Build.spec("build.toml")
credentials = app.secret("credentials", {{"password": secrets.ref("password")}})
app.deployment(
    "cache", image="{PINNED}", node="core",
    env={{"PASSWORD": credentials.key("password")}},
)
app.deployment("web", image=images["web"], ports=[8080])
pipeline = Pipeline(
    app, target, build=images, deliver=NodeLoopbackRegistry(node="edge"),
    secrets=secrets,
)
"""


def _module(tmp_path: Path) -> Path:
    path = tmp_path / "app.py"
    path.write_text(textwrap.dedent(PIPELINE))
    return path


def _render(*args: str) -> Any:
    return CliRunner().invoke(cli, ["render", *args])


def _pod(document: dict[str, Any]) -> dict[str, Any]:
    return dict(document["spec"]["template"]["spec"])


def test_render_pipeline_uses_target_namespace_nodes_and_placeholders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module(tmp_path)
    # Offline: no kubeconfig (it does not exist), no build spec, no state.
    monkeypatch.chdir(tmp_path)
    result = _render(f"{module}:pipeline")
    assert result.exit_code == 0, result.output
    documents = {
        (d["kind"], d["metadata"]["name"]): d
        for d in yaml.safe_load_all(result.stdout)
        if d
    }
    assert {d["metadata"]["namespace"] for d in documents.values()} == {"shop"}
    cache = _pod(documents[("Deployment", "cache")])
    web = _pod(documents[("Deployment", "web")])
    # node="core" resolves through the Target's declared nodes.
    assert cache["nodeSelector"] == {"kubernetes.io/hostname": "shop-control-plane"}
    assert cache["containers"][0]["image"] == PINNED
    # A built image stays a placeholder and is pinned to the delivery node.
    assert web["containers"][0]["image"] == "pipeline.piceli.invalid/web:unresolved"
    assert web["nodeSelector"] == {"kubernetes.io/hostname": "shop-worker"}
    # Secret values are placeholders, never generated.
    assert documents[("Secret", "credentials")]["data"] == {"password": "<redacted>"}
    assert not (tmp_path / "missing.kubeconfig").exists()
    assert not (tmp_path / ".piceli-deploy").exists()
    written = {path.name for path in tmp_path.iterdir()} - {"__pycache__"}
    assert written == {"app.py"}


def test_render_pipeline_json_names_secret_inputs(tmp_path: Path) -> None:
    module = _module(tmp_path)
    result = _render(f"{module}:pipeline", "--format", "json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["state"] == "rendered" and payload["namespace"] == "shop"
    bindings = [
        binding
        for component in payload["components"]
        for resource in component["resources"]
        for binding in resource["secret_bindings"]
    ]
    assert {binding["input"] for binding in bindings} == {"password"}


def test_render_pipeline_refuses_another_namespace_or_a_spec(tmp_path: Path) -> None:
    module = _module(tmp_path)
    result = _render(f"{module}:pipeline", "--namespace", "shop")
    assert result.exit_code == 0, result.output
    result = _render(f"{module}:pipeline", "--namespace", "other", "--format", "json")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "render-target-invalid"
    spec = tmp_path / "release.toml"
    spec.write_text("")
    result = _render(f"{module}:pipeline", "--spec", str(spec), "--format", "json")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "render-target-invalid"


def test_render_app_still_uses_an_empty_context(tmp_path: Path) -> None:
    module = _module(tmp_path)
    result = _render(f"{module}:app", "--namespace", "shop", "--format", "json")
    # The App alone knows no nodes: node="core" is refused as before.
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "render-model-invalid"
