"""Opt-in: a HorizontalPodAutoscaler owns ``spec.replicas`` (kind).

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-m2 --kubeconfig /tmp/m2.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/m2.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-m2 \\
      uv run pytest tests/integration/test_ownership_hpa_kind.py

A release first runs ``web`` with two replicas and no autoscaler. The next
release adds a HorizontalPodAutoscaler (``minReplicas: 3``) targeting it;
the real controller scales the Deployment through its ``scale`` subresource.
From then on Piceli must not fight it: a re-plan is all ``no-op`` with no
drift, a new image is applied without ``spec.replicas`` (no reset, no
conflict), and a rollback keeps the autoscaler's replica count.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from kind_support import (
    DIGEST_1,
    DIGEST_2,
    cli,
    get,
    managers,
    operations,
    requires_kind,
    wait_for,
    write_spec,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(900), requires_kind]

COMPOSITION = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent

AUTOSCALED = {autoscaled}


def build(ctx):
    meta = lambda name: {{"name": name, "namespace": ctx.namespace}}
    labels = {{"app": "web"}}
    web = ResourceIntent.from_manifest({{
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta("web"),
        "spec": {{"replicas": 2, "selector": {{"matchLabels": labels}},
                 "template": {{"metadata": {{"labels": labels}}, "spec": {{
                     "containers": [{{"name": "web", "image": ctx.image("web"),
                                     "resources": {{"requests": {{"cpu": "10m",
                                                                  "memory": "16Mi"}}}}}}]}}}}}},
    }})
    resources = [web]
    if AUTOSCALED:
        resources.append(ResourceIntent.from_manifest({{
            "apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler",
            "metadata": meta("web"),
            "spec": {{"scaleTargetRef": {{"apiVersion": "apps/v1",
                                         "kind": "Deployment", "name": "web"}},
                     "minReplicas": 3, "maxReplicas": 4,
                     "metrics": [{{"type": "Resource", "resource": {{
                         "name": "cpu",
                         "target": {{"type": "Utilization",
                                    "averageUtilization": 80}}}}}}]}},
        }}))
    return DeploymentComposition((DeploymentComponent("web", tuple(resources)),))
"""


def _replicas(namespace: str) -> int:
    return int(get("deployment", "web", namespace)["spec"]["replicas"])


def test_autoscaler_owns_replicas_and_piceli_does_not_fight_it(
    tmp_path: Path, kind_namespace: str
) -> None:
    namespace = kind_namespace
    for module, autoscaled in (("plain", False), ("scaled", True)):
        (tmp_path / f"{module}.py").write_text(
            COMPOSITION.format(autoscaled=autoscaled)
        )

    spec = write_spec(tmp_path, namespace, "plain.py")
    code, applied = cli(spec, "apply", "--auto-approve")
    assert code == 0, applied
    assert _replicas(namespace) == 2

    # Add the autoscaler: Piceli still owns replicas, so it holds the live
    # value (no change) until the autoscaler takes the field over.
    spec = write_spec(tmp_path, namespace, "scaled.py")
    code, planned = cli(spec, "plan")
    assert code == 0, planned
    assert operations(planned) == {
        "Deployment/web": "no-op",
        "HorizontalPodAutoscaler/web": "create",
    }, planned
    assert planned["autoscaled"] == [
        {
            "resource": {
                "api_version": "apps/v1",
                "kind": "Deployment",
                "namespace": namespace,
                "name": "web",
            },
            "field": "/spec/replicas",
            "autoscalers": ["HorizontalPodAutoscaler/web"],
            "mode": "held",
        }
    ]
    code, applied = cli(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, applied
    assert applied["release_state"] == "ready", applied
    second = applied["release"]

    wait_for(lambda: _replicas(namespace) == 3, message="autoscaler to scale to 3")
    # Plan once the rollout settled: status writes during it change the
    # resourceVersion between discovery and the server dry run.
    wait_for(
        lambda: get("deployment", "web", namespace)["status"].get("readyReplicas") == 3,
        message="three ready replicas",
    )
    live = get("deployment", "web", namespace)
    assert "f:replicas" in json.dumps(
        managers(live)["kube-controller-manager/scale"]["fieldsV1"]
    )

    # No perpetual diff and no drift: the autoscaler's field is not Piceli's.
    code, planned = cli(spec, "plan")
    assert code == 0, planned
    assert set(operations(planned).values()) == {"no-op"}, (
        planned["diffs"],
        planned["dry_run_unavailable"],
        planned["autoscaled"],
    )
    assert planned["drift"] == []
    assert planned["autoscaled"][0]["mode"] == "yielded"

    # A new image: applied without spec.replicas, so nothing is reset.
    spec = write_spec(tmp_path, namespace, "scaled.py", digest=DIGEST_2)
    code, planned = cli(spec, "plan")
    assert code == 0, planned
    (web,) = [a for a in planned["actions"] if a["kind"] == "Deployment"]
    assert web["operation"] == "apply"
    code, applied = cli(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, applied
    assert applied["release_state"] == "ready", applied
    live = get("deployment", "web", namespace)
    assert live["spec"]["replicas"] == 3
    assert live["spec"]["template"]["spec"]["containers"][0]["image"].endswith(DIGEST_2)
    owned = managers(live)
    assert "f:replicas" not in json.dumps(owned["m2-e2e"]["fieldsV1"])
    assert "f:replicas" in json.dumps(owned["kube-controller-manager/scale"])

    # Rolling back to the previous release keeps the autoscaler's count too.
    code, rolled = cli(spec, "rollback", "previous", "--auto-approve")
    assert code == 0, rolled
    assert rolled["selected"] == second
    live = get("deployment", "web", namespace)
    assert live["spec"]["replicas"] == 3
    assert live["spec"]["template"]["spec"]["containers"][0]["image"].endswith(DIGEST_1)
