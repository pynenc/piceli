"""Portable, redacted deployment revisions and resumable execution bundles.

The interchange format intentionally contains neither rendered Secret values nor
the private journal receipts.  It is suitable for a caller-owned durable store;
the ``SecretVersionStore`` remains the sole authority for private values.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from piceli.k8s.ops.executor import ActionGrant, ExecutionAuthorization
from piceli.k8s.ops.plan import DeploymentPlan, ObservedSnapshot


REVISION_SCHEMA_VERSION = 1


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _reference_material(plan: DeploymentPlan) -> list[dict[str, Any]]:
    """Expose opaque private references, never their resolved values."""
    return [
        {
            "resource": action.resource.ref.__dict__,
            "pointer": binding.json_pointer,
            "reference": binding.reference.__dict__,
        }
        for action in plan.actions
        for binding in action.resource.secret_bindings
    ]


def _authorization_material(authorization: ExecutionAuthorization) -> dict[str, Any]:
    return {
        "authorization_id": authorization.authorization_id,
        "target": authorization.target.__dict__,
        "provenance": authorization.provenance.__dict__,
        "plan_hash": authorization.plan_hash,
        "snapshot_hash": authorization.snapshot_hash,
        "field_manager": authorization.field_manager,
        "owner_id": authorization.owner_id,
        "expires_at": authorization.expires_at,
        "max_evidence_age_seconds": authorization.max_evidence_age_seconds,
        "resume_revision_id": authorization.resume_revision_id,
        "actions": [
            {
                "resource": grant.resource.__dict__,
                "operation": grant.operation.value,
                "precondition": grant.precondition.__dict__,
                "artifact_digest": grant.artifact_digest,
                "private_bindings": [
                    {
                        "pointer": binding.json_pointer,
                        "reference": binding.reference.__dict__,
                    }
                    for binding in grant.private_bindings
                ],
            }
            for grant in authorization.actions
        ],
        "cluster_resources": [ref.__dict__ for ref in authorization.cluster_resources],
        "compensation_resources": [
            ref.__dict__ for ref in authorization.compensation_resources
        ],
    }


@dataclass(frozen=True)
class DeploymentRevision:
    """An immutable, redacted authorization of one desired-state revision.

    ``plan``, ``snapshot`` and ``authorization`` are retained in memory for
    execution.  ``to_json`` is the portable public representation; importing it
    requires those exact caller-owned objects so it can never recreate private
    credentials, certificates, or discovery evidence from public JSON.
    """

    plan: DeploymentPlan = field(repr=False, compare=False)
    snapshot: ObservedSnapshot = field(repr=False, compare=False)
    authorization: ExecutionAuthorization = field(repr=False, compare=False)
    revision_id: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            self.authorization.plan_hash != self.plan.plan_hash
            or self.authorization.snapshot_hash != self.snapshot.snapshot_hash
            or self.authorization.target != self.plan.target
            or self.snapshot.target != self.plan.target
            or self.authorization.actions
            != tuple(ActionGrant.for_action(action) for action in self.plan.actions)
        ):
            raise ValueError(
                "revision plan, target, snapshot, or authorization mismatch"
            )
        object.__setattr__(self, "revision_id", _digest(self.material()))

    @classmethod
    def create(
        cls,
        plan: DeploymentPlan,
        snapshot: ObservedSnapshot,
        authorization: ExecutionAuthorization,
    ) -> DeploymentRevision:
        return cls(plan, snapshot, authorization)

    def material(self) -> dict[str, Any]:
        """Canonical public state bound by this revision."""
        return {
            "schema_version": REVISION_SCHEMA_VERSION,
            "desired_state": self.plan.summary(),
            "target_snapshot": {
                "target": self.snapshot.target.__dict__,
                "snapshot_hash": self.snapshot.snapshot_hash,
                "captured_at": self.snapshot.captured_at,
                "provenance": self.snapshot.provenance.__dict__,
                "coverage": self.snapshot.coverage.identity_dict(),
                "resources": [
                    {
                        "resource": item.intent.ref.__dict__,
                        "precondition": item.precondition.__dict__,
                        "ownership": item.ownership.value,
                        "retained": item.retained,
                        "artifact_digest": item.intent.artifact_digest,
                    }
                    for item in self.snapshot.resources
                ],
            },
            "authorization": _authorization_material(self.authorization),
            "private_references": _reference_material(self.plan),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"revision_id": self.revision_id, **self.material()}

    def to_json(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_json(
        cls,
        encoded: str | Mapping[str, Any],
        *,
        plan: DeploymentPlan,
        snapshot: ObservedSnapshot,
        authorization: ExecutionAuthorization,
    ) -> DeploymentRevision:
        value = json.loads(encoded) if isinstance(encoded, str) else dict(encoded)
        candidate = cls.create(plan, snapshot, authorization)
        if value != candidate.to_dict():
            raise ValueError(
                "revision interchange does not match supplied private inputs"
            )
        return candidate


@dataclass(frozen=True)
class ExecutionBundle:
    """Stable execution identity for interruption-safe apply and resume."""

    revision: DeploymentRevision = field(repr=False, compare=False)
    execution_id: str
    action_ids: tuple[str, ...]
    authorization: ExecutionAuthorization = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.execution_id:
            raise ValueError("execution id is required")
        if len(self.action_ids) != len(self.revision.plan.actions) or any(
            not re.fullmatch(r"[0-9a-f]{32}", action_id)
            for action_id in self.action_ids
        ):
            raise ValueError("execution bundle action identities are invalid")
        self._validate_authorization(self.authorization)

    @classmethod
    def create(
        cls, revision: DeploymentRevision, *, execution_id: str | None = None
    ) -> ExecutionBundle:
        return cls(
            revision,
            execution_id or uuid.uuid4().hex,
            tuple(uuid.uuid4().hex for _ in revision.plan.actions),
            revision.authorization,
        )

    def _validate_authorization(self, authorization: ExecutionAuthorization) -> None:
        original = self.revision.authorization
        if authorization == original:
            return
        if authorization.resume_revision_id != self.revision.revision_id:
            raise ValueError("replacement authorization must explicitly adopt revision")
        fields = (
            "target",
            "provenance",
            "plan_hash",
            "snapshot_hash",
            "field_manager",
            "owner_id",
            "actions",
            "cluster_resources",
            "compensation_resources",
        )
        if any(
            getattr(authorization, field) != getattr(original, field)
            for field in fields
        ):
            raise ValueError("replacement authorization changes revision scope")

    def with_authorization(
        self, authorization: ExecutionAuthorization
    ) -> ExecutionBundle:
        """Adopt a renewed grant only when it explicitly names this revision."""
        return ExecutionBundle(
            self.revision, self.execution_id, self.action_ids, authorization
        )

    def journal_binding(self) -> dict[str, Any]:
        """Immutable journal binding; use original revision evidence across renewals."""
        return {
            "schema_version": 2,
            "revision_id": self.revision.revision_id,
            "revision": self.revision.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "action_ids": list(self.action_ids),
            "revision": self.revision.to_dict(),
        }

    def to_json(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_json(
        cls,
        encoded: str | Mapping[str, Any],
        *,
        revision: DeploymentRevision,
        authorization: ExecutionAuthorization | None = None,
    ) -> ExecutionBundle:
        value = json.loads(encoded) if isinstance(encoded, str) else dict(encoded)
        if value.get("revision") != revision.to_dict():
            raise ValueError("bundle revision does not match supplied revision")
        return cls(
            revision,
            str(value.get("execution_id", "")),
            tuple(value.get("action_ids", ())),
            authorization or revision.authorization,
        )
