"""Opt-in: import a kubectl-made namespace from a disposable kind cluster.

Runs only when both variables name a disposable cluster and ``kubectl`` is on
``PATH``, for example::

    kind create cluster --name piceli-import --kubeconfig /tmp/import.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/import.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-import \\
      uv run pytest tests/integration/test_import_kind.py

Every ``kubectl`` call passes ``--kubeconfig`` and ``--context`` explicitly.
The test creates a unique namespace with a ConfigMap, a Secret, a
PersistentVolumeClaim and a Deployment (``kubectl create``) and a Service
(``kubectl expose``); imports it with ``piceli import live``; releases the
generated module with ``--adopt-all-desired``; and checks that nothing
changed: no rollout (same pod template, ReplicaSets and pods), the same
cluster IP, the same data and Secret value, and every field the module
renders equals the live field. A second plan must hold no create, delete or
replace, and applying it changes nothing either. (The Deployment's
``generation`` does move: the API server bumps it when annotations change,
and adoption writes the owner annotation.)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from piceli.k8s.cli.release import app as release
from piceli.k8s.ops.plan import manifest_contains

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT and shutil.which("kubectl")),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT; needs kubectl",
    ),
]

OBJECTS = """
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: web-data}
spec:
  accessModes: [ReadWriteOnce]
  resources: {requests: {storage: 64Mi}}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  labels: {app: web}
spec:
  replicas: 1
  selector: {matchLabels: {app: web}}
  template:
    metadata: {labels: {app: web}}
    spec:
      containers:
      - name: nginx
        image: nginx:1.27-alpine
        ports: [{containerPort: 80}]
        env:
        - name: GREETING
          valueFrom: {configMapKeyRef: {name: web-settings, key: greeting}}
        - name: TOKEN
          valueFrom: {secretKeyRef: {name: web-token, key: token}}
        readinessProbe:
          httpGet: {path: /, port: 80}
          periodSeconds: 2
        securityContext: {allowPrivilegeEscalation: false}
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
        env={
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/tmp"),
            "KUBECONFIG": KUBECONFIG,
        },
    ).stdout


@pytest.fixture
def namespace():
    name = "import-" + uuid.uuid4().hex[:8]
    kubectl("create", "namespace", name)
    try:
        yield name
    finally:
        kubectl("delete", "namespace", name, "--wait=false")


def _get(namespace: str, kind: str, name: str) -> dict[str, Any]:
    return json.loads(kubectl("get", kind, name, "-o", "json", namespace=namespace))


def _state(namespace: str) -> dict[str, Any]:
    deployment = _get(namespace, "deployment", "web")
    replica_sets = json.loads(
        kubectl(
            "get", "replicasets", "-l", "app=web", "-o", "json", namespace=namespace
        )
    )["items"]
    pods = json.loads(
        kubectl("get", "pods", "-l", "app=web", "-o", "json", namespace=namespace)
    )["items"]
    return {
        "spec": deployment["spec"],
        "pods": sorted(item["metadata"]["uid"] for item in pods),
        "replica_sets": sorted(item["metadata"]["name"] for item in replica_sets),
        "cluster_ip": _get(namespace, "service", "web")["spec"]["clusterIP"],
        "service_ports": _get(namespace, "service", "web")["spec"]["ports"],
        "config": _get(namespace, "configmap", "web-settings")["data"],
        "secret": _get(namespace, "secret", "web-token")["data"],
        "claim": _get(namespace, "pvc", "web-data")["spec"],
    }


def _fields_match(spec: Path, namespace: str) -> None:
    """Every field the module renders equals the live field (secret data aside)."""
    rendered = CliRunner().invoke(
        cli, ["render", "--spec", str(spec), "--format", "json"]
    )
    assert rendered.exit_code == 0, rendered.output
    for component in json.loads(rendered.stdout)["components"]:
        for resource in component["resources"]:
            desired = resource["manifest"]
            if desired["kind"] == "Secret":
                desired = {k: v for k, v in desired.items() if k != "data"}
            live = _get(namespace, desired["kind"], desired["metadata"]["name"])
            assert manifest_contains(live, desired), desired["metadata"]["name"]


def _release(spec: Path, *args: str) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(release, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}"), result.output


def test_import_live_adopt_and_replan(namespace: str, tmp_path: Path) -> None:
    kubectl(
        "create",
        "configmap",
        "web-settings",
        "--from-literal=greeting=hello",
        namespace=namespace,
    )
    kubectl(
        "create",
        "secret",
        "generic",
        "web-token",
        "--from-literal=token=kept-value",
        namespace=namespace,
    )
    kubectl("create", "-f", "-", namespace=namespace, stdin=OBJECTS)
    kubectl(
        "expose",
        "deployment",
        "web",
        "--port",
        "8080",
        "--target-port",
        "80",
        namespace=namespace,
    )
    kubectl(
        "rollout", "status", "deployment/web", "--timeout=240s", namespace=namespace
    )

    out = tmp_path / "app.py"
    imported = CliRunner().invoke(
        cli,
        [
            "import",
            "live",
            "--kubeconfig",
            KUBECONFIG,
            "--context",
            CONTEXT,
            "--namespace",
            namespace,
            "--name",
            "web",
            "--out",
            str(out),
        ],
    )
    assert imported.exit_code == 0, imported.output
    summary = json.loads(imported.stdout)
    modes = {(item["kind"], item["name"]): item["as"] for item in summary["objects"]}
    assert modes == {
        ("ConfigMap", "web-settings"): "typed",
        ("Secret", "web-token"): "typed",
        ("Deployment", "web"): "typed",
        ("Service", "web"): "typed",
        ("PersistentVolumeClaim", "web-data"): "existing-claim",
    }
    module = out.read_text()
    assert "kept-value" not in module and "a2VwdC12YWx1ZQ" not in module
    (tmp_path / "imported_app.py.txt").write_text(module)  # kept for the report
    digest = summary["images"][0]["running_digest_ref"]
    assert digest and "@sha256:" in digest

    secrets = "\n".join(
        f'[secrets.{item["input"]}]\ntype = "import"\n'
        f'secret = {{ name = "{item["secret"]}", key = "{item["key"]}" }}\n'
        for item in summary["secret_inputs"]
    )
    spec = tmp_path / "release.toml"
    spec.write_text(
        textwrap.dedent(
            f"""
            [target]
            kubeconfig = "{KUBECONFIG}"
            context = "{CONTEXT}"
            namespace = "{namespace}"

            [release]
            name = "web"
            owner = "import-e2e"
            field_manager = "import-e2e"
            composition = "app.py:build"
            state_dir = "state"

            [execution]
            max_seconds = 300
            readiness_seconds = 240
            poll_seconds = 1

            [images]
            web = "{digest}"
            """
        )
        + "\n"
        + secrets
    )
    before = _state(namespace)
    _fields_match(spec, namespace)

    code, planned, output = _release(spec, "plan", "--adopt-all-desired")
    assert code == 0, output
    assert set(planned["summary"]) == {"adopt"}, planned["summary"]
    code, applied, output = _release(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, output
    assert applied["execution"]["state"] == "ready"
    time.sleep(2)
    after = _state(namespace)
    assert after == before  # no rollout, same IP, same data and value
    _fields_match(spec, namespace)

    code, again, output = _release(spec, "plan")
    assert code == 0, output
    operations = {
        f"{action['kind']}/{action['name']}": action["operation"]
        for action in again["actions"]
    }
    print("second plan:", json.dumps(operations, sort_keys=True))
    assert set(operations.values()) <= {"no-op", "apply"}, operations
    code, applied, output = _release(spec, "apply", "--approve", again["plan_hash"])
    assert code == 0, output
    time.sleep(2)
    assert _state(namespace) == before
    _fields_match(spec, namespace)
