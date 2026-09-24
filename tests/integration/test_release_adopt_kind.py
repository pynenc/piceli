"""Opt-in: adopt kubectl-made objects with ``piceli release`` on a disposable kind.

Runs only when both variables name a disposable cluster and ``kubectl`` is on
``PATH``, for example::

    kind create cluster --name piceli-adopt --kubeconfig /tmp/adopt.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/adopt.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-adopt \\
      uv run pytest tests/integration/test_release_adopt_kind.py

Every ``kubectl`` call passes ``--kubeconfig`` and ``--context`` explicitly.
The test creates a unique namespace with a Deployment (``kubectl apply``, then
``kubectl set image`` and ``kubectl rollout restart``), a PVC holding a file
and a Secret; adopts all three through ``piceli release``; checks field
ownership, the data and the Secret value; checks that a later ``kubectl set
image`` is reported as drift; rolls back; and deletes the namespace.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
# nginx:1.27-alpine and nginx:1.26-alpine multi-arch index digests.
DIGEST_1 = "sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
DIGEST_2 = "sha256:1eadbb07820339e8bbfed18c771691970baee292ec4ab2558f1453d26153e22d"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT and shutil.which("kubectl")),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT; needs kubectl",
    ),
]

COMPOSITION = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent


def build(ctx):
    ns = ctx.namespace
    claim = ResourceIntent.from_manifest({
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": "web-data", "namespace": ns},
        "spec": {"accessModes": ["ReadWriteOnce"],
                 "resources": {"requests": {"storage": "64Mi"}}},
    })
    # Adopt the Secret without managing its value (no data declared).
    token = ResourceIntent.from_manifest({
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": "web-token", "namespace": ns},
    })
    web = ResourceIntent.from_manifest({
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "web", "namespace": ns},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "web"}},
                 "template": {"metadata": {"labels": {"app": "web"}}, "spec": {
                     "containers": [{"name": "web", "image": ctx.image("web"),
                                     "env": [{"name": "TOKEN", "valueFrom": {
                                         "secretKeyRef": {"name": "web-token",
                                                          "key": "token"}}}],
                                     "volumeMounts": [{"name": "data",
                                                       "mountPath": "/data"}]}],
                     "volumes": [{"name": "data", "persistentVolumeClaim": {
                         "claimName": "web-data"}}]}}},
    })
    return DeploymentComposition((
        DeploymentComponent("storage", (claim, token)),
        DeploymentComponent("web", (web,), dependencies=("storage",)),
    ))
"""

KUBECTL_OBJECTS = """
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: web-data}
spec:
  accessModes: [ReadWriteOnce]
  resources: {requests: {storage: 64Mi}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: web}
spec:
  replicas: 1
  selector: {matchLabels: {app: web}}
  template:
    metadata: {labels: {app: web}}
    spec:
      containers:
      - name: web
        image: nginx:1.26-alpine
        env:
        - name: TOKEN
          valueFrom: {secretKeyRef: {name: web-token, key: token}}
        volumeMounts: [{name: data, mountPath: /data}]
      volumes:
      - name: data
        persistentVolumeClaim: {claimName: web-data}
"""


def kubectl(*args: str, namespace: str | None = None, stdin: str = "") -> str:
    command = ["kubectl", "--kubeconfig", KUBECONFIG, "--context", CONTEXT]
    if namespace:
        command += ["--namespace", namespace]
    return subprocess.run(
        command + list(args),
        input=stdin,
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
        # Only the explicit kubeconfig; HOME keeps kubectl's cache out of the
        # working directory.
        env={
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/tmp"),
            "KUBECONFIG": KUBECONFIG,
        },
    ).stdout


@pytest.fixture
def namespace():
    name = "adopt-" + uuid.uuid4().hex[:8]
    kubectl("create", "namespace", name)
    try:
        yield name
    finally:
        kubectl("delete", "namespace", name, "--wait=false")


def _spec(directory: Path, namespace: str, digest: str) -> Path:
    (directory / "composition.py").write_text(COMPOSITION)
    path = directory / "release.toml"
    path.write_text(
        textwrap.dedent(
            f"""
            [target]
            kubeconfig = "{KUBECONFIG}"
            context = "{CONTEXT}"
            namespace = "{namespace}"

            [release]
            name = "web"
            owner = "adopt-e2e"
            field_manager = "adopt-e2e"
            composition = "composition.py:build"
            state_dir = "state"

            [execution]
            max_seconds = 300
            readiness_seconds = 240
            poll_seconds = 1

            [images]
            web = "docker.io/library/nginx@{digest}"
            """
        )
    )
    return path


def _cli(spec: Path, *args: str) -> tuple[int, dict]:
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}")


def _managers(namespace: str, kind: str, name: str) -> dict[str, dict]:
    value = json.loads(
        kubectl(
            "get",
            kind,
            name,
            "-o",
            "json",
            "--show-managed-fields",
            namespace=namespace,
        )
    )
    return {
        entry["manager"]: entry
        for entry in value["metadata"]["managedFields"]
        if entry.get("subresource") != "status"
    }


def test_adopt_kubectl_objects_then_drift_and_rollback(namespace, tmp_path):
    kubectl(
        "create",
        "secret",
        "generic",
        "web-token",
        "--from-literal=token=kept-value",
        namespace=namespace,
    )
    kubectl("apply", "-f", "-", namespace=namespace, stdin=KUBECTL_OBJECTS)
    kubectl("rollout", "status", "deploy/web", "--timeout=240s", namespace=namespace)
    kubectl(
        "exec",
        "deploy/web",
        "--",
        "sh",
        "-c",
        "echo kept > /data/hello.txt",
        namespace=namespace,
    )
    kubectl(
        "set", "image", "deployment/web", "web=nginx:1.27-alpine", namespace=namespace
    )
    kubectl("rollout", "restart", "deployment/web", namespace=namespace)
    kubectl("rollout", "status", "deploy/web", "--timeout=240s", namespace=namespace)
    pvc_before = json.loads(
        kubectl("get", "pvc", "web-data", "-o", "json", namespace=namespace)
    )

    spec = _spec(tmp_path, namespace, DIGEST_1)
    code, refused = _cli(spec, "plan")
    assert code == 2 and "--adopt" in refused["message"]
    adopt = [
        "--adopt",
        "Deployment/web",
        "--adopt",
        "PersistentVolumeClaim/web-data",
        "--adopt",
        "Secret/web-token",
    ]
    code, planned = _cli(spec, "plan", *adopt)
    assert code == 0, planned
    modes = {item["name"]: item["adoption"] for item in planned["actions"]}
    assert modes["web"]["mode"] == "takeover"
    assert {"kubectl-client-side-apply", "kubectl-set", "kubectl-rollout"} <= set(
        modes["web"]["transferred_managers"]
    )
    assert modes["web-data"]["mode"] == modes["web-token"]["mode"] == "metadata-only"

    code, applied = _cli(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, applied
    first = applied["release"]

    managers = _managers(namespace, "deployment", "web")
    assert not [name for name in managers if name.startswith("kubectl")]
    live = json.loads(
        kubectl("get", "deploy", "web", "-o", "json", namespace=namespace)
    )
    # Undeclared client-written fields are removed by the takeover.
    assert "kubectl.kubernetes.io/last-applied-configuration" not in live[
        "metadata"
    ].get("annotations", {})
    assert not live["spec"]["template"]["metadata"].get("annotations")
    owned = json.dumps(managers["adopt-e2e"]["fieldsV1"])
    assert "f:image" in owned and "f:selector" in owned
    pvc_after = json.loads(
        kubectl("get", "pvc", "web-data", "-o", "json", namespace=namespace)
    )
    assert pvc_after["spec"] == pvc_before["spec"]
    assert pvc_after["metadata"]["uid"] == pvc_before["metadata"]["uid"]
    assert pvc_after["metadata"]["annotations"]["piceli.io/owner"] == "adopt-e2e"
    assert (
        kubectl(
            "exec", "deploy/web", "--", "cat", "/data/hello.txt", namespace=namespace
        ).strip()
        == "kept"
    )
    token = kubectl(
        "get",
        "secret",
        "web-token",
        "-o",
        "jsonpath={.data.token}",
        namespace=namespace,
    )
    assert token == "a2VwdC12YWx1ZQ=="

    kubectl(
        "set", "image", "deployment/web", "web=nginx:1.26-alpine", namespace=namespace
    )
    code, planned = _cli(spec, "plan")
    assert code == 0, planned
    assert {
        (item["resource"]["name"], tuple(item["managers"])) for item in planned["drift"]
    } == {("web", ("kubectl-set",))}

    spec = _spec(tmp_path, namespace, DIGEST_2)
    code, applied = _cli(spec, "apply", "--auto-approve")
    assert code == 0, applied
    code, rolled = _cli(spec, "rollback", "previous", "--auto-approve")
    assert code == 0, rolled
    assert rolled["selected"] == first
    image = kubectl(
        "get",
        "deploy",
        "web",
        "-o",
        "jsonpath={.spec.template.spec.containers[0].image}",
        namespace=namespace,
    )
    assert image == f"docker.io/library/nginx@{DIGEST_1}"
    assert (
        kubectl(
            "exec", "deploy/web", "--", "cat", "/data/hello.txt", namespace=namespace
        ).strip()
        == "kept"
    )
