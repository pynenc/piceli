"""Server-side dry runs of desired writes, captured as plan evidence.

For every desired object that exists and that this release already manages,
the API server is asked what it would store for exactly the write the executor
sends for an ``apply`` (a merge patch by the release's field manager with the
observed UID and resourceVersion as preconditions), with ``dryRun=All``. The
answers are attached to the discovery artifact (:class:`ServerDryRun`), so
:func:`~piceli.k8s.ops.plan.build_plan` stays a pure function of its inputs and
a stored plan rebuilds to the same hash.

Nothing is persisted on the cluster: every request carries ``dryRun=All``,
which the API server honours for PATCH. A request that fails (RBAC denies
``patch``, a webhook without dry-run support, a conflict) only means that
object has no evidence; the planner then compares the desired manifest with
the live object literally, which can only over-report changes.

The module has no Kubernetes client imports; the provider is injected.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from piceli.k8s.ops.discovery import (
    DiscoveredResource,
    DiscoveryArtifact,
    Ownership,
    ResourceIdentity,
    ServerDryRun,
)
from piceli.k8s.ops.kubernetes_provider import ProviderError
from piceli.k8s.ops.plan import DeploymentComposition, ResourceIntent, ResourceRef

#: At most this many dry-run requests per plan; later objects get no evidence.
MAX_DRY_RUNS = 256

OWNER_ANNOTATION = "piceli.io/owner"
OPERATION_ANNOTATION = "piceli.io/operation"


@dataclass(frozen=True, order=True)
class DryRunUnavailable:
    """An object that has no server evidence, and why (a fixed error code)."""

    resource: ResourceRef
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"resource": self.resource.__dict__, "reason": self.reason}


def update_body(
    intent: ResourceIntent, current: DiscoveredResource, owner_id: str
) -> dict[str, Any]:
    """The merge patch the executor sends for a same-owner ``apply``.

    Mirrors :class:`~piceli.k8s.ops.executor.PlanExecutor`: the owner
    annotation is set, the operation annotation is left to the live object,
    and the observed UID and resourceVersion are the preconditions.
    """
    manifest = intent.manifest
    metadata = manifest["metadata"]
    annotations = metadata.setdefault("annotations", {})
    annotations[OWNER_ANNOTATION] = owner_id
    annotations.pop(OPERATION_ANNOTATION, None)
    observed = current.manifest["metadata"]
    metadata["uid"] = observed["uid"]
    metadata["resourceVersion"] = observed["resourceVersion"]
    return manifest


def _evidence(raw: Mapping[str, Any]) -> str:
    """The stored form: the response without volatile bookkeeping."""
    value = json.loads(json.dumps(raw))
    value.pop("status", None)
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        for key in ("managedFields", "generation", "creationTimestamp", "selfLink"):
            metadata.pop(key, None)
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def probe_candidates(
    composition: DeploymentComposition, artifact: DiscoveryArtifact
) -> list[tuple[ResourceIntent, DiscoveredResource]]:
    """Desired objects that exist, are managed and can be compared.

    Objects with secret bindings (and every Secret) are never probed: their
    private values are never compared, so they always plan as ``apply``.
    """
    live = {resource.identity: resource for resource in artifact.resources}
    candidates = []
    for component in composition.components:
        for intent in component.resources:
            current = live.get(ResourceIdentity(**intent.ref.__dict__))
            if (
                current is None
                or current.ownership is not Ownership.MANAGED
                or not current.content_complete
                or intent.secret_bindings
                or intent.ref.kind == "Secret"
            ):
                continue
            candidates.append((intent, current))
    return sorted(candidates, key=lambda item: item[0].ref)


def capture_server_dry_runs(
    provider: Any,
    artifact: DiscoveryArtifact,
    composition: DeploymentComposition,
    *,
    max_requests: int = MAX_DRY_RUNS,
    deadline: float | None = None,
) -> tuple[DiscoveryArtifact, tuple[DryRunUnavailable, ...]]:
    """Attach server dry runs of the desired writes to ``artifact``.

    Returns the new artifact and the objects left without evidence. A
    provider without ``preview_update`` (a test double, an older adapter)
    yields the artifact unchanged.
    """
    preview = getattr(provider, "preview_update", None)
    if preview is None:
        return artifact, ()
    runs: list[ServerDryRun] = []
    unavailable: list[DryRunUnavailable] = []
    for index, (intent, current) in enumerate(probe_candidates(composition, artifact)):
        if index >= max_requests:
            unavailable.append(DryRunUnavailable(intent.ref, "dry-run-limit-exceeded"))
            continue
        if deadline is not None and time.monotonic() >= deadline:
            unavailable.append(DryRunUnavailable(intent.ref, "deadline-exceeded"))
            continue
        try:
            raw = preview(
                current,
                update_body(intent, current, provider.owner_id),
                deadline=deadline,
            )
            runs.append(
                ServerDryRun(
                    current.identity,
                    intent.digest,
                    current.manifest["metadata"]["resourceVersion"],
                    _evidence(raw),
                )
            )
        except ProviderError as error:
            unavailable.append(DryRunUnavailable(intent.ref, error.category))
        except (ValueError, TypeError, KeyError):
            unavailable.append(
                DryRunUnavailable(intent.ref, "invalid-dry-run-response")
            )
    if not runs:
        return artifact, tuple(unavailable)
    try:
        return replace(artifact, server_dry_runs=tuple(runs)), tuple(unavailable)
    except ValueError:
        # The evidence does not fit the discovery byte budget: plan without it.
        return artifact, tuple(
            sorted(
                unavailable
                + [
                    DryRunUnavailable(
                        ResourceRef(**run.resource.__dict__), "limit-exceeded"
                    )
                    for run in runs
                ]
            )
        )
