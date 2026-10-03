"""A fake composition status (``piceli.gitops-status.v1``) for UI journeys and clips.

Generic names only; the namespace ``piceli-test`` is the fake API's.
"""

from __future__ import annotations

from typing import Any

PRODUCT = "3f9c2d1e8a7b4c5d6e0f1a2b3c4d5e6f7a8b9c0d"
ASSETS = "b7e4a1c9d2f3081726354a5b6c7d8e9f0a1b2c3d"
BRANCH = "c1d2e3f4a5b60718293a4b5c6d7e8f9001122334"
DIGEST = "sha256:" + "a" * 64

STATUS: dict[str, Any] = {
    "schema": "piceli.gitops-status.v1",
    "controller": {
        "state": "running",
        "last_poll": "2026-10-01T09:30:00Z",
        "poll_seconds": 60,
    },
    "sources": {
        "product": {
            "url": "https://git.example/shop/product.git",
            "refs": {"main": PRODUCT, "wp-login": BRANCH, "v1.4.0": PRODUCT},
            "last_poll": "2026-10-01T09:30:00Z",
        },
        "assets": {
            "url": "git@git.example:shop/assets.git",
            "refs": {"main": ASSETS},
            "last_poll": "2026-10-01T09:30:00Z",
        },
    },
    "envs": {
        "main": {
            "namespace": "piceli-test",
            "state": "deployed",
            "health": "healthy",
            "revision": {"product": PRODUCT, "assets": ASSETS},
            "last_sync": "2026-10-01T09:28:00Z",
            "components": {
                "web": {
                    "source": "product",
                    "commit": PRODUCT,
                    "digest": DIGEST,
                    "state": "synced",
                    "health": "healthy",
                    "updated_at": "2026-10-01T09:28:00Z",
                },
                "worker": {
                    "source": "product",
                    "commit": PRODUCT,
                    "digest": "sha256:" + "b" * 64,
                    "state": "unchanged",
                    "health": "healthy",
                    "updated_at": "2026-10-01T08:10:00Z",
                },
                "media": {
                    "source": "assets",
                    "commit": ASSETS,
                    "digest": "sha256:" + "c" * 64,
                    "state": "rolling",
                    "health": "progressing",
                    "updated_at": "2026-10-01T09:29:00Z",
                },
            },
        },
        "wp-login": {
            "state": "pending",
            "health": "unknown",
            "revision": {"product": BRANCH, "assets": ASSETS},
            "components": {
                "web": {
                    "source": "product",
                    "commit": BRANCH,
                    "state": "building",
                    "health": "unknown",
                },
            },
        },
    },
}


PLAN_HASH = "sha256:" + "d" * 64


def controller_status() -> dict[str, Any]:
    """:data:`STATUS` with what a 0.14.6 controller adds: ``rc`` waiting for the
    approval of a plan whose changes it publishes, main's trigger and last
    action, and the registry use the controller measured."""
    import copy

    status = copy.deepcopy(STATUS)
    status["controller"]["environments"] = [
        {"name": "main", "promote": False},
        {"name": "rc", "promote": True},
    ]
    status["controller"]["registry_usage"] = {
        "used_bytes": 553_889_792,
        "used_source": "du",
        "claim": "piceli-registry-storage",
        "measured_at": "2026-10-01T09:25:00Z",
    }
    status["envs"]["main"].update(
        {"trigger": "push product/main", "last_action": "deployed"}
    )
    status["envs"]["rc"] = {
        "namespace": "piceli-test",
        "state": "approval-required",
        "health": "healthy",
        "trigger": "tag product/v1.4.0",
        "plan_hash": PLAN_HASH,
        "revision": {"product": PRODUCT, "assets": ASSETS},
        "pending_plan": {
            "plan_hash": PLAN_HASH,
            "combined_hash": "sha256:" + "9" * 64,
            "release": "shop-0123456789ab",
            "counts": {"update": 1, "no-op": 4},
            "changes": [{"operation": "update", "kind": "Deployment", "name": "web"}],
            "changes_total": 1,
            "create_namespace": False,
            "stop": [],
            "images": {},
        },
        "components": {
            "web": {
                "source": "product",
                "commit": PRODUCT,
                "digest": DIGEST,
                "state": "rolling",
                "health": "healthy",
            }
        },
    }
    status["envs"]["stage"] = {
        "namespace": "piceli-test",
        "state": "deployed",
        "health": "degraded",
        "reason": "pipeline-checks-failed",
        "trigger": "checks-changed",
        "last_action": "verified",
        "revision": {"product": PRODUCT, "assets": ASSETS},
        "verification": {
            "state": "failed",
            "trigger": "checks-changed",
            "checks_hash": "sha256:" + "f" * 64,
            "at": "2026-10-01T09:27:00Z",
            "failed": [{"check": "deliberate-failure", "code": "check-failed"}],
        },
        "components": {
            "web": {
                "source": "product",
                "commit": PRODUCT,
                "digest": DIGEST,
                "state": "unchanged",
                "health": "healthy",
            }
        },
    }
    return status


#: What ``piceli ui forward-serve``'s Cluster page reads in the cluster, where
#: the UI cannot measure the registry claim and shows the controller's ``du``.
CLUSTER: dict[str, Any] = {
    "state": "ready",
    "cluster": "my-cluster",
    "nodes": [
        {
            "name": "node-a",
            "arch": "arm64",
            "roles": ["controller", "registry"],
            "ready": True,
            "mirror": {"kind": "k3s", "state": "ready"},
        }
    ],
    "registry": {
        "state": "ready",
        "host": "piceli-registry.piceli-system.svc:5000",
        "registry": {
            "ready": True,
            "pods": [
                {
                    "name": "piceli-registry-5d9f",
                    "node": "node-a",
                    "phase": "Running",
                    "ready": True,
                }
            ],
        },
        "storage": {
            "claim": "piceli-registry-storage",
            "phase": "Bound",
            "capacity": "50Gi",
            "used_bytes": 553_889_792,
            "used_source": "du",
            "measured_by": "controller",
            "measured_at": "2026-10-01T09:25:00Z",
        },
    },
    "controller": {
        "health": "healthy",
        "last_poll": "2026-10-01T09:30:00Z",
        "poll_failures": 0,
    },
    "ui": {"health": "healthy"},
}
