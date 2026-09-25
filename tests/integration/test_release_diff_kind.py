"""Opt-in: unchanged releases plan as all no-op on a real API server (kind).

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-diff --kubeconfig /tmp/diff.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/diff.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-diff \\
    PICELI_KIND_NODE=piceli-diff-control-plane \\
      uv run pytest tests/integration/test_release_diff_kind.py

The test never reads the ambient kubeconfig. In a uniquely named namespace it
releases the node-local registry (``examples/two-images/registry.py``) and a
workload (ConfigMap, bound Secret, PVC, Deployment, Service), re-plans both
unchanged, checks that every object (the bound Secret too) is a no-op and that planning
changed no object (server dry runs only), then changes one image and checks
that the diff shows exactly that field.
"""

from __future__ import annotations

import json
import os
import shutil
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
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "two-images"
# nginx:1.27-alpine and nginx:1.26-alpine multi-arch index digests.
DIGEST_1 = "sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
DIGEST_2 = "sha256:1eadbb07820339e8bbfed18c771691970baee292ec4ab2558f1453d26153e22d"

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


def build(ctx):
    meta = lambda name: {"name": name, "namespace": ctx.namespace}
    labels = {"app": "web"}
    config = ResourceIntent.from_manifest(
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("web-config"),
         "data": {"greeting": "hello"}}
    )
    token = ResourceIntent.from_manifest(
        {"apiVersion": "v1", "kind": "Secret", "metadata": meta("web-token"),
         "data": {"token": "<private>"}}
    ).with_secret("/data/token", ctx.secret("api-token"))
    claim = ResourceIntent.from_manifest(
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": meta("web-data"),
         "spec": {"accessModes": ["ReadWriteOnce"],
                  "resources": {"requests": {"storage": "64Mi"}}}}
    )
    web = ResourceIntent.from_manifest(
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta("web"),
         "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                  "template": {"metadata": {"labels": labels}, "spec": {
                      "containers": [{
                          "name": "web", "image": ctx.image("web"),
                          "ports": [{"containerPort": 80}],
                          "resources": {"requests": {"cpu": "0.05", "memory": "32Mi"}},
                          "envFrom": [{"configMapRef": {"name": "web-config"}}],
                          "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                      "volumes": [{"name": "data", "persistentVolumeClaim":
                                   {"claimName": "web-data"}}]}}}}
    )
    service = ResourceIntent.from_manifest(
        {"apiVersion": "v1", "kind": "Service", "metadata": meta("web"),
         "spec": {"selector": labels, "ports": [{"name": "http", "port": 80}]}}
    )
    return DeploymentComposition((
        DeploymentComponent("config", (config, token, claim)),
        DeploymentComponent("web", (web, service), dependencies=("config",)),
    ))
"""


@pytest.fixture
def namespace():
    from kubernetes.client import CoreV1Api

    name = "diff-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    api = CoreV1Api(client)
    api.create_namespace({"metadata": {"name": name}})
    try:
        yield name
    finally:
        api.delete_namespace(name)
        client.close()


def _target(namespace: str) -> str:
    return textwrap.dedent(
        f"""
        [target]
        kubeconfig = "{KUBECONFIG}"
        context = "{CONTEXT}"
        namespace = "{namespace}"
        [target.nodes.primary]
        name = "{NODE}"
        """
    )


def _specs(directory: Path, namespace: str, digest: str) -> tuple[Path, Path]:
    shutil.copy(EXAMPLE / "registry.py", directory / "registry.py")
    (directory / "web.py").write_text(COMPOSITION)
    registry = directory / "registry.toml"
    registry.write_text(
        _target(namespace)
        + textwrap.dedent(
            """
            [release]
            name = "registry"
            owner = "diff-registry-e2e"
            field_manager = "diff-registry-e2e"
            composition = "registry.py:build"
            state_dir = "registry-state"
            [execution]
            readiness_seconds = 240
            [images]
            registry = "docker.io/library/registry:3.1.1@sha256:325b4b29b041e82803abeb703e201655e4e23ab83264ec1a7c9ddb0a5b14a6e0"
            [values]
            registry_storage = "1Gi"
            """
        )
    )
    web = directory / "web.toml"
    web.write_text(
        _target(namespace)
        + textwrap.dedent(
            f"""
            [release]
            name = "web"
            owner = "diff-web-e2e"
            field_manager = "diff-web-e2e"
            composition = "web.py:build"
            state_dir = "web-state"
            [execution]
            readiness_seconds = 240
            [images]
            web = "docker.io/library/nginx@{digest}"
            [secrets.api-token]
            type = "random"
            """
        )
    )
    return registry, web


def _run(spec: Path, *args: str) -> tuple[int, dict, str]:
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}"), result.stderr


def _apply(spec: Path) -> dict:
    code, applied, stderr = _run(spec, "apply", "--auto-approve")
    if code != 0:
        # A WaitForFirstConsumer claim binds after the first apply.
        code, applied, stderr = _run(spec, "apply", "--auto-approve")
    assert code == 0, (applied, stderr)
    return applied


def _operations(payload: dict) -> dict[str, str]:
    return {f"{a['kind']}/{a['name']}": a["operation"] for a in payload["actions"]}


def _versions(namespace: str) -> dict[str, str]:
    from kubernetes.client import AppsV1Api, CoreV1Api

    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    try:
        core, apps = CoreV1Api(client), AppsV1Api(client)
        listed = {
            "ConfigMap": core.list_namespaced_config_map(namespace).items,
            "Secret": core.list_namespaced_secret(namespace).items,
            "PersistentVolumeClaim": core.list_namespaced_persistent_volume_claim(
                namespace
            ).items,
            "Service": core.list_namespaced_service(namespace).items,
            "Deployment": apps.list_namespaced_deployment(namespace).items,
        }
        return {
            f"{kind}/{item.metadata.name}": item.metadata.resource_version
            for kind, items in listed.items()
            for item in items
        }
    finally:
        client.close()


def test_unchanged_releases_plan_as_noop_and_one_image_diff(tmp_path, namespace):
    registry, web = _specs(tmp_path, namespace, DIGEST_1)
    _apply(registry)
    _apply(web)
    versions = _versions(namespace)

    code, planned, stderr = _run(registry, "plan")
    assert code == 0, (planned, stderr)
    assert set(_operations(planned).values()) == {"no-op"}, _operations(planned)
    assert planned["diffs"] == [] and planned["dry_run_unavailable"] == []

    code, planned, stderr = _run(web, "plan")
    assert code == 0, (planned, stderr)
    assert _operations(planned) == {
        "ConfigMap/web-config": "no-op",
        "PersistentVolumeClaim/web-data": "no-op",
        # Secret-bound values are compared privately (in-process).
        "Secret/web-token": "no-op",
        "Deployment/web": "no-op",
        "Service/web": "no-op",
    }, planned["diffs"]
    assert planned["dry_run_unavailable"] == []
    # Planning sent only reads and dry runs: no object changed.
    assert _versions(namespace) == versions

    registry, web = _specs(tmp_path, namespace, DIGEST_2)
    code, planned, stderr = _run(web, "plan")
    assert code == 0, (planned, stderr)
    assert _operations(planned)["Deployment/web"] == "apply"
    assert _operations(planned)["Service/web"] == "no-op"
    (diff,) = [d for d in planned["diffs"] if d["resource"]["kind"] == "Deployment"]
    assert diff["basis"] == "server-dry-run"
    assert diff["changes"] == [
        {
            "path": "/spec/template/spec/containers/0/image",
            "op": "replace",
            "before": f"docker.io/library/nginx@{DIGEST_1}",
            "after": f"docker.io/library/nginx@{DIGEST_2}",
        }
    ]
    code, diffed, stderr = _run(web, "diff")
    assert code == 0, (diffed, stderr)
    assert [
        line
        for line in stderr.splitlines()
        if line.startswith("+") and f"image: docker.io/library/nginx@{DIGEST_2}" in line
    ]
    assert [d["changes"] for d in diffed["diffs"] if d["resource"]["name"] == "web"]
    assert _versions(namespace) == versions

    code, applied, stderr = _run(web, "apply", "--approve", planned["plan_hash"])
    assert code == 0, (applied, stderr)
    code, again, stderr = _run(web, "plan")
    assert code == 0, (again, stderr)
    assert _operations(again) == {
        "ConfigMap/web-config": "no-op",
        "Secret/web-token": "no-op",
        "PersistentVolumeClaim/web-data": "no-op",
        "Deployment/web": "no-op",
        "Service/web": "no-op",
    }
