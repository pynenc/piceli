"""Read-only public projections of the deployment engine's frozen plan."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from piceli.k8s.ops.discovery import public_manifest
from piceli.services.contracts import DesiredResource, PlanStep, ResourceIdentity

_IDENTITY = ("api_version", "kind", "namespace", "name")


def resource_identity(value: Mapping[str, Any], target_id: str) -> ResourceIdentity:
    return ResourceIdentity(
        target_id=target_id, **{key: value[key] for key in _IDENTITY}
    )


def _key(value: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(value[key] for key in _IDENTITY)


def plan_steps(plan: Mapping[str, Any], target_id: str) -> list[PlanStep]:
    """Keep executor ordinals, including trailing prune actions outside levels."""
    levels = {
        _key(resource): level
        for level, resources in enumerate(plan.get("levels", []))
        for resource in resources
    }
    return [
        PlanStep(
            ordinal=ordinal,
            level=levels.get(_key(action["resource"])),
            resource=resource_identity(action["resource"], target_id),
            operation=action["operation"],
            dependencies=[
                resource_identity(item, target_id)
                for item in action.get("dependencies", [])
            ],
        )
        for ordinal, action in enumerate(plan.get("actions", []))
    ]


def _masked_paths(value: Any, path: str = "") -> list[str]:
    if isinstance(value, str) and value in {"<private>", "<redacted>"}:
        return [path]
    if isinstance(value, list):
        return [
            masked
            for index, item in enumerate(value)
            for masked in _masked_paths(item, f"{path}/{index}")
        ]
    if isinstance(value, dict):
        return [
            masked
            for key, item in value.items()
            for masked in _masked_paths(
                item, path + "/" + key.replace("~", "~0").replace("/", "~1")
            )
        ]
    return []


def desired_resources(
    plan: Mapping[str, Any],
    target_id: str,
    *,
    max_resources: int = 1000,
    max_bytes: int = 2_000_000,
) -> tuple[list[DesiredResource], bool]:
    """Snapshot desired objects only, retaining explicit gaps and redaction masks.

    Complete means every desired resource is included. Masked fields remain
    unknown even in a complete inventory and cannot be compared for equality.
    """
    result: list[DesiredResource] = []
    complete = isinstance(plan.get("actions"), list)
    size = 0
    for action in plan.get("actions", []):
        if action.get("operation") == "delete":
            continue
        manifest = action.get("manifest")
        if not isinstance(manifest, dict):
            complete = False
            continue
        public, _ = public_manifest(manifest)
        projected = DesiredResource(
            resource=resource_identity(action["resource"], target_id),
            manifest=public,
            not_compared=sorted(_masked_paths(public)),
        )
        size += len(projected.model_dump_json().encode())
        if len(result) >= max_resources or size > max_bytes:
            complete = False
            break
        result.append(projected)
    return result, complete
