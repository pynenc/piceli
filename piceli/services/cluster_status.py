"""Read-only browser projection of ``cluster status`` and ``registry status``.

The target is the already registered, explicit UI target. No browser input
selects credentials or a Kubernetes API address. Only public status fields
cross the service boundary; the cluster declaration's credential reference is
not returned.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from piceli.gitops import GitOpsError
from piceli.services.query import QueryError, QueryService

SYSTEM_NAMESPACE = "piceli-system"


def _items(api: Any, path: str) -> list[dict[str, Any]]:
    result = api.call(path, "GET")
    return [item for item in (result or {}).get("items", []) if isinstance(item, dict)]


def _ready(item: Mapping[str, Any]) -> bool | None:
    conditions = (item.get("status") or {}).get("conditions") or []
    return next(
        (
            condition.get("status") == "True"
            for condition in conditions
            if condition.get("type") == "Ready"
        ),
        None,
    )


def _public_status(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist the CLI status shape; never return configuration or Secrets."""
    nodes = []
    for item in raw.get("nodes") or []:
        if not isinstance(item, Mapping):
            continue
        mirror = item.get("mirror") or {}
        nodes.append(
            {
                "name": str(item.get("name") or "unknown")[:253],
                "arch": item.get("arch")
                if item.get("arch") in ("amd64", "arm64")
                else None,
                "roles": [
                    role
                    for role in item.get("roles") or []
                    if isinstance(role, str) and len(role) < 64
                ],
                "ready": item.get("ready")
                if isinstance(item.get("ready"), bool)
                else None,
                "mirror": {
                    "kind": mirror.get("kind")
                    if mirror.get("kind") in ("containerd", "k3s")
                    else None,
                    "state": mirror.get("state")
                    if mirror.get("state")
                    in ("ready", "missing", "pending", "needs-restart")
                    else "missing",
                }
                if isinstance(mirror, Mapping)
                else None,
            }
        )
    registry = raw.get("registry")
    if isinstance(registry, Mapping):
        storage = registry.get("storage") or {}
        server = registry.get("registry") or {}
        registry = {
            "state": registry.get("state")
            if isinstance(registry.get("state"), str)
            else "unknown",
            "host": registry.get("host")
            if isinstance(registry.get("host"), str)
            else None,
            "ready": server.get("ready") is True
            if isinstance(server, Mapping)
            else False,
            "pods": [
                {
                    "name": pod.get("name"),
                    "node": pod.get("node"),
                    "phase": pod.get("phase"),
                    "ready": pod.get("ready") is True,
                }
                for pod in (server.get("pods") or [])
                if isinstance(pod, Mapping)
            ]
            if isinstance(server, Mapping)
            else [],
            "storage": {
                "claim": storage.get("claim"),
                "phase": storage.get("phase"),
                "capacity": storage.get("capacity"),
                "used_bytes": storage.get("used_bytes")
                if isinstance(storage.get("used_bytes"), int)
                else None,
            }
            if isinstance(storage, Mapping)
            else None,
        }
    controller = raw.get("controller") or {}
    ui = raw.get("ui") or {}
    return {
        "schema": "piceli.ui-cluster.v1",
        "state": raw.get("state") if isinstance(raw.get("state"), str) else "unknown",
        "cluster": raw.get("cluster")
        if isinstance(raw.get("cluster"), str)
        else "unknown",
        "nodes": nodes,
        "registry": registry,
        "controller": {
            "health": controller.get("health")
            if isinstance(controller.get("health"), str)
            else "unknown",
            "last_poll": controller.get("last_poll")
            if isinstance(controller.get("last_poll"), str)
            else None,
            "poll_failures": controller.get("poll_failures")
            if isinstance(controller.get("poll_failures"), int)
            else None,
        }
        if isinstance(controller, Mapping)
        else None,
        "ui": {
            "health": ui.get("health")
            if isinstance(ui.get("health"), str)
            else "unknown"
        }
        if isinstance(ui, Mapping)
        else None,
    }


class ClusterStatusControl:
    def __init__(
        self,
        query: QueryService,
        application_id: str,
        reader: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        if application_id not in query.registrations:
            raise ValueError("cluster status needs an installed UI scope")
        self.query = query
        self.application_id = application_id
        self.reader = reader

    def status(self) -> dict[str, Any]:
        registration = self.query.registration(self.application_id, action="inspect")
        try:
            raw = (
                self.reader()
                if self.reader is not None
                else self._live(registration.target)
            )
        except (GitOpsError, OSError, ValueError):
            raise QueryError("ui-observation-unavailable", 503) from None
        if not isinstance(raw, Mapping):
            raise QueryError("ui-observation-unavailable", 503)
        return _public_status(raw)

    @staticmethod
    def _live(target: Any) -> dict[str, Any]:
        from piceli.artifacts import cluster_registry as cr
        from piceli.gitops.install import connect
        from piceli.pipeline.model import Registry

        with connect(
            target.kubeconfig,
            target.context,
            transport=target.transport,
            exec_policy=target.exec_policy,
        ) as api:
            ns = SYSTEM_NAMESPACE
            config = api.call(
                f"/api/v1/namespaces/{ns}/configmaps/piceli-cluster", "GET"
            )
            declaration: dict[str, Any] = {}
            if isinstance(config, Mapping):
                text = (config.get("data") or {}).get("cluster.json")
                if isinstance(text, str):
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        declaration = parsed
            # The installed UI may have a namespaced read grant but no node
            # grant. Keep the page available and show readiness as unknown.
            try:
                nodes = _items(api, "/api/v1/nodes")
            except GitOpsError:
                nodes = []
            declared_nodes = declaration.get("nodes") or []
            by_name = {(item.get("metadata") or {}).get("name"): item for item in nodes}
            names = {
                item.get("name") for item in declared_nodes if isinstance(item, Mapping)
            } | set(by_name)
            registry_decl = declaration.get("registry") or {}
            registry = None
            mirror_rows: dict[str, Mapping[str, Any]] = {}
            if isinstance(registry_decl, Mapping):
                on = registry_decl.get("on") or next(iter(names), "unknown")
                declared_registry = Registry.in_cluster(
                    on=str(on),
                    namespace=str(registry_decl.get("namespace") or ns),
                    name=str(registry_decl.get("name") or "piceli-registry"),
                    storage=str(registry_decl.get("storage") or "20Gi"),
                )
                rns = declared_registry.namespace
                pods = _items(api, f"/api/v1/namespaces/{rns}/pods")
                deployment = api.call(
                    f"/apis/apps/v1/namespaces/{rns}/deployments/{declared_registry.name}",
                    "GET",
                )
                service = api.call(
                    f"/api/v1/namespaces/{rns}/services/{declared_registry.name}", "GET"
                )
                claim = api.call(
                    f"/api/v1/namespaces/{rns}/persistentvolumeclaims/{cr.claim_name(declared_registry)}",
                    "GET",
                )
                used_bytes = None
                running = next(
                    (
                        pod
                        for pod in pods
                        if ((pod.get("metadata") or {}).get("labels") or {}).get(
                            "app.kubernetes.io/component"
                        )
                        == "registry"
                        and (pod.get("spec") or {}).get("nodeName")
                    ),
                    None,
                )
                if running is not None and claim is not None:
                    try:
                        node_name = running["spec"]["nodeName"]
                        summary = api.call(
                            f"/api/v1/nodes/{node_name}/proxy/stats/summary", "GET"
                        )
                        if isinstance(summary, Mapping):
                            used_bytes = cr.used_bytes(
                                summary, rns, cr.claim_name(declared_registry)
                            )
                    except GitOpsError:
                        pass
                registry = cr.summarize(
                    declared_registry,
                    deployment=deployment if isinstance(deployment, Mapping) else None,
                    service=service if isinstance(service, Mapping) else None,
                    claim=claim if isinstance(claim, Mapping) else None,
                    nodes=nodes,
                    pods=pods,
                    used_bytes=used_bytes,
                )
                mirror_rows = {
                    str(item.get("node")): item
                    for item in registry.get("mirrors") or []
                }
            rows = []
            for name in sorted(value for value in names if isinstance(value, str)):
                found = by_name.get(name) or {}
                declared = next(
                    (
                        item
                        for item in declared_nodes
                        if isinstance(item, Mapping) and item.get("name") == name
                    ),
                    {},
                )
                labels = (found.get("metadata") or {}).get("labels") or {}
                roles = [
                    key.removeprefix("piceli.io/role-")
                    for key, value in labels.items()
                    if key.startswith("piceli.io/role-") and value == "true"
                ]
                mirror = mirror_rows.get(name) or {}
                rows.append(
                    {
                        "name": name,
                        "arch": (found.get("status") or {})
                        .get("nodeInfo", {})
                        .get("architecture")
                        or declared.get("arch"),
                        "roles": sorted(roles or declared.get("roles") or []),
                        "ready": _ready(found) if found else None,
                        "mirror": {
                            "kind": mirror.get("runtime")
                            or labels.get("piceli.io/runtime"),
                            "state": "needs-restart"
                            if mirror.get("restart") == "needed"
                            else mirror.get("mirror", "missing"),
                        },
                    }
                )
            status_cm = api.call(
                f"/api/v1/namespaces/{ns}/configmaps/piceli-gitops-status", "GET"
            )
            controller_status: dict[str, Any] = {}
            if isinstance(status_cm, Mapping):
                text = (status_cm.get("data") or {}).get("status.json")
                if isinstance(text, str):
                    parsed = json.loads(text)
                    if isinstance(parsed, Mapping):
                        controller_status = dict(parsed.get("controller") or {})
            controller_deployment = api.call(
                f"/apis/apps/v1/namespaces/{ns}/deployments/piceli-gitops", "GET"
            )
            ui_deployment = api.call(
                f"/apis/apps/v1/namespaces/{ns}/deployments/piceli-ui", "GET"
            )
            return {
                "state": "ready" if config is not None else "not-initialized",
                "cluster": declaration.get("name") or "unknown",
                "nodes": rows,
                "registry": registry,
                "controller": {
                    "health": "healthy"
                    if (controller_deployment or {})
                    .get("status", {})
                    .get("readyReplicas")
                    else "unavailable",
                    "last_poll": controller_status.get("last_poll"),
                    "poll_failures": controller_status.get("poll_failures"),
                },
                "ui": {
                    "health": "healthy"
                    if (ui_deployment or {}).get("status", {}).get("readyReplicas")
                    else "unavailable"
                },
            }
