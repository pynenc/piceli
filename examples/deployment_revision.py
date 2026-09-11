"""Persist and resume an authorized Piceli deployment without secret values.

``plan``, ``snapshot`` and ``authorization`` come from the normal discovery and
planning flow. Keep the private SecretVersionStore and journal on durable
caller-owned storage; this file intentionally has no Kubernetes side effects.
"""

from __future__ import annotations

import json

from piceli.k8s.ops.executor import ExecutionAuthorization, PlanExecutor
from piceli.k8s.ops.plan import DeploymentPlan, ObservedSnapshot
from piceli.k8s.ops.revision import DeploymentRevision, ExecutionBundle


def create_bundle(
    plan: DeploymentPlan,
    snapshot: ObservedSnapshot,
    authorization: ExecutionAuthorization,
) -> str:
    """Return canonical JSON safe to persist alongside private version stores."""
    revision = DeploymentRevision.create(plan, snapshot, authorization)
    return ExecutionBundle.create(revision).to_json()


def resume_bundle(
    executor: PlanExecutor,
    encoded: str,
    *,
    plan: DeploymentPlan,
    snapshot: ObservedSnapshot,
    authorization: ExecutionAuthorization,
) -> dict[str, object]:
    """Validate public interchange against exact private inputs, then resume."""
    revision = DeploymentRevision.from_json(
        json.loads(encoded),
        plan=plan,
        snapshot=snapshot,
        authorization=authorization,
    )
    bundle = ExecutionBundle.from_json(encoded, revision=revision)
    return executor.run_bundle(bundle, resume=True)
