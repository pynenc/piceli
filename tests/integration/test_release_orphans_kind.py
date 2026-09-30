"""Opt-in: ``piceli release orphans`` reports and prunes leftovers (kind).

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-orphans --kubeconfig /tmp/orphans.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/orphans.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-orphans \\
    PICELI_KIND_NODE=piceli-orphans-control-plane \\
      uv run pytest tests/integration/test_release_orphans_kind.py

The test never reads the ambient kubeconfig. In a uniquely named namespace it
deploys an app with components ``a`` and ``b`` (``b``: a Deployment, a
StatefulSet with a claim template, a ConfigMap and a claim), then a second
release without ``b``, and an earlier environment of the same app (another
owner, other names, the same ownership labels):

* ``orphans`` lists ``b``'s objects and the earlier environment's;
* ``--prune`` needs the approved hash, then removes exactly the approved
  objects; the claims (``b``'s claim and the StatefulSet's own) survive;
* ``--include-claims`` removes the app's claim, waiting while the pruned
  registry's pod (slow to stop) still mounts it, and the StatefulSet's own
  claim (created by its controller, without Piceli's owner annotation) still
  survives.
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
    pytest.mark.timeout(1500),
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT and NODE),
        reason="set PICELI_KIND_KUBECONFIG, PICELI_KIND_CONTEXT and PICELI_KIND_NODE",
    ),
]

COMPOSITION = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent

PREFIX = "{prefix}"
WITH_B = {with_b}
# The registry's pod takes this long to stop, so it still mounts the claim
# when the claim is pruned (pvc-protection keeps the claim until it is gone).
REGISTRY_STOP_SECONDS = 20
LABELS = {{"app.kubernetes.io/part-of": "orphan-shop"}}


def build(ctx):
    def meta(name):
        return {{"name": PREFIX + name, "namespace": ctx.namespace, "labels": LABELS}}

    def container(name, mounts=(), stop_seconds=0):
        spec = {{"name": name, "image": ctx.image("web"),
                "resources": {{"requests": {{"cpu": "0.02", "memory": "16Mi"}}}},
                "volumeMounts": list(mounts)}}
        if stop_seconds:
            spec["lifecycle"] = {{"preStop": {{"exec": {{
                "command": ["sleep", str(stop_seconds)]}}}}}}
        return spec

    def pods(name, containers, volumes=()):
        labels = {{"app": PREFIX + name}}
        return {{"metadata": {{"labels": labels}},
                "spec": {{"containers": containers, "volumes": list(volumes)}}}}

    def selector(name):
        return {{"matchLabels": {{"app": PREFIX + name}}}}

    config = lambda name: ResourceIntent.from_manifest(
        {{"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta(name),
         "data": {{"k": "v"}}}})
    web = ResourceIntent.from_manifest(
        {{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta("web"),
         "spec": {{"replicas": 1, "selector": selector("web"),
                  "template": pods("web", [container("web")])}}}})
    components = [DeploymentComponent("a", (config("a-config"), web))]
    if WITH_B:
        claim = ResourceIntent.from_manifest(
            {{"apiVersion": "v1", "kind": "PersistentVolumeClaim",
             "metadata": meta("b-data"),
             "spec": {{"accessModes": ["ReadWriteOnce"],
                      "resources": {{"requests": {{"storage": "16Mi"}}}}}}}})
        registry = ResourceIntent.from_manifest(
            {{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta("b-registry"),
             "spec": {{"replicas": 1, "selector": selector("b-registry"),
                      "template": pods(
                          "b-registry",
                          [container("registry", [{{"name": "data", "mountPath": "/data"}}],
                                     stop_seconds=REGISTRY_STOP_SECONDS)],
                          [{{"name": "data", "persistentVolumeClaim":
                            {{"claimName": PREFIX + "b-data"}}}}])}}}})
        backup = ResourceIntent.from_manifest(
            {{"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": meta("b-backup"),
             "spec": {{"replicas": 1, "serviceName": PREFIX + "b-backup",
                      "selector": selector("b-backup"),
                      "template": pods(
                          "b-backup",
                          [container("backup", [{{"name": "store", "mountPath": "/store"}}])]),
                      "volumeClaimTemplates": [{{
                          "metadata": {{"name": "store"}},
                          "spec": {{"accessModes": ["ReadWriteOnce"],
                                   "resources": {{"requests": {{"storage": "16Mi"}}}}}}}}]}}}})
        components.append(
            DeploymentComponent("b", (config("b-config"), claim, registry, backup))
        )
    return DeploymentComposition(tuple(components))
"""


@pytest.fixture
def namespace():
    from kubernetes.client import CoreV1Api

    name = "orphans-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    api = CoreV1Api(client)
    api.create_namespace({"metadata": {"name": name}})
    try:
        yield name
    finally:
        api.delete_namespace(name)
        client.close()


def _spec(directory: Path, namespace: str, *, owner: str, prefix: str, with_b: bool):
    stem = f"{owner}-{'ab' if with_b else 'a'}"
    (directory / f"{stem}.py").write_text(
        COMPOSITION.format(prefix=prefix, with_b=with_b)
    )
    spec = directory / f"{owner}.toml"
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
            name = "{owner}"
            owner = "{owner}"
            field_manager = "{owner}"
            composition = "{stem}.py:build"
            state_dir = "state-{owner}"
            [execution]
            readiness_seconds = 300
            [images]
            web = "docker.io/library/nginx@{DIGEST}"
            """
        )
    )
    return spec


def _run(spec: Path, *args: str) -> tuple[int, dict, str]:
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}"), result.stderr


def _live(namespace: str) -> set[str]:
    from kubernetes.client import AppsV1Api, CoreV1Api

    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    try:
        core, apps = CoreV1Api(client), AppsV1Api(client)
        return (
            {
                f"Deployment/{item.metadata.name}"
                for item in apps.list_namespaced_deployment(namespace).items
            }
            | {
                f"StatefulSet/{item.metadata.name}"
                for item in apps.list_namespaced_stateful_set(namespace).items
            }
            | {
                f"ConfigMap/{item.metadata.name}"
                for item in core.list_namespaced_config_map(namespace).items
                if item.metadata.name != "kube-root-ca.crt"
            }
            | {
                f"PersistentVolumeClaim/{item.metadata.name}"
                for item in core.list_namespaced_persistent_volume_claim(
                    namespace
                ).items
            }
        )
    finally:
        client.close()


def _names(report: dict, *, prunable: bool) -> set[str]:
    return {
        f"{item['kind']}/{item['name']}"
        for item in report["orphans"]
        if item["prunable"] is prunable
    }


def test_orphans_list_and_prune_leftovers(tmp_path, namespace):
    current = _spec(tmp_path, namespace, owner="shop", prefix="", with_b=True)
    code, out, err = _run(current, "apply", "--auto-approve")
    assert code == 0, (out, err)
    old = _spec(tmp_path, namespace, owner="shop-old", prefix="old-", with_b=False)
    code, out, err = _run(old, "apply", "--auto-approve")
    assert code == 0, (out, err)
    current = _spec(tmp_path, namespace, owner="shop", prefix="", with_b=False)
    code, out, err = _run(current, "apply", "--auto-approve")
    assert code == 0, (out, err)
    assert "Deployment/b-registry" in _live(namespace)  # no [release] prune

    code, report, err = _run(current, "orphans")
    assert code == 0, (report, err)
    assert report["state"] == "listed"
    assert _names(report, prunable=True) == {
        "ConfigMap/b-config",
        "Deployment/b-registry",
        "StatefulSet/b-backup",
    }
    assert _names(report, prunable=False) == {
        "PersistentVolumeClaim/b-data",
        "ConfigMap/old-a-config",
        "Deployment/old-web",
    }

    code, pending, err = _run(current, "orphans", "--prune")
    assert code == 3, (pending, err)
    code, done, err = _run(
        current, "orphans", "--prune", "--approve", pending["plan_hash"]
    )
    assert code == 0, (done, err)
    assert {(i["kind"], i["outcome"]) for i in done["deleted"]} == {
        ("ConfigMap", "deleted"),
        ("Deployment", "deleted"),
        ("StatefulSet", "deleted"),
    }
    live = _live(namespace)
    assert live == {
        "ConfigMap/a-config",
        "Deployment/web",
        "PersistentVolumeClaim/b-data",
        "PersistentVolumeClaim/store-b-backup-0",
        "ConfigMap/old-a-config",
        "Deployment/old-web",
    }

    flags = ["--include-claims", "--include-other-owners"]
    code, pending, err = _run(current, "orphans", "--prune", *flags)
    assert code == 3, (pending, err)
    assert _names(pending, prunable=True) == {
        "PersistentVolumeClaim/b-data",
        "ConfigMap/old-a-config",
        "Deployment/old-web",
    }
    code, done, err = _run(
        current, "orphans", "--prune", "--approve", pending["plan_hash"], *flags
    )
    assert code == 0, (done, err)
    # The claim stays terminating while the pruned registry's pod still mounts
    # it (pvc-protection): the prune waits and reports it once it is gone.
    assert {(i["kind"], i["name"], i["outcome"]) for i in done["deleted"]} == {
        ("PersistentVolumeClaim", "b-data", "deleted"),
        ("ConfigMap", "old-a-config", "deleted"),
        ("Deployment", "old-web", "deleted"),
    }
    # The StatefulSet's own claim carries no Piceli annotation: never touched.
    assert _live(namespace) == {
        "ConfigMap/a-config",
        "Deployment/web",
        "PersistentVolumeClaim/store-b-backup-0",
    }
    code, report, err = _run(current, "orphans")
    assert report["orphans"] == []
