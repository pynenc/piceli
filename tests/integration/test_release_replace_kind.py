"""Opt-in: replace, adopt-all-desired and inherited retained metadata on kind.

Runs only when both variables name a disposable cluster and ``kubectl`` is on
``PATH``, for example::

    kind create cluster --name piceli-p4 --kubeconfig /tmp/p4.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/p4.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-p4 \\
      uv run pytest tests/integration/test_release_replace_kind.py

Every ``kubectl`` call passes ``--kubeconfig`` and ``--context`` explicitly.
Each test works in its own namespace and deletes it at the end:

* a kubectl-made Deployment whose selector cannot change in place is
  replaced; its backup file restores it with ``kubectl create -f``;
* ``--adopt-all-desired`` adopts a kubectl Deployment, Service and an
  unowned PVC (metadata-only, with a label difference);
* a retained PVC owned by a retired owner id (``inherited_owners``) whose
  only difference is metadata is applied with a metadata-only write;
* unauthorized adopt/replace are refused before any write.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
# nginx:1.27-alpine multi-arch index digest.
DIGEST = "sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT and shutil.which("kubectl")),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT; needs kubectl",
    ),
]

WEB = """
def web(ctx, *, claim=None, selector=None):
    labels = selector or {"app": "web"}
    pod = {"containers": [{"name": "web", "image": ctx.image("web")}]}
    if claim:
        pod["containers"][0]["volumeMounts"] = [{"name": "data", "mountPath": "/data"}]
        pod["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]
    return ResourceIntent.from_manifest({
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "web", "namespace": ctx.namespace},
        "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                 "template": {"metadata": {"labels": labels}, "spec": pod}},
    })


def claim(ctx, name, **metadata):
    return ResourceIntent.from_manifest({
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": ctx.namespace, **metadata},
        "spec": {"accessModes": ["ReadWriteOnce"],
                 "resources": {"requests": {"storage": "64Mi"}}},
    })


def service(ctx):
    return ResourceIntent.from_manifest({
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": "web", "namespace": ctx.namespace},
        "spec": {"selector": {"app": "web"},
                 "ports": [{"name": "http", "port": 80, "targetPort": 80}]},
    })
"""

COMPOSITIONS = {
    "replace": """
def build(ctx):
    return DeploymentComposition((DeploymentComponent("web", (web(ctx),)),))
""",
    "adopt_all": """
def build(ctx):
    data = claim(ctx, "web-data", labels={"app.kubernetes.io/part-of": "shop"})
    return DeploymentComposition((
        DeploymentComponent("storage", (data,)),
        DeploymentComponent("web", (web(ctx, claim="web-data"), service(ctx)),
                            dependencies=("storage",)),
    ))
""",
    "inherited": """
def build(ctx):
    data = claim(ctx, "state", labels={"app.kubernetes.io/part-of": "shop"},
                 annotations={"example.test/tier": "state"})
    return DeploymentComposition((
        DeploymentComponent("storage", (data,)),
        DeploymentComponent("web", (web(ctx, claim="state"),),
                            dependencies=("storage",)),
    ))
""",
}

HEADER = (
    "from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, "
    "ResourceIntent\n"
)


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


def get(namespace: str, kind: str, name: str) -> dict:
    return json.loads(
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


def versions(namespace: str) -> dict[str, str]:
    """resourceVersion of every object of the kinds these tests touch."""
    items = json.loads(
        kubectl("get", "deploy,svc,pvc,cm", "-o", "json", namespace=namespace)
    )["items"]
    return {
        f"{item['kind']}/{item['metadata']['name']}": item["metadata"][
            "resourceVersion"
        ]
        for item in items
        if item["metadata"]["name"] != "kube-root-ca.crt"
    }


@pytest.fixture
def namespace():
    name = "p4-" + uuid.uuid4().hex[:8]
    kubectl("create", "namespace", name)
    try:
        yield name
    finally:
        kubectl("delete", "namespace", name, "--wait=false")


def _spec(directory: Path, namespace: str, composition: str, extra: str = "") -> Path:
    (directory / "composition.py").write_text(HEADER + WEB + COMPOSITIONS[composition])
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
            owner = "p4-e2e"
            field_manager = "p4-e2e"
            composition = "composition.py:build"
            state_dir = "state"
            {extra}

            [execution]
            max_seconds = 300
            readiness_seconds = 240
            poll_seconds = 1

            [images]
            web = "docker.io/library/nginx@{DIGEST}"
            """
        )
    )
    return path


def _cli(spec: Path, *args: str) -> tuple[int, dict, str]:
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}"), result.stderr


LEGACY = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: web}
spec:
  replicas: 1
  selector: {matchLabels: {app: web, track: legacy}}
  template:
    metadata: {labels: {app: web, track: legacy}}
    spec:
      containers:
      - {name: web, image: nginx:1.26-alpine}
"""


def test_replace_recreates_and_the_backup_restores(namespace, tmp_path):
    kubectl("apply", "-f", "-", namespace=namespace, stdin=LEGACY)
    kubectl("rollout", "status", "deploy/web", "--timeout=240s", namespace=namespace)
    before = get(namespace, "deployment", "web")
    spec = _spec(tmp_path, namespace, "replace")

    # Unauthorized: refused before any write, with the flags that unblock it.
    snapshot = versions(namespace)
    code, refused, _ = _cli(spec, "plan")
    assert code == 2, refused
    assert refused["blocking"][0]["suggest"] == [
        "--adopt Deployment/web",
        "--replace Deployment/web",
    ]
    # Adopting cannot change an immutable selector: the apply fails.
    code, planned, _ = _cli(spec, "plan", "--adopt", "Deployment/web")
    assert code == 0, planned
    code, failed, _ = _cli(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 1, failed
    assert failed["execution"]["failure_category"] == "invalid-request"
    assert versions(namespace) == snapshot

    code, planned, stderr = _cli(spec, "plan", "--replace", "Deployment/web")
    assert code == 0, planned
    [action] = planned["actions"]
    assert action["operation"] == "replace"
    assert "DELETES uid " + before["metadata"]["uid"] in stderr
    code, applied, stderr = _cli(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, (applied, stderr)
    [replaced] = applied["adopted"]
    backup = Path(replaced["backup"])
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700

    after = get(namespace, "deployment", "web")
    assert after["metadata"]["uid"] != before["metadata"]["uid"]
    assert after["spec"]["selector"] == {"matchLabels": {"app": "web"}}
    assert after["metadata"]["annotations"]["piceli.io/owner"] == "p4-e2e"
    managers = {
        entry["manager"]
        for entry in after["metadata"]["managedFields"]
        if entry.get("subresource") != "status"
    }
    assert not [name for name in managers if name.startswith("kubectl")]
    # Background propagation removed the legacy ReplicaSet and pods.
    pods = json.loads(
        kubectl("get", "pods", "-l", "track=legacy", "-o", "json", namespace=namespace)
    )["items"]
    assert all(pod["metadata"].get("deletionTimestamp") for pod in pods)

    # Restore the previous object from the backup.
    kubectl("delete", "deployment", "web", "--wait=true", namespace=namespace)
    kubectl("create", "-f", str(backup), namespace=namespace)
    restored = get(namespace, "deployment", "web")
    assert restored["spec"]["selector"] == before["spec"]["selector"]
    assert (
        restored["spec"]["template"]["spec"]["containers"][0]["image"]
        == "nginx:1.26-alpine"
    )
    kubectl("rollout", "status", "deploy/web", "--timeout=240s", namespace=namespace)


KUBECTL_STACK = """
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
        volumeMounts: [{name: data, mountPath: /data}]
      volumes:
      - name: data
        persistentVolumeClaim: {claimName: web-data}
---
apiVersion: v1
kind: Service
metadata: {name: web}
spec:
  selector: {app: web}
  ports: [{name: http, port: 80, targetPort: 80}]
---
apiVersion: v1
kind: ConfigMap
metadata: {name: unrelated}
data: {keep: "yes"}
"""


def test_adopt_all_desired_takes_over_a_kubectl_stack(namespace, tmp_path):
    kubectl("apply", "-f", "-", namespace=namespace, stdin=KUBECTL_STACK)
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
    pvc_before = get(namespace, "pvc", "web-data")
    unrelated = get(namespace, "configmap", "unrelated")
    spec = _spec(tmp_path, namespace, "adopt_all")

    snapshot = versions(namespace)
    code, refused, _ = _cli(spec, "plan")
    assert code == 2
    assert {(item["kind"], item["name"]) for item in refused["blocking"]} == {
        ("Deployment", "web"),
        ("Service", "web"),
        ("PersistentVolumeClaim", "web-data"),
    }
    pvc_block = next(
        item for item in refused["blocking"] if item["kind"] == "PersistentVolumeClaim"
    )
    assert pvc_block["suggest"] == ["--adopt PersistentVolumeClaim/web-data"]
    # A replace of the retained claim is refused too.
    code, refused, _ = _cli(spec, "plan", "--replace", "PersistentVolumeClaim/web-data")
    assert code == 2
    assert {(item["kind"], item["code"]) for item in refused["blocking"]} == {
        ("Deployment", "resource-requires-adoption"),
        ("Service", "resource-requires-adoption"),
        ("PersistentVolumeClaim", "replace-refused"),
    }
    assert versions(namespace) == snapshot

    code, planned, stderr = _cli(spec, "plan", "--adopt-all-desired")
    assert code == 0, planned
    assert planned["authorized"]["adopt"] == [
        "Deployment/web",
        "PersistentVolumeClaim/web-data",
        "Service/web",
    ]
    modes = {item["kind"]: item["adoption"] for item in planned["actions"]}
    assert modes["PersistentVolumeClaim"]["mode"] == "metadata-only"
    assert modes["PersistentVolumeClaim"]["metadata_changes"] == [
        "labels/app.kubernetes.io/part-of"
    ]
    assert modes["Deployment"]["mode"] == modes["Service"]["mode"] == "takeover"
    code, applied, stderr = _cli(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, (applied, stderr)

    pvc_after = get(namespace, "pvc", "web-data")
    assert pvc_after["metadata"]["uid"] == pvc_before["metadata"]["uid"]
    assert pvc_after["spec"] == pvc_before["spec"]
    assert pvc_after["metadata"]["labels"] == {"app.kubernetes.io/part-of": "shop"}
    assert pvc_after["metadata"]["annotations"]["piceli.io/owner"] == "p4-e2e"
    for kind in ("deployment", "service"):
        managers = {
            entry["manager"]
            for entry in get(namespace, kind, "web")["metadata"]["managedFields"]
            if entry.get("subresource") != "status"
        }
        assert not [name for name in managers if name.startswith("kubectl")], kind
    assert (
        kubectl(
            "exec", "deploy/web", "--", "cat", "/data/hello.txt", namespace=namespace
        ).strip()
        == "kept"
    )
    assert (
        get(namespace, "configmap", "unrelated")["metadata"]["resourceVersion"]
        == unrelated["metadata"]["resourceVersion"]
    )


RETIRED_CLAIM = """
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: state
  annotations: {piceli.io/owner: retired-engine}
spec:
  accessModes: [ReadWriteOnce]
  resources: {requests: {storage: 64Mi}}
"""


def test_inherited_retained_claim_gets_a_metadata_only_write(namespace, tmp_path):
    kubectl("apply", "-f", "-", namespace=namespace, stdin=RETIRED_CLAIM)
    before = get(namespace, "pvc", "state")
    spec = _spec(
        tmp_path, namespace, "inherited", 'inherited_owners = ["retired-engine"]'
    )
    code, planned, stderr = _cli(spec, "plan")
    assert code == 0, planned
    claim = next(i for i in planned["actions"] if i["kind"] == "PersistentVolumeClaim")
    assert claim["operation"] == "apply"
    assert claim["metadata_only"] == [
        "annotations/example.test/tier",
        "labels/app.kubernetes.io/part-of",
    ]
    assert "retained, metadata-only" in stderr
    code, applied, stderr = _cli(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, (applied, stderr)
    after = get(namespace, "pvc", "state")
    assert after["metadata"]["uid"] == before["metadata"]["uid"]
    assert after["spec"]["resources"] == before["spec"]["resources"]
    assert after["spec"]["accessModes"] == before["spec"]["accessModes"]
    assert after["metadata"]["labels"] == {"app.kubernetes.io/part-of": "shop"}
    annotations = after["metadata"]["annotations"]
    assert annotations["example.test/tier"] == "state"
    assert annotations["piceli.io/owner"] == "p4-e2e"
    # The metadata patch owns only metadata fields.
    [entry] = [
        entry
        for entry in after["metadata"]["managedFields"]
        if entry["manager"] == "p4-e2e"
    ]
    assert set(entry["fieldsV1"]) == {"f:metadata"}

    # Re-planning is a no-write reconcile.
    code, planned, _ = _cli(spec, "plan")
    assert code == 0, planned
    claim = next(i for i in planned["actions"] if i["kind"] == "PersistentVolumeClaim")
    assert "metadata_only" not in claim
