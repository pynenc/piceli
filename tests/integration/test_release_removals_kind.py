"""Opt-in: unchanged secrets are no-ops and dropped keys are removed (kind).

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-removals --kubeconfig /tmp/removals.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/removals.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-removals \\
    PICELI_KIND_NODE=piceli-removals-control-plane \\
      uv run pytest tests/integration/test_release_removals_kind.py

The test never reads the ambient kubeconfig. In a uniquely named namespace it
releases a ConfigMap (with a label and a key), a bound Secret and a Deployment
(with two env vars), then:

* re-plans unchanged: every object, the Secret included, is ``no-op``, and
  applying that plan writes nothing;
* drops the ConfigMap key and label and one env var from the composition: the
  plan lists the removals, applying it removes them live, a key another writer
  added survives, and the Secret of the new release is still ``no-op``.
"""

from __future__ import annotations

import json
import os
import textwrap
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
NODE = os.environ.get("PICELI_KIND_NODE", "")
# nginx:1.27-alpine multi-arch index digest.
DIGEST = "sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT and NODE),
        reason="set PICELI_KIND_KUBECONFIG, PICELI_KIND_CONTEXT and PICELI_KIND_NODE",
    ),
]

COMPOSITION = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent

FULL = {full}


def build(ctx):
    meta = lambda name: {{"name": name, "namespace": ctx.namespace}}
    labels = {{"app": "web"}}
    settings_meta = meta("web-settings")
    data = {{"greeting": "hello"}}
    env = [{{"name": "MODE", "value": "blue"}}]
    if FULL:
        settings_meta["labels"] = {{"tier": "front"}}
        data["extra"] = "remove-me"
        env.append({{"name": "LEGACY", "value": "1"}})
    settings = ResourceIntent.from_manifest(
        {{"apiVersion": "v1", "kind": "ConfigMap", "metadata": settings_meta,
         "data": data}}
    )
    token = ResourceIntent.from_manifest(
        {{"apiVersion": "v1", "kind": "Secret", "metadata": meta("web-token"),
         "data": {{"token": "<private>"}}}}
    ).with_secret("/data/token", ctx.secret("api-token"))
    web = ResourceIntent.from_manifest(
        {{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta("web"),
         "spec": {{"replicas": 1, "selector": {{"matchLabels": labels}},
                  "template": {{"metadata": {{"labels": labels}}, "spec": {{
                      "containers": [{{
                          "name": "web", "image": ctx.image("web"),
                          "env": env,
                          "resources": {{"requests": {{"cpu": "0.05", "memory": "32Mi"}}}},
                          "envFrom": [{{"configMapRef": {{"name": "web-settings"}}}}]}}]}}}}}}}}
    )
    return DeploymentComposition((
        DeploymentComponent("config", (settings, token)),
        DeploymentComponent("web", (web,), dependencies=("config",)),
    ))
"""


@pytest.fixture
def namespace():
    from kubernetes.client import CoreV1Api

    name = "removals-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    api = CoreV1Api(client)
    api.create_namespace({"metadata": {"name": name}})
    try:
        yield name
    finally:
        api.delete_namespace(name)
        client.close()


def _spec(directory: Path, namespace: str, module: str) -> Path:
    # Composition modules are cached per path, so each version is a file.
    for name, full in (("full.py", True), ("trimmed.py", False)):
        (directory / name).write_text(COMPOSITION.format(full=full))
    spec = directory / "web.toml"
    spec.write_text(
        textwrap.dedent(
            f"""
            [target]
            kubeconfig = "{KUBECONFIG}"
            context = "{CONTEXT}"
            namespace = "{namespace}"
            [target.nodes.primary]
            name = "{NODE}"
            [release]
            name = "web"
            owner = "removals-web-e2e"
            field_manager = "removals-web-e2e"
            composition = "{module}:build"
            state_dir = "state"
            [execution]
            readiness_seconds = 240
            [images]
            web = "docker.io/library/nginx@{DIGEST}"
            [secrets.api-token]
            type = "random"
            """
        )
    )
    return spec


def _run(spec: Path, *args: str) -> tuple[int, dict, str]:
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}"), result.stderr


def _operations(payload: dict) -> dict[str, str]:
    return {f"{a['kind']}/{a['name']}": a["operation"] for a in payload["actions"]}


def _live(namespace: str) -> dict:
    from kubernetes.client import AppsV1Api, CoreV1Api

    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    try:
        core, apps = CoreV1Api(client), AppsV1Api(client)
        settings = core.read_namespaced_config_map("web-settings", namespace)
        token = core.read_namespaced_secret("web-token", namespace)
        web = apps.read_namespaced_deployment("web", namespace)
        return {
            "data": dict(settings.data or {}),
            "labels": dict(settings.metadata.labels or {}),
            "env": [item.name for item in web.spec.template.spec.containers[0].env],
            "versions": {
                "ConfigMap": settings.metadata.resource_version,
                "Secret": token.metadata.resource_version,
                "Deployment": web.metadata.resource_version,
            },
        }
    finally:
        client.close()


def _add_foreign_key(namespace: str) -> None:
    from kubernetes.client import CoreV1Api

    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    try:
        CoreV1Api(client).patch_namespaced_config_map(
            "web-settings",
            namespace,
            {"data": {"foreign": "keep"}},
            field_manager="another-writer",
        )
    finally:
        client.close()


def test_unchanged_secret_is_noop_and_dropped_keys_are_removed(tmp_path, namespace):
    spec = _spec(tmp_path, namespace, "full.py")
    code, applied, stderr = _run(spec, "apply", "--auto-approve")
    assert code == 0, (applied, stderr)
    before = _live(namespace)
    assert before["data"] == {"greeting": "hello", "extra": "remove-me"}
    assert before["labels"] == {"tier": "front"}
    assert before["env"] == ["MODE", "LEGACY"]

    # Re-deploying unchanged: every object, the bound Secret too, is a no-op.
    code, planned, stderr = _run(spec, "plan")
    assert code == 0, (planned, stderr)
    assert _operations(planned) == {
        "ConfigMap/web-settings": "no-op",
        "Secret/web-token": "no-op",
        "Deployment/web": "no-op",
    }, planned["diffs"]
    assert planned["diffs"] == []
    code, outcome, stderr = _run(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, (outcome, stderr)
    assert _live(namespace)["versions"] == before["versions"]

    # Another writer adds a key the release never declared.
    _add_foreign_key(namespace)

    # Drop a ConfigMap key and label and an env var from the composition.
    spec = _spec(tmp_path, namespace, "trimmed.py")
    code, planned, stderr = _run(spec, "plan")
    assert code == 0, (planned, stderr)
    assert planned["mode"] == "create"
    operations = _operations(planned)
    assert operations == {
        "ConfigMap/web-settings": "apply",
        # A new release carries the secret value over: still unchanged.
        "Secret/web-token": "no-op",
        "Deployment/web": "apply",
    }, planned["diffs"]
    (settings,) = [a for a in planned["actions"] if a["name"] == "web-settings"]
    assert settings["removes"] == ["/data/extra", "/metadata/labels/tier"]
    diffs = {item["resource"]["kind"]: item for item in planned["diffs"]}
    assert {
        (change["path"], change["op"]) for change in diffs["ConfigMap"]["changes"]
    } == {("/data/extra", "remove"), ("/metadata/labels/tier", "remove")}
    assert [
        change["op"]
        for change in diffs["Deployment"]["changes"]
        if change["path"].startswith("/spec/template/spec/containers/0/env")
    ] == ["remove"]
    token = _live(namespace)
    code, outcome, stderr = _run(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, (outcome, stderr)
    assert outcome["release_state"] == "ready"

    after = _live(namespace)
    assert after["data"] == {"greeting": "hello", "foreign": "keep"}
    assert after["labels"] == {}
    assert after["env"] == ["MODE"]
    assert after["versions"]["Secret"] == token["versions"]["Secret"]

    code, again, stderr = _run(spec, "plan")
    assert code == 0, (again, stderr)
    assert set(_operations(again).values()) == {"no-op"}, again["diffs"]
