"""Observed objects for the explicitly disposable browser preview.

The production graph receives these through the same Kubernetes observation
path as any cluster. Only ownerReferences form edges; the Service and ConfigMap
remain independent despite sharing a workload name or selector.
"""

from __future__ import annotations

import copy
from typing import Any

from piceli.testing import FakeAPI, manifest


def _owner(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": value["apiVersion"],
        "kind": value["kind"],
        "name": value["metadata"]["name"],
        "uid": value["metadata"]["uid"],
        "controller": True,
    }


def seed_resources(api: FakeAPI) -> None:
    deployment = manifest("Deployment", "api")
    deployment["spec"]["replicas"] = 2
    deployment["spec"]["template"]["spec"]["containers"] = [
        {
            "name": "api",
            "image": "registry.example/shop/api:v1.4.0",
            "ports": [{"name": "http", "containerPort": 8080}],
        }
    ]
    deployment = api.put(deployment, owned=True)
    replica = manifest("ReplicaSet", "api-release")
    replica["metadata"]["ownerReferences"] = [_owner(deployment)]
    replica["spec"] = copy.deepcopy(deployment["spec"])
    replica["status"] = {"replicas": 2, "readyReplicas": 2, "availableReplicas": 2}
    replica = api.put(replica)
    for suffix in ("1", "2"):
        pod = manifest("Pod", f"api-release-{suffix}")
        pod["metadata"].update(
            {
                "labels": {"app": "api"},
                "ownerReferences": [_owner(replica)],
            }
        )
        pod["spec"] = copy.deepcopy(deployment["spec"]["template"]["spec"])
        pod["status"] = {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [
                {
                    "name": "api",
                    "ready": True,
                    "restartCount": 0,
                    "state": {"running": {}},
                }
            ],
        }
        api.put(pod)
    service = manifest("Service", "api")
    service["spec"] = {
        "type": "ClusterIP",
        "selector": {"app": "api"},
        "ports": [{"name": "http", "port": 8080, "targetPort": 8080}],
    }
    api.put(service)
    api.put(manifest("ConfigMap", "settings"))


def seed_logs(api: FakeAPI) -> None:
    """Recent, timestamped container output for both api pods (fixture text only)."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    lines = {
        "api-release-1": [
            (240, "INFO starting api v1.4.0"),
            (236, "INFO listening on :8080"),
            (120, "INFO GET /healthz 200 1ms"),
            (64, "WARN slow upstream response from cache after 812ms"),
            (31, "ERROR request failed: upstream timeout token=fixture-secret-value"),
            (30, "    at fetchCatalog (catalog.js:41)"),
            (12, "INFO GET /orders 200 18ms"),
        ],
        "api-release-2": [
            (238, "INFO starting api v1.4.0"),
            (233, 'level=info msg="listening" port=8080'),
            (90, 'level=debug msg="cache refresh" keys=128'),
            (45, 'level=warn msg="retrying payment provider" attempt=2'),
            (8, "INFO GET /orders 200 22ms"),
        ],
    }
    for pod, rows in lines.items():
        api.pod_logs[(pod, "api", False)] = "".join(
            f"{(now - timedelta(seconds=age)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')} {text}\n"
            for age, text in rows
        )
    api.pod_logs[("api-release-1", "api", True)] = (
        f"{(now - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')} "
        "ERROR previous instance exited: out of memory\n"
    )
