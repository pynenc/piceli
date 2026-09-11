"""Fail-closed migration of verified legacy journal executions.

Legacy bindings alone never contain enough evidence to safely recreate a
deployment.  This module accepts a canonical redacted archive, reconstructs only
private placeholders, and copies verified receipts into a separate journal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NoReturn

from piceli.k8s.ops.bounds import object_keys, strict_json, timestamp
from piceli.k8s.ops.discovery import (
    DiscoveryArtifact,
    DiscoveryProvenance,
    EvidenceSource,
    PlanTarget,
)
from piceli.k8s.ops.execution_journal import ExecutionJournal
from piceli.k8s.ops.executor import ActionGrant, ExecutionAuthorization
from piceli.k8s.ops.plan import (
    DeploymentPlan,
    ObservedSnapshot,
    PlanAction,
    PlanOperation,
    ResourceIntent,
    ResourcePrecondition,
    ResourceRef,
)
from piceli.k8s.ops.revision import DeploymentRevision, ExecutionBundle
from piceli.k8s.ops.secret_versions import (
    PRIVATE_VALUE,
    SecretBinding,
    SecretVersionRef,
    SecretVersionStore,
    pointer_parts,
    replace_pointer,
)


LEGACY_EXECUTION_ARCHIVE_SCHEMA_VERSION = 1
_REDACTED_VALUE = "<redacted>"


class LegacyEvidenceInsufficient(ValueError):
    """Archive or journal evidence cannot prove a safe exact migration."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"legacy-evidence-insufficient:{code}")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _fail(code: str) -> NoReturn:
    raise LegacyEvidenceInsufficient(code)


def _private_references(plan: DeploymentPlan) -> list[dict[str, Any]]:
    return [
        {
            "resource": action.resource.ref.__dict__,
            "pointer": binding.json_pointer,
            "reference": binding.reference.__dict__,
        }
        for action in plan.actions
        for binding in action.resource.secret_bindings
    ]


def _authorization_archive(authorization: ExecutionAuthorization) -> dict[str, Any]:
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


def _snapshot_archive(snapshot: ObservedSnapshot) -> dict[str, Any]:
    return {
        "target": snapshot.target.__dict__,
        "snapshot_hash": snapshot.snapshot_hash,
        "captured_at": snapshot.captured_at,
        "provenance": snapshot.provenance.__dict__,
        "coverage": snapshot.coverage.identity_dict(),
        "resources": [
            {
                "resource": item.intent.ref.__dict__,
                "precondition": item.precondition.__dict__,
                "ownership": item.ownership.value,
                "retained": item.retained,
                "artifact_digest": item.intent.artifact_digest,
            }
            for item in snapshot.resources
        ],
        "defaulted_fields": [
            {"resource": item.resource.__dict__, "json_pointer": item.json_pointer}
            for item in snapshot.defaulted_fields
        ],
        "incomplete_content": [item.__dict__ for item in snapshot.incomplete_content],
    }


@dataclass(frozen=True)
class LegacyExecutionArchive:
    """Canonical public evidence required to migrate one legacy execution."""

    _encoded: str

    @classmethod
    def from_revision(cls, revision: DeploymentRevision) -> LegacyExecutionArchive:
        discovery = revision.snapshot.discovery
        if discovery is None:
            raise ValueError("legacy archive requires immutable discovery evidence")
        value = {
            "schema_version": LEGACY_EXECUTION_ARCHIVE_SCHEMA_VERSION,
            "revision": revision.to_dict(),
            "plan": revision.plan.summary(),
            "snapshot": _snapshot_archive(revision.snapshot),
            "discovery": discovery.to_dict(),
            "authorization": _authorization_archive(revision.authorization),
            "private_references": _private_references(revision.plan),
        }
        return cls(_canonical(value))

    @classmethod
    def from_json(cls, encoded: str) -> LegacyExecutionArchive:
        if not isinstance(encoded, str):
            raise ValueError("legacy archive must be JSON")
        value = strict_json(encoded)
        if not isinstance(value, dict) or _canonical(value) != encoded:
            raise ValueError("legacy archive must use canonical JSON")
        return cls(encoded)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LegacyExecutionArchive:
        return cls(_canonical(dict(value)))

    def to_dict(self) -> dict[str, Any]:
        value = strict_json(self._encoded)
        assert isinstance(value, dict)
        return value

    def to_json(self) -> str:
        return self._encoded


@dataclass(frozen=True)
class LegacyExecutionImport:
    """Verified migration output; ``report`` deliberately omits private IDs."""

    revision: DeploymentRevision
    bundle: ExecutionBundle
    lineage: dict[str, str]

    def report(self) -> dict[str, Any]:
        return {
            "execution_id": self.bundle.execution_id,
            "revision_id": self.revision.revision_id,
            "action_count": len(self.bundle.action_ids),
            "source_execution": self.lineage["source_execution"],
            "source_binding_sha256": self.lineage["source_binding_sha256"],
            "archive_sha256": self.lineage["archive_sha256"],
        }


def _reference(value: Mapping[str, Any]) -> SecretVersionRef:
    object_keys(value, {"store_id", "version"})
    return SecretVersionRef(**value)


def _references(value: Any) -> list[tuple[ResourceRef, SecretBinding]]:
    if not isinstance(value, list):
        _fail("private-references")
    result: list[tuple[ResourceRef, SecretBinding]] = []
    for item in value:
        if not isinstance(item, Mapping):
            _fail("private-references")
        try:
            object_keys(item, {"resource", "pointer", "reference"})
            resource = item["resource"]
            reference = item["reference"]
            if not isinstance(resource, Mapping) or not isinstance(reference, Mapping):
                _fail("private-references")
            result.append(
                (
                    ResourceRef(**resource),
                    SecretBinding(item["pointer"], _reference(reference)),
                )
            )
        except (TypeError, ValueError):
            _fail("private-references")
    if len(set(result)) != len(result):
        _fail("private-references")
    return result


def _pointer_value(value: dict[str, Any], pointer: str) -> Any:
    current: Any = value
    try:
        for part in pointer_parts(pointer):
            current = current[int(part)] if isinstance(current, list) else current[part]
    except (KeyError, IndexError, TypeError, ValueError):
        _fail("private-reference-missing")
    return current


def _contains_redaction(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_redaction(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_redaction(item) for item in value)
    return value == _REDACTED_VALUE


def _hydrate(
    manifest: Mapping[str, Any],
    resource: ResourceRef,
    references: list[tuple[ResourceRef, SecretBinding]],
) -> dict[str, Any]:
    result = json.loads(_canonical(manifest))
    bindings = [binding for ref, binding in references if ref == resource]
    for binding in bindings:
        if _pointer_value(result, binding.json_pointer) != _REDACTED_VALUE:
            _fail("private-reference-missing")
        replace_pointer(result, binding.json_pointer, PRIVATE_VALUE)
    if _contains_redaction(result):
        _fail("private-reference-missing")
    return result


def _parse_plan(
    value: Any, references: list[tuple[ResourceRef, SecretBinding]]
) -> DeploymentPlan:
    if not isinstance(value, Mapping):
        _fail("plan")
    try:
        object_keys(
            value,
            {
                "schema_version",
                "plan_hash",
                "target",
                "snapshot_hash",
                "actions",
                "levels",
                "protected_resources",
            },
        )
        target_value = value["target"]
        if not isinstance(target_value, Mapping) or not isinstance(
            value["actions"], list
        ):
            _fail("plan")
        actions: list[PlanAction] = []
        for raw in value["actions"]:
            if not isinstance(raw, Mapping):
                _fail("plan")
            object_keys(
                raw,
                {
                    "operation",
                    "resource",
                    "artifact_digest",
                    "dependencies",
                    "precondition",
                    "manifest",
                },
            )
            if not all(
                isinstance(raw[key], Mapping)
                for key in ("resource", "precondition", "manifest")
            ):
                _fail("plan")
            ref = ResourceRef(**raw["resource"])
            manifest = _hydrate(raw["manifest"], ref, references)
            intent = ResourceIntent.from_manifest(manifest)
            for _, binding in [item for item in references if item[0] == ref]:
                intent = intent.with_secret(binding.json_pointer, binding.reference)
            dependencies = tuple(ResourceRef(**item) for item in raw["dependencies"])
            action = PlanAction(
                PlanOperation(raw["operation"]),
                intent,
                dependencies,
                ResourcePrecondition(**raw["precondition"]),
            )
            if action.summary() != dict(raw):
                _fail("plan-manifest")
            actions.append(action)
        levels = tuple(
            tuple(ResourceRef(**item) for item in level) for level in value["levels"]
        )
        plan = DeploymentPlan(
            PlanTarget(**target_value),
            value["snapshot_hash"],
            tuple(actions),
            levels,
            tuple(ResourceRef(**item) for item in value["protected_resources"]),
            value["schema_version"],
        )
        if plan.summary() != dict(value):
            _fail("plan-hash")
        return plan
    except LegacyEvidenceInsufficient:
        raise
    except (KeyError, TypeError, ValueError):
        _fail("plan")


def _parse_discovery(
    value: Any, references: list[tuple[ResourceRef, SecretBinding]]
) -> DiscoveryArtifact:
    if not isinstance(value, Mapping):
        _fail("discovery")
    prepared = json.loads(_canonical(value))
    resources = prepared.get("resources")
    if not isinstance(resources, list):
        _fail("discovery")
    try:
        for item in resources:
            if not isinstance(item, dict) or not isinstance(item.get("identity"), dict):
                _fail("discovery")
            identity = item["identity"]
            manifest = item.get("manifest")
            if not isinstance(manifest, Mapping):
                _fail("discovery")
            ref = ResourceRef(**identity)
            item["manifest"] = _hydrate(manifest, ref, references)
            item["content_complete"] = True
        artifact = DiscoveryArtifact.from_dict(prepared)
        if not artifact.execution_authoritative:
            _fail("snapshot-coverage")
        return artifact
    except LegacyEvidenceInsufficient:
        raise
    except (KeyError, TypeError, ValueError):
        _fail("discovery")


def _parse_authorization(value: Any) -> ExecutionAuthorization:
    if not isinstance(value, Mapping):
        _fail("authorization")
    try:
        object_keys(
            value,
            {
                "authorization_id",
                "target",
                "provenance",
                "plan_hash",
                "snapshot_hash",
                "field_manager",
                "owner_id",
                "expires_at",
                "max_evidence_age_seconds",
                "actions",
                "cluster_resources",
                "compensation_resources",
            },
        )
        if not all(isinstance(value[key], Mapping) for key in ("target", "provenance")):
            _fail("authorization")
        grants = []
        for raw in value["actions"]:
            if not isinstance(raw, Mapping):
                _fail("authorization")
            object_keys(
                raw,
                {
                    "resource",
                    "operation",
                    "precondition",
                    "artifact_digest",
                    "private_bindings",
                },
            )
            if not isinstance(raw["resource"], Mapping) or not isinstance(
                raw["precondition"], Mapping
            ):
                _fail("authorization")
            bindings = tuple(
                SecretBinding(item["pointer"], _reference(item["reference"]))
                for item in raw["private_bindings"]
                if isinstance(item, Mapping)
                and set(item) == {"pointer", "reference"}
                and isinstance(item["reference"], Mapping)
            )
            if len(bindings) != len(raw["private_bindings"]):
                _fail("authorization")
            grants.append(
                ActionGrant(
                    ResourceRef(**raw["resource"]),
                    PlanOperation(raw["operation"]),
                    ResourcePrecondition(**raw["precondition"]),
                    raw["artifact_digest"],
                    bindings,
                )
            )
        authorization = ExecutionAuthorization(
            value["authorization_id"],
            PlanTarget(**value["target"]),
            DiscoveryProvenance(
                EvidenceSource(value["provenance"]["source"]),
                value["provenance"]["endpoint_id"],
                value["provenance"]["cluster_uid"],
                value["provenance"]["namespace_uid"],
            ),
            value["plan_hash"],
            value["snapshot_hash"],
            value["field_manager"],
            value["owner_id"],
            tuple(grants),
            value["expires_at"],
            tuple(ResourceRef(**item) for item in value["cluster_resources"]),
            tuple(ResourceRef(**item) for item in value["compensation_resources"]),
            value["max_evidence_age_seconds"],
        )
        if timestamp(authorization.expires_at, allow_future=True) <= datetime.now(
            timezone.utc
        ) or _authorization_archive(authorization) != dict(value):
            _fail("authorization")
        return authorization
    except LegacyEvidenceInsufficient:
        raise
    except (KeyError, TypeError, ValueError):
        _fail("authorization")


def _legacy_binding(
    plan: DeploymentPlan, authorization: ExecutionAuthorization
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "plan_hash": plan.plan_hash,
        "snapshot_hash": plan.snapshot_hash,
        "target": plan.target.__dict__,
        "provenance": authorization.provenance.__dict__,
        "owner": authorization.owner_id,
        "manager": authorization.field_manager,
        "authorization_id": authorization.authorization_id,
        "actions": [
            {
                "operation": action.operation.value,
                "resource": action.resource.ref.__dict__,
                "precondition": action.precondition.__dict__,
                "digest": action.resource.artifact_digest,
                "private_bindings": [
                    {
                        "pointer": binding.json_pointer,
                        "reference": binding.reference.__dict__,
                    }
                    for binding in action.resource.secret_bindings
                ],
            }
            for action in plan.actions
        ],
        "cluster_resources": [ref.__dict__ for ref in authorization.cluster_resources],
        "compensation_resources": [
            ref.__dict__ for ref in authorization.compensation_resources
        ],
    }


def import_legacy_execution(
    *,
    legacy_journal: ExecutionJournal,
    legacy_execution_id: str,
    archive: LegacyExecutionArchive,
    journal: ExecutionJournal,
    secrets: SecretVersionStore,
) -> LegacyExecutionImport:
    """Verify and import one legacy execution without Kubernetes IO.

    ``journal`` must be a new destination. The source journal is read-only and
    only unambiguous receipts are copied.
    """
    if not isinstance(archive, LegacyExecutionArchive):
        raise TypeError("archive must be LegacyExecutionArchive")
    if Path(legacy_journal.path).resolve() == Path(journal.path).resolve():
        _fail("destination-journal")
    value = archive.to_dict()
    try:
        object_keys(
            value,
            {
                "schema_version",
                "revision",
                "plan",
                "snapshot",
                "discovery",
                "authorization",
                "private_references",
            },
        )
        if value["schema_version"] != LEGACY_EXECUTION_ARCHIVE_SCHEMA_VERSION:
            _fail("schema-version")
        references = _references(value["private_references"])
        plan = _parse_plan(value["plan"], references)
        discovery = _parse_discovery(value["discovery"], references)
        snapshot = ObservedSnapshot.from_discovery(discovery)
        if _snapshot_archive(snapshot) != value["snapshot"]:
            _fail("snapshot")
        authorization = _parse_authorization(value["authorization"])
        revision = DeploymentRevision.create(plan, snapshot, authorization)
        if revision.to_dict() != value["revision"]:
            _fail("revision")
        if _private_references(plan) != value["private_references"]:
            _fail("private-references")
        for _, binding in references:
            if not secrets.contains(plan.target, binding.reference):
                _fail("private-reference-missing")
        source = legacy_journal.export_execution(legacy_execution_id)
        if source["binding"] != _legacy_binding(plan, authorization):
            _fail("legacy-binding")
        action_ids = tuple(action["operation_id"] for action in source["actions"])
        bundle = ExecutionBundle(
            revision, legacy_execution_id, action_ids, authorization
        )
        for action in source["actions"]:
            for key in ("before", "after"):
                if key in action["payload"] and not secrets.contains(
                    plan.target, SecretVersionRef(**action["payload"][key])
                ):
                    _fail("legacy-receipt-reference")
        journal.import_legacy_execution(
            execution=legacy_execution_id,
            binding=bundle.journal_binding(),
            source_execution=legacy_execution_id,
            source_binding=source["binding"],
            archive=value,
            state=source["state"],
            cancelled=source["cancelled"],
            actions=source["actions"],
        )
        lineage = journal.migration_lineage(legacy_execution_id)
        assert lineage is not None
        return LegacyExecutionImport(revision, bundle, lineage)
    except LegacyEvidenceInsufficient:
        raise
    except (KeyError, TypeError, ValueError):
        _fail("inconsistent-evidence")
