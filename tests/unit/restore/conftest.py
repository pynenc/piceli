"""Shared builders: a stateful app rendered to manifests, and its live twin."""

from __future__ import annotations

import copy
from typing import Any

from piceli import App, ClaimTemplate, ExistingClaim

OLD = "example/db@sha256:" + "1" * 64
NEW = "example/db@sha256:" + "2" * 64
CACHE = "example/cache@sha256:" + "3" * 64


def stateful_app(image: str = NEW, size: str = "1Gi", replicas: int = 2) -> App:
    app = App("shop")
    app.stateful_set(
        "db",
        image=image,
        replicas=replicas,
        volumes={"/var/lib/db": ClaimTemplate("data", size=size)},
    )
    app.deployment(
        "cache", image=CACHE, volumes={"/data": ExistingClaim("cache-state")}
    )
    app.deployment("web", image=CACHE)
    return app


def workloads(app: App) -> list[dict[str, Any]]:
    found = []
    for component in app.render("shop").components:
        for resource in component.resources:
            if resource.ref.kind in {"Deployment", "StatefulSet", "DaemonSet"}:
                found.append(copy.deepcopy(dict(resource.manifest)))
    return found


def claim(name: str, phase: str = "Bound") -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": "shop"},
        "status": {"phase": phase},
    }


def claims() -> list[dict[str, Any]]:
    return [
        claim("data-db-0"),
        claim("data-db-1"),
        claim("cache-state"),
        claim("unrelated"),
    ]
