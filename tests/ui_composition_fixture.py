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
