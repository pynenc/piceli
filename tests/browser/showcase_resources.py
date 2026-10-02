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
