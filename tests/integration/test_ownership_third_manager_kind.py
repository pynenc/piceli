"""Opt-in: adoption and pruning with a third field manager present (kind).

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-m2 --kubeconfig /tmp/m2.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/m2.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-m2 \\
      uv run pytest tests/integration/test_ownership_third_manager_kind.py

``kubectl apply`` creates a Deployment and two ConfigMaps. Two more writers
then share them: ``audit-controller`` (a controller, by its name) annotates
the Deployment, and ``config-bot`` (a client tool) labels a ConfigMap and adds
a data key. Piceli adopts all three objects:

* the takeover transfers the client managers (``kubectl-client-side-apply``,
  ``config-bot``) and keeps the controller: its annotation survives, the
  client-written label the release does not declare is removed;
* a later release drops a data key both Piceli and ``config-bot`` declared
  (kept: another manager owns it), a key only Piceli declared (removed) and a
  whole ConfigMap (pruned, although another manager wrote to it).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from kind_support import (
    cli,
    get,
    kubectl,
    managers,
    operations,
    requires_kind,
    write_spec,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(900), requires_kind]

KUBECTL_OBJECTS = """
apiVersion: v1
kind: ConfigMap
metadata: {name: settings}
data: {mode: blue, old: "1"}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: cache}
data: {size: "64"}
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
        resources: {requests: {cpu: 10m, memory: 16Mi}}
"""

COMPOSITION = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent

FULL = {full}


def build(ctx):
    meta = lambda name: {{"name": name, "namespace": ctx.namespace}}
    data = {{"mode": "blue"}}
    resources = []
    if FULL:
        data.update({{"old": "1", "shared": "yes"}})
        resources.append(ResourceIntent.from_manifest({{
            "apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("cache"),
            "data": {{"size": "64"}},
        }}))
    resources.append(ResourceIntent.from_manifest({{
        "apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("settings"),
        "data": data,
    }}))
    labels = {{"app": "web"}}
    resources.append(ResourceIntent.from_manifest({{
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta("web"),
        "spec": {{"replicas": 1, "selector": {{"matchLabels": labels}},
                 "template": {{"metadata": {{"labels": labels}}, "spec": {{
                     "containers": [{{"name": "web", "image": ctx.image("web"),
                                     "resources": {{"requests": {{"cpu": "10m",
                                                                  "memory": "16Mi"}}}}}}]}}}}}},
    }}))
    return DeploymentComposition((DeploymentComponent("app", tuple(resources)),))
"""


def _patch(namespace: str, manager: str, kind: str, name: str, body: dict) -> None:
    # A merge patch (an ``Update`` entry). ``kubectl apply --server-side``
    # would instead migrate kubectl's client-side-apply entry to ``manager``.
    kubectl(
        "patch",
        kind,
        name,
        "--type=merge",
        f"--field-manager={manager}",
        "-p",
        json.dumps(body),
        namespace=namespace,
    )


def test_adoption_and_pruning_with_a_third_manager(
    tmp_path: Path, kind_namespace: str
) -> None:
    namespace = kind_namespace
    kubectl("apply", "-f", "-", namespace=namespace, stdin=KUBECTL_OBJECTS)
    _patch(
        namespace,
        "audit-controller",
        "deployment",
        "web",
        {"metadata": {"annotations": {"audit.example/checked": "true"}}},
    )
    _patch(
        namespace,
        "config-bot",
        "configmap",
        "settings",
        {"metadata": {"labels": {"team": "shop"}}, "data": {"shared": "yes"}},
    )
    _patch(
        namespace,
        "config-bot",
        "configmap",
        "cache",
        {"metadata": {"labels": {"team": "shop"}}},
    )
    for module, full in (("full", True), ("trimmed", False)):
        (tmp_path / f"{module}.py").write_text(COMPOSITION.format(full=full))

    spec = write_spec(tmp_path, namespace, "full.py")
    adopt = [
        "--adopt",
        "Deployment/web",
        "--adopt",
        "ConfigMap/settings",
        "--adopt",
        "ConfigMap/cache",
    ]
    code, planned = cli(spec, "plan", *adopt)
    assert code == 0, planned
    moved = {
        item["name"]: set(item["adoption"]["transferred_managers"])
        for item in planned["actions"]
    }
    assert moved["web"] == {"kubectl-client-side-apply"}
    assert moved["settings"] == {"kubectl-client-side-apply", "config-bot"}
    code, applied = cli(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0 and applied["release_state"] == "ready", applied

    web = get("deployment", "web", namespace)
    assert web["metadata"]["annotations"]["audit.example/checked"] == "true"
    assert "audit-controller" in managers(web)
    assert not [name for name in managers(web) if name.startswith("kubectl")]
    settings = get("configmap", "settings", namespace)
    assert settings["data"] == {"mode": "blue", "old": "1", "shared": "yes"}
    assert "team" not in (settings["metadata"].get("labels") or {})
    assert set(managers(settings)) == {"m2-e2e"}

    # config-bot writes again after the adoption: it now owns ``shared``.
    _patch(namespace, "config-bot", "configmap", "settings", {"data": {"shared": "no"}})
    _patch(
        namespace,
        "config-bot",
        "configmap",
        "cache",
        {"metadata": {"labels": {"team": "shop"}}},
    )

    spec = write_spec(tmp_path, namespace, "trimmed.py", prune=True)
    code, planned = cli(spec, "plan")
    assert code == 0, planned
    assert operations(planned) == {
        "ConfigMap/cache": "delete",
        "ConfigMap/settings": "apply",
        "Deployment/web": "no-op",
    }, planned
    (action,) = [a for a in planned["actions"] if a["name"] == "settings"]
    assert action["removes"] == ["/data/old"]
    code, applied = cli(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0 and applied["release_state"] == "ready", applied

    settings = get("configmap", "settings", namespace)
    assert settings["data"] == {"mode": "blue", "shared": "no"}
    assert kubectl("get", "configmap", "cache", namespace=namespace, check=False) == ""
    web = get("deployment", "web", namespace)
    assert web["metadata"]["annotations"]["audit.example/checked"] == "true"
