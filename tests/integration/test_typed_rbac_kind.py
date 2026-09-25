"""Opt-in: pod defaults, typed RBAC (cluster rules included) and a release-wide
NetworkPolicy on a real cluster (kind).

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-app --kubeconfig /tmp/app.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/app.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-app \\
    PICELI_KIND_NODE=piceli-app-control-plane \\
      uv run pytest tests/integration/test_typed_rbac_kind.py

The test never reads the ambient kubeconfig. In a uniquely named namespace it
releases an app whose pods run with ``PodDefaults`` (restricted security, an
extra node selector, a grace period), a ``watcher`` ServiceAccount with
namespaced and cluster rules bound to a Deployment, and a NetworkPolicy that
lets only the app's own pods connect. It checks the grants with
``kubectl auth can-i --as=system:serviceaccount:...``, then:

* a release without the cluster rules (``prune = true``) deletes exactly this
  release's ClusterRole and ClusterRoleBinding;
* a rollback to the first release recreates them;
* a rollback back to the trimmed release removes them again.
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
from piceli.k8s.ops.discovery import RELEASE_NAMESPACE_ANNOTATION
from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
NODE = os.environ.get("PICELI_KIND_NODE", "")
KUBECTL = shutil.which("kubectl")
# nginx:1.27-alpine multi-arch index digest (runs `sleep` as a non-root user).
DIGEST = "sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT and NODE and KUBECTL),
        reason="set PICELI_KIND_KUBECONFIG, PICELI_KIND_CONTEXT and "
        "PICELI_KIND_NODE, and install kubectl",
    ),
]

COMPOSITION = """
from piceli import App, PodDefaults, Rule, Security

CLUSTER = {cluster}


def build(ctx):
    shop = App("shop", pod_defaults=PodDefaults(
        security=Security.restricted(user=10001, fs_group=10001),
        node_selector={{"kubernetes.io/os": "linux"}},
        termination_grace_seconds=5,
        automount_token=False,
    ))
    watcher = shop.service_account(
        "watcher",
        rules=[Rule(resources=["pods"], verbs=["get", "list", "watch"])],
        cluster_rules=(
            [Rule(resources=["nodes"], verbs=["get", "list"])] if CLUSTER else []
        ),
    )
    shop.deployment(
        "watcher",
        image=ctx.image("watcher"),
        command=["sleep", "3600"],
        service_account=watcher,
        node="primary",
    )
    shop.deployment("web", image=ctx.image("watcher"), command=["sleep", "3600"])
    shop.network_policy(
        selector=shop.release_selector,
        allow_from_selector=shop.release_selector,
        name="shop-internal",
    )
    return shop
"""


@pytest.fixture
def namespace():
    from kubernetes.client import CoreV1Api, RbacAuthorizationV1Api

    name = "rbac-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    core = CoreV1Api(client)
    core.create_namespace({"metadata": {"name": name}})
    try:
        yield name
    finally:
        rbac = RbacAuthorizationV1Api(client)
        cluster_name = f"{name}:shop:watcher"
        for delete in (
            rbac.delete_cluster_role_binding,
            rbac.delete_cluster_role,
        ):
            try:
                delete(cluster_name)
            except Exception:  # already gone is the expected case
                pass
        core.delete_namespace(name)
        client.close()


def _spec(directory: Path, namespace: str, module: str) -> Path:
    # Composition modules are cached per path, so each version is a file.
    for name, cluster in (("full.py", True), ("trimmed.py", False)):
        (directory / name).write_text(COMPOSITION.format(cluster=cluster))
    spec = directory / "shop.toml"
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
            name = "shop"
            owner = "rbac-shop-e2e"
            field_manager = "rbac-shop-e2e"
            composition = "{module}:build"
            state_dir = "state"
            prune = true
            [execution]
            readiness_seconds = 240
            [images]
            watcher = "docker.io/library/nginx@{DIGEST}"
            """
        )
    )
    return spec


def _run(spec: Path, *args: str) -> tuple[int, dict, str]:
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}"), result.output


def _operations(payload: dict) -> dict[str, str]:
    return {f"{a['kind']}/{a['name']}": a["operation"] for a in payload["actions"]}


def _approve(spec: Path, *command: str) -> dict:
    code, planned, output = _run(spec, *command)
    assert code in (0, 3), output
    execute = ("apply",) if command == ("plan",) else command
    code, done, output = _run(spec, *execute, "--approve", planned["plan_hash"])
    assert code == 0, output
    assert done["execution"]["state"] == "ready", done
    return planned


def _can(namespace: str, verb: str, resource: str) -> bool:
    result = subprocess.run(  # fixed argv, explicit kubeconfig
        [
            str(KUBECTL),
            "--kubeconfig",
            KUBECONFIG,
            "--context",
            CONTEXT,
            "auth",
            "can-i",
            verb,
            resource,
            "--namespace",
            namespace,
            f"--as=system:serviceaccount:{namespace}:watcher",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    answer = result.stdout.strip()
    assert answer in {"yes", "no"}, (answer, result.returncode)
    return answer == "yes"


def _cluster_objects(namespace: str) -> dict[str, dict]:
    from kubernetes.client import RbacAuthorizationV1Api

    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    try:
        rbac = RbacAuthorizationV1Api(client)
        found = {}
        for kind, items in (
            ("ClusterRole", rbac.list_cluster_role().items),
            ("ClusterRoleBinding", rbac.list_cluster_role_binding().items),
        ):
            for item in items:
                if item.metadata.name.startswith(f"{namespace}:"):
                    annotations = item.metadata.annotations or {}
                    found[f"{kind}/{item.metadata.name}"] = {
                        key: annotations[key]
                        for key in ("piceli.io/owner", RELEASE_NAMESPACE_ANNOTATION)
                        if key in annotations
                    }
        return found
    finally:
        client.close()


def _pod_spec(namespace: str, name: str) -> dict:
    from kubernetes.client import AppsV1Api

    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    try:
        deployment = AppsV1Api(client).read_namespaced_deployment(name, namespace)
        return client.sanitize_for_serialization(deployment.spec.template.spec)
    finally:
        client.close()


def test_pod_defaults_rbac_and_release_wide_policy(tmp_path, namespace):
    cluster_name = f"{namespace}:shop:watcher"
    full = _spec(tmp_path, namespace, "full.py")
    code, planned, output = _run(full, "plan")
    assert code == 0, output
    scoped = sorted(
        f"{a['kind']}/{a['name']}"
        for a in planned["actions"]
        if a.get("cluster_scoped")
    )
    assert scoped == [
        f"ClusterRole/{cluster_name}",
        f"ClusterRoleBinding/{cluster_name}",
    ]
    assert "[cluster-scoped]" in output
    code, applied, output = _run(full, "apply", "--approve", planned["plan_hash"])
    assert code == 0, output
    assert applied["execution"]["state"] == "ready", applied

    # Pod defaults reached the live pods; the node pin merged with the selector.
    watcher = _pod_spec(namespace, "watcher")
    assert watcher["securityContext"]["runAsUser"] == 10001
    assert watcher["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert watcher["nodeSelector"] == {
        "kubernetes.io/os": "linux",
        "kubernetes.io/hostname": NODE,
    }
    assert watcher["terminationGracePeriodSeconds"] == 5
    assert watcher["automountServiceAccountToken"] is True
    assert watcher["containers"][0]["securityContext"]["capabilities"] == {
        "drop": ["ALL"]
    }
    assert _pod_spec(namespace, "web")["automountServiceAccountToken"] is False

    # The grants are exactly the declared ones.
    assert _can(namespace, "list", "pods")
    assert _can(namespace, "list", "nodes")
    assert not _can(namespace, "delete", "pods")
    assert not _can(namespace, "list", "secrets")
    assert _cluster_objects(namespace) == {
        f"ClusterRole/{cluster_name}": {
            "piceli.io/owner": "rbac-shop-e2e",
            RELEASE_NAMESPACE_ANNOTATION: namespace,
        },
        f"ClusterRoleBinding/{cluster_name}": {
            "piceli.io/owner": "rbac-shop-e2e",
            RELEASE_NAMESPACE_ANNOTATION: namespace,
        },
    }

    # Unchanged: a no-op, cluster-scoped objects included.
    code, again, output = _run(full, "plan")
    assert code == 0, output
    assert set(_operations(again).values()) == {"no-op"}, again["diffs"]

    # Dropping the cluster rules prunes exactly this release's cluster objects.
    trimmed = _spec(tmp_path, namespace, "trimmed.py")
    planned = _approve(trimmed, "plan")
    assert {k for k, v in _operations(planned).items() if v == "delete"} == {
        f"ClusterRole/{cluster_name}",
        f"ClusterRoleBinding/{cluster_name}",
    }
    assert _cluster_objects(namespace) == {}
    assert _can(namespace, "list", "pods")
    assert not _can(namespace, "list", "nodes")

    # Roll back to the first release: the cluster objects come back.
    pending = _approve(trimmed, "rollback", "previous")
    assert {k for k, v in _operations(pending).items() if v == "create"} == {
        f"ClusterRole/{cluster_name}",
        f"ClusterRoleBinding/{cluster_name}",
    }
    assert _can(namespace, "list", "nodes")

    # And back again: removed, and nothing cluster-scoped is left behind.
    _approve(trimmed, "rollback", "previous")
    assert _cluster_objects(namespace) == {}
    assert not _can(namespace, "list", "nodes")
