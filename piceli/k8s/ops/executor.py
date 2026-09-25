"""Explicitly authorized, recoverable execution of immutable Piceli plans."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from piceli.k8s.ops.bounds import positive, seconds, text, timestamp
from piceli.k8s.ops.discovery import (
    RETAINED_KINDS,
    DiscoveredResource,
    DiscoveryProvenance,
    DiscoveryRequest,
    EvidenceSource,
    Ownership,
    PlanTarget,
    ReadinessStatus,
    ResourceIdentity,
    capture_discovery,
)
from piceli.k8s.ops.execution_journal import ExecutionJournal
from piceli.k8s.ops.kubernetes_provider import (
    OPERATION_ANNOTATION,
    OWNER_ANNOTATION,
    KubernetesProvider,
    ProviderError,
)
from piceli.k8s.ops.plan import (
    REPLACEABLE_MANAGED_KINDS,
    AdoptionMode,
    DeploymentPlan,
    ObservedResource,
    ObservedSnapshot,
    PlanAction,
    PlanOperation,
    ResourceIntent,
    ResourcePrecondition,
    ResourceRef,
    adoption_for,
    dry_run_confirms,
    field_manager_entries,
    has_removed_field,
    manifest_contains,
    metadata_changes,
    metadata_patch,
    overlapping_managers,
    private_comparable,
    removal_patch,
    replace_propagation,
    replace_refusal,
    without_metadata_maps,
)
from piceli.k8s.ops.replace_backup import write_backup
from piceli.k8s.ops.secret_versions import (
    PRIVATE_VALUE,
    SecretBinding,
    SecretVersionRef,
    SecretVersionStore,
    replace_pointer,
)
from piceli.telemetry import NoopTelemetry

if TYPE_CHECKING:
    from piceli.k8s.ops.revision import ExecutionBundle


@dataclass(frozen=True)
class ActionGrant:
    """Exact action permission, including private versions excluded from plan hash."""

    resource: ResourceRef
    operation: PlanOperation
    precondition: ResourcePrecondition
    artifact_digest: str
    private_bindings: tuple[SecretBinding, ...] = field(default=(), repr=False)

    @classmethod
    def for_action(cls, action: PlanAction) -> ActionGrant:
        return cls(
            action.resource.ref,
            action.operation,
            action.precondition,
            action.resource.artifact_digest,
            action.resource.secret_bindings,
        )


@dataclass(frozen=True)
class ExecutionAuthorization:
    """Caller-issued grant. Portable evidence and public hashes are insufficient."""

    authorization_id: str
    target: PlanTarget
    provenance: DiscoveryProvenance
    plan_hash: str
    snapshot_hash: str
    field_manager: str
    owner_id: str
    actions: tuple[ActionGrant, ...]
    expires_at: str
    cluster_resources: tuple[ResourceRef, ...] = ()
    compensation_resources: tuple[ResourceRef, ...] = ()
    max_evidence_age_seconds: float = 300
    resume_revision_id: str | None = None
    # Earlier owner ids whose objects this grant lets the executor treat as
    # its own (retained objects are then reconciled without a write, or
    # re-stamped by an explicit metadata-only adoption). Honoured only for ids
    # the provider also classifies as inherited.
    inherited_owner_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.inherited_owner_ids, str):
            raise ValueError("inherited owner ids must be a tuple of ids")
        for value in self.inherited_owner_ids:
            text(value, "inherited owner")
        object.__setattr__(
            self, "inherited_owner_ids", tuple(sorted(set(self.inherited_owner_ids)))
        )
        if self.owner_id in self.inherited_owner_ids:
            raise ValueError("owner id cannot also be inherited")
        for value in (
            self.authorization_id,
            self.plan_hash,
            self.snapshot_hash,
            self.field_manager,
            self.owner_id,
        ):
            text(value, "authorization identity")
        timestamp(self.expires_at, allow_future=True)
        seconds(self.max_evidence_age_seconds, "evidence age", 86400)
        if self.resume_revision_id is not None:
            text(self.resume_revision_id, "resume revision")
        if (
            not isinstance(self.provenance, DiscoveryProvenance)
            or self.provenance.source is EvidenceSource.SYNTHETIC
        ):
            raise ValueError("synthetic evidence cannot authorize execution")
        if len({item.resource for item in self.actions}) != len(self.actions):
            raise ValueError("duplicate action grant")
        resources = {item.resource for item in self.actions}
        if (
            not set(self.compensation_resources) <= resources
            or not set(self.cluster_resources) <= resources
        ):
            raise ValueError("authorization scope references an absent action")
        if any(ref.namespace for ref in self.cluster_resources):
            raise ValueError("cluster scope grant must name cluster resources")


@dataclass(frozen=True)
class ExecutionLimits:
    max_actions: int = 256
    max_seconds: float = 60
    readiness_seconds: float = 10
    poll_seconds: float = 0.1
    max_polls: int = 100
    concurrency: int = 1
    # How long an interrupted write may still reach the API server (a request
    # already sent is not cancelled by the client's death). Only after it may
    # resume send again a write whose object provably did not change.
    write_settle_seconds: float = 60

    def __post_init__(self) -> None:
        positive(self.max_actions, "actions", 4096)
        seconds(self.write_settle_seconds, "write settle")
        positive(self.max_polls, "polls", 10000)
        seconds(self.max_seconds, "execution")
        seconds(self.readiness_seconds, "readiness", self.max_seconds)
        seconds(self.poll_seconds, "poll", self.readiness_seconds)
        if (
            not isinstance(self.concurrency, int)
            or isinstance(self.concurrency, bool)
            or self.concurrency != 1
        ):
            raise ValueError("this executor supports one ordered action at a time")


def _terminating(current: DiscoveredResource, action: PlanAction) -> bool:
    """The object this DELETE removed, still finishing its deletion.

    An ``Orphan`` delete adds the ``orphan`` finalizer, so the API server
    keeps the object (with a ``deletionTimestamp``) until the garbage
    collector removes the finalizer. The same UID with a deletion timestamp is
    that object on its way out, never a new one.
    """
    metadata = current.manifest.get("metadata", {})
    return bool(metadata.get("deletionTimestamp")) and (
        metadata.get("uid") == action.precondition.uid
    )


def _identity(ref: ResourceRef) -> ResourceIdentity:
    return ResourceIdentity(**ref.__dict__)


def _version(resource: DiscoveredResource) -> ResourcePrecondition:
    metadata = resource.manifest["metadata"]
    return ResourcePrecondition(metadata["uid"], metadata["resourceVersion"])


def _owners(resource: DiscoveredResource) -> list[dict[str, Any]]:
    fields = resource.manifest["metadata"].get("managedFields", [])
    if not isinstance(fields, list) or any(
        not isinstance(item, dict) for item in fields
    ):
        raise ValueError("invalid field ownership evidence")
    return sorted(
        [
            {
                key: item.get(key)
                for key in (
                    "manager",
                    "operation",
                    "apiVersion",
                    "fieldsType",
                    "fieldsV1",
                )
            }
            for item in fields
            if item.get("subresource") != "status"
        ],
        key=lambda item: json.dumps(item, sort_keys=True),
    )


def _receipt_intent(resource: DiscoveredResource) -> dict[str, Any]:
    """Return desired intent without Kubernetes controller rollout counters."""
    manifest = ResourceIntent.from_manifest(resource.manifest).manifest
    annotations = manifest.get("metadata", {}).get("annotations", {})
    annotations.pop("deployment.kubernetes.io/revision", None)
    if not annotations:
        manifest.get("metadata", {}).pop("annotations", None)
    return manifest


def _reference(value: dict[str, str]) -> SecretVersionRef:
    return SecretVersionRef(**value)


def _reject_inline_secrets(intent: ResourceIntent) -> None:
    def check(raw: Any, public: Any) -> None:
        if isinstance(raw, dict) and isinstance(public, dict):
            for key in raw:
                check(raw[key], public[key])
        elif isinstance(raw, list) and isinstance(public, list):
            for left, right in zip(raw, public, strict=False):
                check(left, right)
        elif raw != public and raw != PRIVATE_VALUE:
            raise ValueError(
                "execution requires private version references for sensitive values"
            )

    check(intent.manifest, intent.redacted_manifest())
    pointers = [binding.json_pointer for binding in intent.secret_bindings]
    if len(set(pointers)) != len(pointers):
        raise ValueError("duplicate private binding")
    for pointer in pointers:
        if any(
            other != pointer and other.startswith(pointer + "/") for other in pointers
        ):
            raise ValueError("overlapping private binding")


class PlanExecutor:
    """Serial dependency order, durable receipts and conservative ambiguity handling."""

    def __init__(
        self,
        provider: KubernetesProvider,
        journal: ExecutionJournal,
        secrets: SecretVersionStore,
        *,
        limits: ExecutionLimits | None = None,
        after_response: Callable[[int], None] | None = None,
        telemetry: NoopTelemetry | None = None,
        backups: Path | None = None,
        progress: Callable[[str], None] | None = None,
        progress_seconds: float = 5.0,
    ) -> None:
        self.provider = provider
        # Human progress (``applied 3/7``, ``waiting for Deployment/web``):
        # a short fixed phrase with kinds and names only, at most every
        # ``progress_seconds`` unless the object waited on changes.
        self.progress = progress
        self.progress_seconds = progress_seconds
        self._progress_at = 0.0
        self._progress_key: str | None = None
        # Private directory for replace backups; a plan with a REPLACE action
        # is refused before any write when it is not configured.
        self.backups = backups
        self.journal = journal
        self.secrets = secrets
        self.limits = limits or ExecutionLimits()
        # Fault injection occurs strictly after HTTP response and before receipt.
        self.after_response = after_response
        self.telemetry = telemetry or NoopTelemetry()
        # Owner ids accepted for retained objects; widened per run only by the
        # authorization's inherited-owner grant.
        self._owners: frozenset[str] = frozenset({provider.owner_id})

    def _note(self, message: str, key: str | None = None) -> None:
        """Report progress at most every few seconds, or at once for a new ``key``."""
        if self.progress is None:
            return
        now = time.monotonic()
        fresh = key is not None and key != self._progress_key
        if not fresh and now - self._progress_at < self.progress_seconds:
            return
        self._progress_at = now
        if key is not None:
            self._progress_key = key
        try:
            self.progress(message)
        except Exception:  # progress is advisory; never break an execution
            pass

    def preview(self, plan: DeploymentPlan) -> dict[str, Any]:
        return plan.summary()

    def _manifest(self, intent: ResourceIntent) -> dict[str, Any]:
        _reject_inline_secrets(intent)
        manifest = intent.manifest
        for binding in intent.secret_bindings:
            replace_pointer(
                manifest,
                binding.json_pointer,
                self.secrets.resolve(self.provider.target, binding.reference),
            )
        if PRIVATE_VALUE in json.dumps(manifest) or "<redacted>" in json.dumps(
            manifest
        ):
            raise ValueError("unresolved private or redacted manifest")
        return manifest

    def _validate(
        self,
        plan: DeploymentPlan,
        snapshot: ObservedSnapshot,
        authorization: ExecutionAuthorization,
        deadline: float,
    ) -> None:
        if len(plan.actions) > self.limits.max_actions:
            raise ValueError("plan exceeds action budget")
        if timestamp(authorization.expires_at, allow_future=True) <= datetime.now(UTC):
            raise ValueError("execution authorization expired")
        if authorization.target != plan.target or plan.target != self.provider.target:
            raise ValueError("execution target mismatch")
        if (
            authorization.provenance != self.provider.provenance
            or snapshot.provenance != authorization.provenance
        ):
            raise ValueError("execution provenance mismatch")
        if (
            authorization.plan_hash != plan.plan_hash
            or authorization.snapshot_hash != snapshot.snapshot_hash
        ):
            raise ValueError("execution plan or snapshot binding mismatch")
        if (
            authorization.field_manager != self.provider.field_manager
            or authorization.owner_id != self.provider.owner_id
        ):
            raise ValueError("execution field manager or owner mismatch")
        if (
            tuple(ActionGrant.for_action(action) for action in plan.actions)
            != authorization.actions
        ):
            raise ValueError("execution action/private-version scope mismatch")
        artifact = snapshot.discovery
        if (
            artifact is None
            or not artifact.execution_authoritative
            or not snapshot.coverage.complete
            or snapshot.incomplete_content
        ):
            raise ValueError("execution requires complete, private, verified discovery")
        if ObservedSnapshot.from_discovery(artifact) != snapshot:
            raise ValueError("snapshot does not match discovery evidence")
        if not set(authorization.inherited_owner_ids) <= set(
            self.provider.inherited_owner_ids
        ):
            raise ValueError("inherited owner grant exceeds the provider's owners")
        if (
            datetime.now(UTC) - timestamp(artifact.captured_at)
        ).total_seconds() > authorization.max_evidence_age_seconds:
            raise ValueError("discovery evidence expired")
        plan.validate_for(snapshot)
        if len({action.resource.ref for action in plan.actions}) != len(plan.actions):
            raise ValueError("duplicate plan action")
        observed = {item.intent.ref: item for item in snapshot.resources}
        visited: set[ResourceRef] = set()
        for action in plan.actions:
            ref = action.resource.ref
            api = self.provider.api_for(_identity(ref), deadline=deadline)
            if (
                ResourceIntent.from_manifest(
                    action.resource.manifest, scope=api.scope
                ).ref
                != ref
            ):
                raise ValueError("plan resource identity mismatch")
            if not ref.namespace and ref not in authorization.cluster_resources:
                raise ValueError("cluster resource requires exact scope authorization")
            if not set(action.dependencies) <= visited:
                raise ValueError("plan actions violate dependency order")
            visited.add(ref)
            if not snapshot.coverage.is_complete_for(ref.api_version, ref.kind):
                raise ValueError("action type has incomplete discovery")
            current = observed.get(ref)
            if action.operation is PlanOperation.CREATE:
                if not action.precondition.must_not_exist:
                    raise ValueError("create requires absence precondition")
            elif current is None:
                raise ValueError("existing action requires observed resource")
            elif current.ownership is Ownership.UNMANAGED and action.operation not in {
                PlanOperation.ADOPT,
                PlanOperation.REPLACE,
            }:
                raise ValueError("unmanaged resource requires exact adoption action")
            if action.operation is PlanOperation.REPLACE:
                assert current is not None
                refusal = replace_refusal(current)
                if refusal is not None or ref.kind in RETAINED_KINDS:
                    raise ValueError(f"replace refused for {ref}: {refusal}")
                if self.backups is None:
                    raise ValueError("replace requires a private backup directory")
                if replace_propagation(ref.kind) == "Background" and any(
                    current.precondition.uid in child.owner_uids and child.retained
                    for child in snapshot.resources
                ):
                    raise ValueError("unsafe retained descendants")
            if action.operation is PlanOperation.DELETE:
                if current is None or current.retained or ref.kind in RETAINED_KINDS:
                    raise ValueError("retained resource cannot be deleted")
                if any(
                    current.precondition.uid in child.owner_uids
                    and (child.retained or child.ownership is Ownership.UNMANAGED)
                    for child in snapshot.resources
                ):
                    raise ValueError("unsafe retained or unmanaged descendants")
            else:
                manifest = self._manifest(action.resource)
                if action.removals and (
                    current is None
                    or current.retained
                    or current.ownership is not Ownership.MANAGED
                ):
                    raise ValueError(
                        "field removals require a managed, non-retained object"
                    )
                if action.operation is PlanOperation.ADOPT:
                    assert current is not None
                    self._validate_adoption(action, current, manifest, authorization)
                elif action.metadata_changes:
                    assert current is not None
                    if not current.retained:
                        raise ValueError(
                            "metadata-only apply requires a retained object"
                        )
                    self._validate_metadata_write(
                        action, current, manifest, action.metadata_changes
                    )
        self.provider.verify_target(deadline=deadline)

    def _granted_owners(self, authorization: ExecutionAuthorization) -> frozenset[str]:
        """Owner ids whose objects count as ours: exact owner plus granted heirs."""
        return frozenset({self.provider.owner_id}) | (
            frozenset(authorization.inherited_owner_ids)
            & self.provider.inherited_owner_ids
        )

    def _validate_adoption(
        self,
        action: PlanAction,
        current: ObservedResource,
        manifest: dict[str, Any],
        authorization: ExecutionAuthorization,
    ) -> None:
        """Refuse, before any write, an adoption the evidence does not support."""
        adoption = action.adoption
        if adoption is None:
            # Restored pre-adoption-mode plans keep the old, never-forced path.
            return
        expected = adoption_for(action.resource, current, self.provider.field_manager)
        if (
            adoption.mode,
            adoption.previous_owner,
            set(adoption.transferred_managers) - {self.provider.field_manager},
            adoption.metadata_changes,
        ) != (
            expected.mode,
            expected.previous_owner,
            set(expected.transferred_managers),
            expected.metadata_changes,
        ):
            raise ValueError("adoption details do not match discovery evidence")
        if adoption.mode is AdoptionMode.METADATA_ONLY:
            if not current.retained:
                raise ValueError("metadata-only adoption requires a retained object")
            if current.ownership is not Ownership.UNMANAGED and (
                current.owner not in self._granted_owners(authorization)
            ):
                raise ValueError("retained adoption owner is not granted")
            self._validate_metadata_write(
                action, current, manifest, adoption.metadata_changes
            )
            return
        if current.retained or action.resource.ref.kind in RETAINED_KINDS:
            raise ValueError("takeover adoption requires a non-retained object")

    @staticmethod
    def _validate_metadata_write(
        action: PlanAction,
        current: ObservedResource,
        manifest: dict[str, Any],
        planned: tuple[str, ...],
    ) -> None:
        """A metadata-only write may change exactly the planned labels/annotations."""
        if not _spec_contained(current.intent.manifest, manifest):
            # Value-free: the private content differs from the live object.
            raise ValueError(
                "retained adoption requires the live object to contain the "
                f"desired manifest: {action.resource.ref}"
            )
        if metadata_changes(manifest, current.intent.manifest) != planned:
            raise ValueError("metadata-only changes do not match discovery evidence")

    def _binding(
        self, plan: DeploymentPlan, authorization: ExecutionAuthorization
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
            "cluster_resources": [
                ref.__dict__ for ref in authorization.cluster_resources
            ],
            "compensation_resources": [
                ref.__dict__ for ref in authorization.compensation_resources
            ],
        }

    def _guard(
        self, execution: str, authorization: ExecutionAuthorization, deadline: float
    ) -> None:
        if self.journal.cancelled(execution):
            raise ProviderError("cancelled")
        if time.monotonic() >= deadline:
            raise ProviderError("deadline-exceeded")
        if timestamp(authorization.expires_at, allow_future=True) <= datetime.now(UTC):
            raise ProviderError("authorization-expired")
        self.provider.verify_target(deadline=deadline)

    def _store(self, resource: DiscoveredResource) -> dict[str, str]:
        return self.secrets.put(self.provider.target, resource.manifest).__dict__

    def _load(
        self, value: dict[str, str], identity: ResourceIdentity
    ) -> DiscoveredResource:
        return self.provider._resource(
            self.secrets.resolve(self.provider.target, _reference(value)),
            self.provider.api_for(identity),
        )

    def _receipt(
        self, current: DiscoveredResource, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return payload | {
            "after": self._store(current),
            "uid": _version(current).uid,
            "resource_version": _version(current).resource_version,
        }

    def _check_current(
        self,
        current: DiscoveredResource | None,
        action: PlanAction,
        snapshot: ObservedSnapshot,
    ) -> None:
        if action.precondition.must_not_exist:
            if current is not None:
                raise ProviderError("absence-precondition-failed")
            return
        if (
            current is None
            or current.manifest["metadata"].get("uid") != action.precondition.uid
        ):
            raise ProviderError("uid-version-precondition-failed")
        baseline = (
            next(
                item
                for item in snapshot.discovery.resources
                if item.identity == current.identity
            )
            if snapshot.discovery
            else None
        )
        if baseline is None or _owners(current) != _owners(baseline):
            raise ProviderError("field-owner-precondition-failed")
        if current.manifest["metadata"].get("generation") != baseline.manifest[
            "metadata"
        ].get("generation"):
            raise ProviderError("generation-precondition-failed")
        if current.manifest["metadata"].get(
            "resourceVersion"
        ) != action.precondition.resource_version and _receipt_intent(
            current
        ) != _receipt_intent(baseline):
            raise ProviderError("resource-content-precondition-failed")
        if current.ownership is Ownership.UNMANAGED and action.operation not in {
            PlanOperation.ADOPT,
            PlanOperation.REPLACE,
        }:
            raise ProviderError("ownership-precondition-failed")
        if (
            action.operation in {PlanOperation.DELETE, PlanOperation.REPLACE}
            and current.retained
        ):
            raise ProviderError("retained-resource")
        if action.operation is PlanOperation.REPLACE and (
            (
                current.ownership is not Ownership.UNMANAGED
                and action.resource.ref.kind not in REPLACEABLE_MANAGED_KINDS
            )
            or current.manifest["metadata"].get("ownerReferences")
        ):
            raise ProviderError("replace-precondition-failed")
        if (
            action.operation is PlanOperation.NOOP
            and not _contains(
                ResourceIntent.from_manifest(current.manifest).manifest,
                private_comparable(self._manifest(action.resource)),
            )
            # A no-op planned from the server's dry run: declared values may
            # be stored in canonical form (``cpu: 0.5`` as ``500m``).
            and not dry_run_confirms(snapshot, action.resource, current.manifest)
        ):
            raise ProviderError("no-op-content-mismatch")

    def _retained_identical(
        self,
        current: DiscoveredResource | None,
        action: PlanAction,
    ) -> bool:
        """Retained resources may be observed, never rewritten under a new op id."""
        if current is None or not current.retained:
            return False
        metadata = current.manifest["metadata"]
        if metadata.get("annotations", {}).get(OWNER_ANNOTATION) not in self._owners:
            raise ProviderError("ownership-precondition-failed")
        if not _contains(
            ResourceIntent.from_manifest(current.manifest).manifest,
            self._manifest(action.resource),
        ):
            raise ProviderError("retained-content-precondition-failed")
        return True

    def _foreign_owners(
        self, resource: DiscoveredResource, desired: dict[str, Any]
    ) -> tuple[str, ...]:
        """Other field managers that own a field this plan declares."""
        try:
            entries = field_manager_entries(resource.manifest)
        except ValueError:
            raise ProviderError("invalid-field-ownership-evidence") from None
        return overlapping_managers(
            entries, desired, exclude=(self.provider.field_manager,)
        )

    def _manifest_intent(self, action: PlanAction) -> dict[str, Any]:
        """Resolved desired content without execution metadata."""
        return ResourceIntent.from_manifest(self._manifest(action.resource)).manifest

    def _adopt_retained(
        self,
        execution: str,
        row: dict[str, Any],
        action: PlanAction,
        current: DiscoveredResource,
        payload: dict[str, Any],
        authorization: ExecutionAuthorization,
        deadline: float,
    ) -> None:
        """Metadata-only write of a retained object: never spec or data.

        Used by a metadata-only ADOPT and by an APPLY whose only difference is
        metadata (including objects of a granted inherited owner). The patch
        sets the owner annotation and the planned labels/annotations.
        """
        if not current.retained:
            raise ProviderError("retained-adoption-precondition-failed")
        previous = (
            current.manifest["metadata"].get("annotations", {}).get(OWNER_ANNOTATION)
        )
        if current.ownership is Ownership.MANAGED and previous not in self._owners:
            raise ProviderError("ownership-precondition-failed")
        if action.operation is not PlanOperation.ADOPT and (
            current.ownership is not Ownership.MANAGED
        ):
            raise ProviderError("ownership-precondition-failed")
        manifest = self._manifest(action.resource)
        live = ResourceIntent.from_manifest(current.manifest).manifest
        if not _spec_contained(live, manifest):
            raise ProviderError("retained-content-precondition-failed")
        planned = (
            action.adoption.metadata_changes
            if action.adoption is not None
            else action.metadata_changes
        )
        changes = metadata_changes(manifest, live)
        if changes != planned:
            raise ProviderError("retained-content-precondition-failed")
        if previous == self.provider.owner_id and not changes:
            # Already ours (e.g. adopted by an earlier execution): observe only.
            receipt = self._receipt(current, payload | {"retained_reconciled": True})
            self.journal.record(execution, row["ordinal"], "applied", receipt)
            return
        detail = {
            "mode": AdoptionMode.METADATA_ONLY.value,
            "previous_owner": previous if isinstance(previous, str) else None,
        } | ({"metadata_changes": list(changes)} if changes else {})
        payload["adoption" if action.adoption is not None else "metadata_only"] = detail
        patch = metadata_patch(manifest, changes)
        self.provider.adopt_metadata(
            current,
            operation_id=row["operation_id"],
            dry_run=True,
            deadline=deadline,
            labels=patch.get("labels"),
            annotations=patch.get("annotations"),
        )
        self._guard(execution, authorization, deadline)
        self.journal.record(execution, row["ordinal"], "intent", payload)
        try:
            result = self.provider.adopt_metadata(
                current,
                operation_id=row["operation_id"],
                deadline=deadline,
                labels=patch.get("labels"),
                annotations=patch.get("annotations"),
            )
            if self.after_response is not None:
                self.after_response(row["ordinal"])
            assert result is not None
            self.journal.record(
                execution, row["ordinal"], "applied", self._receipt(result, payload)
            )
        except ProviderError as error:
            if not error.ambiguous:
                self.journal.record(execution, row["ordinal"], "failed", payload)
            raise

    def _replace_manifest(
        self, action: PlanAction, operation_id: str
    ) -> dict[str, Any]:
        manifest = self._manifest(action.resource)
        manifest["metadata"].setdefault("annotations", {}).update(
            {
                OWNER_ANNOTATION: self.provider.owner_id,
                OPERATION_ANNOTATION: operation_id,
            }
        )
        return manifest

    def _replace(
        self,
        execution: str,
        row: dict[str, Any],
        action: PlanAction,
        current: DiscoveredResource,
        payload: dict[str, Any],
        authorization: ExecutionAuthorization,
        deadline: float,
    ) -> None:
        """Delete an unmanaged object (or a managed Job or StatefulSet whose
        immutable fields change) and create it from the release.

        Order: a restorable backup of the live object is written (owner-only
        file) and journaled with the intent; the delete carries the observed
        UID and resourceVersion as preconditions; the create requires absence.
        A failure after the delete is reported as ambiguous so the row stays
        ``intent`` and ``resume`` finishes the create (the backup restores the
        previous object by hand, see docs/release_cli.md).
        """
        ref = action.resource.ref
        metadata = current.manifest["metadata"]
        if (
            self.backups is None
            or current.retained
            or ref.kind in RETAINED_KINDS
            or (
                current.ownership is not Ownership.UNMANAGED
                and ref.kind not in REPLACEABLE_MANAGED_KINDS
            )
            or metadata.get("ownerReferences")
        ):
            raise ProviderError("replace-precondition-failed")
        manifest = self._replace_manifest(action, row["operation_id"])
        propagation = replace_propagation(ref.kind)
        uid, version = metadata["uid"], metadata["resourceVersion"]
        # Admission gate for the delete; persists nothing.
        self.provider.delete(
            current.identity,
            uid=uid,
            resource_version=version,
            propagation=propagation,
            dry_run=True,
            deadline=deadline,
        )
        try:
            path, digest = write_backup(
                self.backups,
                execution=execution,
                ordinal=row["ordinal"],
                manifest=current.manifest,
            )
        except (OSError, ValueError):
            raise ProviderError("replace-backup-failed") from None
        payload["replace"] = {
            "backup": str(path),
            "backup_sha256": digest,
            "deleted_uid": uid,
            "deleted_resource_version": version,
            "propagation": propagation,
            "phase": "deleting",
        }
        self._guard(execution, authorization, deadline)
        self.journal.record(execution, row["ordinal"], "intent", payload)
        try:
            self.provider.delete(
                current.identity,
                uid=uid,
                resource_version=version,
                propagation=propagation,
                deadline=deadline,
            )
        except ProviderError as error:
            if not error.ambiguous:
                # Nothing was deleted: a retry starts from the precondition.
                self.journal.record(execution, row["ordinal"], "failed", payload)
            raise
        payload["replace"]["phase"] = "deleted"
        self.journal.record(execution, row["ordinal"], "intent", payload)
        result = self._create_replacement(
            execution, action, manifest, uid, authorization, deadline
        )
        if self.after_response is not None:
            self.after_response(row["ordinal"])
        self.journal.record(
            execution, row["ordinal"], "applied", self._receipt(result, payload)
        )

    def _create_replacement(
        self,
        execution: str,
        action: PlanAction,
        manifest: dict[str, Any],
        deleted_uid: str,
        authorization: ExecutionAuthorization,
        deadline: float,
    ) -> DiscoveredResource:
        """Wait until the deleted object is gone, then create (after the delete,
        every failure is ambiguous: the row stays ``intent`` for ``resume``)."""
        identity = _identity(action.resource.ref)
        try:
            end = min(deadline, time.monotonic() + self.limits.readiness_seconds)
            for _ in range(self.limits.max_polls):
                self._guard(execution, authorization, deadline)
                current = self.provider.get(identity, deadline=deadline)
                if current is None:
                    break
                if current.manifest["metadata"].get("uid") != deleted_uid:
                    raise ProviderError("replace-recreated-by-another-writer")
                if time.monotonic() >= end:
                    raise ProviderError("replace-delete-timeout")
                time.sleep(
                    min(self.limits.poll_seconds, max(0, end - time.monotonic()))
                )
            else:
                raise ProviderError("replace-delete-timeout")
            result = self.provider.write(
                identity, manifest, create=True, deadline=deadline
            )
            if result is None:
                raise ProviderError("invalid-write-response")
            return result
        except ProviderError as error:
            raise ProviderError(
                error.category, status=error.status, ambiguous=True
            ) from None

    def _resume_replace(
        self,
        execution: str,
        row: dict[str, Any],
        action: PlanAction,
        payload: dict[str, Any],
        authorization: ExecutionAuthorization,
        deadline: float,
    ) -> DiscoveredResource:
        """Finish an interrupted replace from what the cluster shows now."""
        replace = payload["replace"]
        deleted_uid = replace["deleted_uid"]
        identity = _identity(action.resource.ref)
        manifest = self._replace_manifest(action, row["operation_id"])
        current = self.provider.get(identity, deadline=deadline)
        if current is not None:
            metadata = current.manifest["metadata"]
            if metadata.get("uid") != deleted_uid:
                annotations = metadata.get("annotations", {})
                if (
                    current.ownership is Ownership.MANAGED
                    and annotations.get(OPERATION_ANNOTATION) == row["operation_id"]
                    and _contains(
                        ResourceIntent.from_manifest(current.manifest).manifest,
                        self._manifest(action.resource),
                    )
                ):
                    return current
                raise ProviderError(
                    "replace-recreated-by-another-writer", ambiguous=True
                )
            if not metadata.get("deletionTimestamp"):
                if (
                    replace.get("phase") != "deleting"
                    or metadata.get("resourceVersion")
                    != replace["deleted_resource_version"]
                ):
                    raise ProviderError("replace-delete-not-observed", ambiguous=True)
                self._guard(execution, authorization, deadline)
                try:
                    self.provider.delete(
                        current.identity,
                        uid=deleted_uid,
                        resource_version=replace["deleted_resource_version"],
                        propagation=replace["propagation"],
                        deadline=deadline,
                    )
                except ProviderError as error:
                    raise ProviderError(
                        error.category, status=error.status, ambiguous=True
                    ) from None
        return self._create_replacement(
            execution, action, manifest, deleted_uid, authorization, deadline
        )

    def _resume_takeover(
        self,
        row: dict[str, Any],
        action: PlanAction,
        adoption: dict[str, Any],
        deadline: float,
    ) -> tuple[DiscoveredResource, tuple[str, ...]]:
        current = self.provider.get(_identity(action.resource.ref), deadline=deadline)
        if current is None:
            raise ProviderError("ambiguous-write-blocked", ambiguous=True)
        if current.manifest["metadata"]["uid"] != action.precondition.uid:
            raise ProviderError("recreated-object", ambiguous=True)
        annotations = current.manifest["metadata"].get("annotations", {})
        owner = annotations.get(OWNER_ANNOTATION)
        if owner is not None and owner not in self._owners | {
            action.adoption.previous_owner if action.adoption else None
        }:
            # Someone else claimed the object in between.
            raise ProviderError("ownership-precondition-failed", ambiguous=True)
        manifest = self._manifest(action.resource)
        metadata = manifest["metadata"]
        metadata.setdefault("annotations", {}).update(
            {
                OWNER_ANNOTATION: self.provider.owner_id,
                OPERATION_ANNOTATION: row["operation_id"],
            }
        )
        metadata["uid"] = current.manifest["metadata"]["uid"]
        return self.provider.converge_takeover(
            current,
            manifest,
            transferred_managers=tuple(adoption["transferred_managers"]),
            deadline=deadline,
        )

    def _unwritten(
        self, row: dict[str, Any], action: PlanAction, deadline: float
    ) -> bool:
        """Whether an interrupted write (an ``intent`` row) certainly never landed.

        A create whose object is still absent, or a write/delete whose object
        still has the UID and resourceVersion recorded just before the write,
        once ``write_settle_seconds`` have passed since it was sent (a request
        still in flight could land later). Every write is preconditioned on
        exactly that version, so nothing reached the object and sending the
        write again is safe. Replacements and takeovers have their own
        idempotent resume; rows written before ``written_at`` existed never
        qualify.
        """
        base = row["payload"]
        if "replace" in base or _is_takeover(row):
            return False
        try:
            sent = timestamp(base.get("written_at"))
        except ValueError:
            return False
        if (
            datetime.now(UTC) - sent
        ).total_seconds() < self.limits.write_settle_seconds:
            return False
        current = self.provider.get(_identity(action.resource.ref), deadline=deadline)
        if "before" not in base:
            return action.operation is PlanOperation.CREATE and current is None
        if current is None:
            return False
        before = self._load(base["before"], current.identity)
        return _version(before) == _version(current)

    def _reconcile(
        self, row: dict[str, Any], action: PlanAction, deadline: float
    ) -> tuple[DiscoveredResource | None, bool]:
        current = self.provider.get(_identity(action.resource.ref), deadline=deadline)
        if action.operation is PlanOperation.DELETE:
            if current is None or _terminating(current, action):
                # Gone, or the delete was accepted and the API server is
                # finishing it (an ``Orphan`` delete waits for the garbage
                # collector to remove its finalizer).
                return None, False
            raise ProviderError("ambiguous-delete-blocked", ambiguous=True)
        if current is None:
            raise ProviderError("ambiguous-write-blocked", ambiguous=True)
        if _metadata_write(action):
            annotations = current.manifest["metadata"].get("annotations", {})
            if (
                current.manifest["metadata"]["uid"] != action.precondition.uid
                or annotations.get(OWNER_ANNOTATION) != self.provider.owner_id
                or annotations.get(OPERATION_ANNOTATION) != row["operation_id"]
                or current.ownership is not Ownership.MANAGED
            ):
                raise ProviderError("ambiguous-write-blocked", ambiguous=True)
            if not _contains(
                ResourceIntent.from_manifest(current.manifest).manifest,
                self._manifest(action.resource),
            ):
                raise ProviderError("ambiguous-content-blocked", ambiguous=True)
            return current, False
        if self._retained_identical(current, action):
            return current, True
        metadata = current.manifest["metadata"]
        if row["payload"].get("same_owner_update", False):
            if current.ownership is not Ownership.MANAGED:
                raise ProviderError("ambiguous-write-blocked", ambiguous=True)
            if (
                not action.precondition.must_not_exist
                and metadata["uid"] != action.precondition.uid
            ):
                raise ProviderError("recreated-object", ambiguous=True)
            live = ResourceIntent.from_manifest(current.manifest).manifest
            if not _contains(live, self._manifest(action.resource)) or (
                has_removed_field(live, action.removals)
            ):
                raise ProviderError("ambiguous-content-blocked", ambiguous=True)
            return current, False
        if (
            metadata.get("annotations", {}).get(OPERATION_ANNOTATION)
            != row["operation_id"]
            or current.ownership is not Ownership.MANAGED
        ):
            raise ProviderError("ambiguous-write-blocked", ambiguous=True)
        if (
            not action.precondition.must_not_exist
            and metadata["uid"] != action.precondition.uid
        ):
            raise ProviderError("recreated-object", ambiguous=True)
        expected = self._manifest(action.resource)
        if not _contains(
            ResourceIntent.from_manifest(current.manifest).manifest, expected
        ):
            raise ProviderError("ambiguous-content-blocked", ambiguous=True)
        return current, False

    def _verify_receipt(
        self,
        row: dict[str, Any],
        action: PlanAction,
        current: DiscoveredResource | None,
    ) -> None:
        payload = row["payload"]
        if action.operation is PlanOperation.DELETE:
            if current is not None and not _terminating(current, action):
                raise ProviderError("deleted-resource-reappeared")
            return
        if current is None or current.manifest["metadata"]["uid"] != payload["uid"]:
            raise ProviderError("recreated-object")
        after = self._load(payload["after"], current.identity)
        # Only what this plan declares is compared: the server may populate
        # other fields after the write (a WaitForFirstConsumer claim gains
        # spec.volumeName and provisioner annotations when it binds, a
        # Deployment a revision annotation). Values are compared as the server
        # returned them, so quantity canonicalization is not drift either.
        desired = self._manifest_intent(action)
        if _scaled(current):
            # An autoscaler took ``spec.replicas`` through the scale
            # subresource since the write: its value, not drift.
            spec = desired.get("spec")
            if isinstance(spec, dict):
                spec.pop("replicas", None)
        if _project(_receipt_intent(current), desired) != _project(
            _receipt_intent(after), desired
        ) or set(self._foreign_owners(current, desired)) - set(
            self._foreign_owners(after, desired)
        ):
            raise ProviderError("applied-resource-drift")
        if current.ownership != after.ownership:
            raise ProviderError("ownership-precondition-failed")
        if (
            action.operation is not PlanOperation.NOOP
            and not payload.get("retained_reconciled", False)
            and not payload.get("same_owner_update", False)
            and current.manifest["metadata"]
            .get("annotations", {})
            .get(OPERATION_ANNOTATION)
            != row["operation_id"]
        ):
            raise ProviderError("operation-identity-mismatch")

    @staticmethod
    def _workload_claims(action: PlanAction) -> set[tuple[str, str]]:
        if action.resource.ref.kind not in {
            "Deployment",
            "StatefulSet",
            "DaemonSet",
            "Job",
            "Pod",
        }:
            return set()
        spec = action.resource.manifest.get("spec", {})
        pod_spec = spec.get("template", {}).get("spec", spec)
        volumes = pod_spec.get("volumes", []) if isinstance(pod_spec, dict) else []
        return {
            (
                action.resource.ref.namespace,
                volume["persistentVolumeClaim"]["claimName"],
            )
            for volume in volumes
            if isinstance(volume, dict)
            and isinstance(volume.get("persistentVolumeClaim"), dict)
            and isinstance(volume["persistentVolumeClaim"].get("claimName"), str)
        }

    def _first_consumer_refs(self, plan: DeploymentPlan) -> set[ResourceRef]:
        claims = {
            (
                action.resource.ref.namespace,
                action.resource.ref.name,
            ): action.resource.ref
            for action in plan.actions
            if action.resource.ref.kind == "PersistentVolumeClaim"
        }
        deferred: set[ResourceRef] = set()
        for action in plan.actions:
            for claim in self._workload_claims(action):
                if claim in claims:
                    deferred.update((claims[claim], action.resource.ref))
        # Submit the whole declared workload wave before starting the strict
        # readiness loop. This lets WFFC claims see their consumers promptly,
        # avoids serial API polling during a rollout, and still verifies every
        # workload only after the claims have bound.
        deferred.update(
            action.resource.ref
            for action in plan.actions
            if self._workload_claims(action)
            or action.resource.ref.kind
            in {"Deployment", "StatefulSet", "DaemonSet", "Job", "Pod"}
        )
        return deferred

    def _ready(
        self,
        execution: str,
        row: dict[str, Any],
        action: PlanAction,
        authorization: ExecutionAuthorization,
        deadline: float,
    ) -> None:
        end = min(deadline, time.monotonic() + self.limits.readiness_seconds)
        started = time.monotonic()
        ref = action.resource.ref
        for poll in range(self.limits.max_polls):
            self._guard(execution, authorization, end)
            if poll:  # not ready at the first look: say what we wait for
                waited = int(time.monotonic() - started)
                self._note(
                    f"waiting for {ref.kind}/{ref.name} to be ready ({waited}s)",
                    key=f"{ref.kind}/{ref.name}",
                )
            current = self.provider.get(_identity(action.resource.ref), deadline=end)
            self._verify_receipt(row, action, current)
            if (action.operation is PlanOperation.DELETE and current is None) or (
                action.operation is not PlanOperation.DELETE
                and current is not None
                and self.provider.readiness(current).status is ReadinessStatus.READY
            ):
                payload = (
                    row["payload"]
                    if current is None
                    else self._receipt(current, row["payload"])
                )
                self.journal.record(execution, row["ordinal"], "ready", payload)
                return
            if (
                current is not None
                and action.operation is not PlanOperation.DELETE
                and self.provider.readiness(current).status
                is ReadinessStatus.UNSUPPORTED
            ):
                raise ProviderError("readiness-unsupported")
            time.sleep(min(self.limits.poll_seconds, max(0, end - time.monotonic())))
        raise ProviderError("readiness-timeout")

    def cancel(self, execution: str) -> dict[str, Any]:
        """Persist cancellation for the running loop and future resumptions."""
        with self.telemetry.operation("cancel", execution) as operation:
            self.journal.cancel(execution)
            result = self.journal.summary(execution)
            operation.state = result["state"]
            return result

    def run(
        self,
        execution: str,
        plan: DeploymentPlan,
        snapshot: ObservedSnapshot,
        authorization: ExecutionAuthorization,
        *,
        resume: bool = False,
    ) -> dict[str, Any]:
        with self.telemetry.operation(
            "resume" if resume else "apply",
            execution,
            plan_hash=plan.plan_hash,
            action_count=len(plan.actions),
        ) as operation:
            result = self._run(execution, plan, snapshot, authorization, resume=resume)
            operation.state = result["state"]
            return result

    def run_bundle(
        self, bundle: ExecutionBundle, *, resume: bool = False
    ) -> dict[str, Any]:
        """Run a durable bundle without minting new action or private identities."""
        with self.telemetry.operation(
            "resume" if resume else "apply",
            bundle.execution_id,
            plan_hash=bundle.revision.plan.plan_hash,
            action_count=len(bundle.revision.plan.actions),
        ) as operation:
            result = self._run(
                bundle.execution_id,
                bundle.revision.plan,
                bundle.revision.snapshot,
                bundle.authorization,
                resume=resume,
                operation_ids=list(bundle.action_ids),
                binding=bundle.journal_binding(),
            )
            operation.state = result["state"]
            return result

    def _run(
        self,
        execution: str,
        plan: DeploymentPlan,
        snapshot: ObservedSnapshot,
        authorization: ExecutionAuthorization,
        *,
        resume: bool = False,
        operation_ids: list[str] | None = None,
        binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        text(execution, "execution id")
        deadline = time.monotonic() + self.limits.max_seconds
        with self.journal.exclusive():
            self._validate(plan, snapshot, authorization, deadline)
            self._owners = self._granted_owners(authorization)
            self.journal.start(
                execution,
                binding or self._binding(plan, authorization),
                operation_ids or [uuid.uuid4().hex for _ in plan.actions],
            )
            if resume:
                self.journal.resume(execution)
            try:
                deferred = self._first_consumer_refs(plan)
                deferred_rows: list[tuple[dict[str, Any], PlanAction]] = []
                total = len(plan.actions)
                for index, (row, action) in enumerate(
                    zip(self.journal.actions(execution), plan.actions, strict=False),
                    start=1,
                ):
                    self._guard(execution, authorization, deadline)
                    self._note(
                        f"applying {index}/{total}: "
                        f"{action.resource.ref.kind}/{action.resource.ref.name}"
                    )
                    if row["state"] in {"compensated", "compensating"}:
                        raise ProviderError("compensation-already-started")
                    if row["state"] == "intent" and self._unwritten(
                        row, action, deadline
                    ):
                        # Interrupted before the write reached the object:
                        # send it again, exactly as planned.
                        row = dict(row, state="pending")
                    if row["state"] in {"pending", "failed"}:
                        current = self.provider.get(
                            _identity(action.resource.ref), deadline=deadline
                        )
                        self._check_current(current, action, snapshot)
                        payload: dict[str, Any] = (
                            {"before": self._store(current)}
                            if current is not None
                            else {}
                        )
                        if action.operation is PlanOperation.NOOP:
                            assert current is not None
                            payload = self._receipt(current, payload)
                            self.journal.record(
                                execution, row["ordinal"], "applied", payload
                            )
                        elif _metadata_write(action):
                            assert current is not None
                            self._adopt_retained(
                                execution,
                                row,
                                action,
                                current,
                                payload,
                                authorization,
                                deadline,
                            )
                        elif action.operation is PlanOperation.REPLACE:
                            assert current is not None
                            self._replace(
                                execution,
                                row,
                                action,
                                current,
                                payload,
                                authorization,
                                deadline,
                            )
                        elif self._retained_identical(current, action):
                            assert current is not None
                            payload = self._receipt(
                                current, payload | {"retained_reconciled": True}
                            )
                            self.journal.record(
                                execution, row["ordinal"], "applied", payload
                            )
                        else:
                            manifest = (
                                self._manifest(action.resource)
                                if action.operation is not PlanOperation.DELETE
                                else None
                            )
                            if manifest is not None:
                                metadata = manifest["metadata"]
                                annotations = metadata.setdefault("annotations", {})
                                annotations[OWNER_ANNOTATION] = self.provider.owner_id
                                same_owner_update = (
                                    current is not None
                                    and current.ownership is Ownership.MANAGED
                                    and action.operation is PlanOperation.APPLY
                                )
                                if same_owner_update:
                                    # The previous release may own this annotation's
                                    # SSA field. Exact content reconciliation keeps
                                    # crash safety without forcing that ownership.
                                    annotations.pop(OPERATION_ANNOTATION, None)
                                    payload["same_owner_update"] = True
                                    # Three-way removal: explicit nulls for the
                                    # keys an earlier release declared.
                                    manifest = removal_patch(manifest, action.removals)
                                    metadata = manifest["metadata"]
                                elif action.removals:
                                    raise ProviderError("ownership-precondition-failed")
                                else:
                                    annotations[OPERATION_ANNOTATION] = row[
                                        "operation_id"
                                    ]
                                if current is not None:
                                    metadata.update(
                                        {
                                            "uid": current.manifest["metadata"]["uid"],
                                            "resourceVersion": current.manifest[
                                                "metadata"
                                            ]["resourceVersion"],
                                        }
                                    )
                                takeover = _mode(action) is AdoptionMode.TAKEOVER
                                if takeover:
                                    assert current is not None
                                    assert action.adoption is not None
                                    payload["adoption"] = {
                                        "mode": AdoptionMode.TAKEOVER.value,
                                        "previous_owner": action.adoption.previous_owner,
                                        "transferred_managers": _transferred(
                                            action, self.provider.field_manager
                                        ),
                                    }
                                # Admission/field conflicts are surfaced without force;
                                # a takeover's admission check is the one forced
                                # dry run (see KubernetesProvider.take_over).
                                if same_owner_update:
                                    assert current is not None
                                    self.provider.update_owned(
                                        current,
                                        manifest,
                                        dry_run=True,
                                        deadline=deadline,
                                    )
                                elif takeover:
                                    assert current is not None
                                    self.provider.take_over(
                                        current,
                                        manifest,
                                        transferred_managers=tuple(
                                            payload["adoption"]["transferred_managers"]
                                        ),
                                        dry_run=True,
                                        deadline=deadline,
                                    )
                                else:
                                    self.provider.write(
                                        _identity(action.resource.ref),
                                        manifest,
                                        create=action.operation is PlanOperation.CREATE,
                                        dry_run=True,
                                        deadline=deadline,
                                    )
                            self._guard(execution, authorization, deadline)
                            payload["written_at"] = datetime.now(UTC).isoformat()
                            self.journal.record(
                                execution, row["ordinal"], "intent", payload
                            )
                            try:
                                if action.operation is PlanOperation.DELETE:
                                    assert current is not None
                                    self.provider.delete(
                                        current.identity,
                                        uid=current.manifest["metadata"]["uid"],
                                        resource_version=current.manifest["metadata"][
                                            "resourceVersion"
                                        ],
                                        deadline=deadline,
                                    )
                                    result = None
                                elif takeover:
                                    assert manifest is not None
                                    assert current is not None
                                    result, moved = self.provider.take_over(
                                        current,
                                        manifest,
                                        transferred_managers=tuple(
                                            payload["adoption"]["transferred_managers"]
                                        ),
                                        deadline=deadline,
                                    )
                                    payload["adoption"]["completed_transfer"] = list(
                                        moved
                                    )
                                else:
                                    assert manifest is not None
                                    result = (
                                        self.provider.update_owned(
                                            current,
                                            manifest,
                                            deadline=deadline,
                                        )
                                        if same_owner_update and current is not None
                                        else self.provider.write(
                                            _identity(action.resource.ref),
                                            manifest,
                                            create=action.operation
                                            is PlanOperation.CREATE,
                                            deadline=deadline,
                                        )
                                    )
                                if self.after_response is not None:
                                    self.after_response(row["ordinal"])
                                if result is not None:
                                    payload = self._receipt(result, payload)
                                self.journal.record(
                                    execution, row["ordinal"], "applied", payload
                                )
                            except ProviderError as error:
                                if not error.ambiguous:
                                    self.journal.record(
                                        execution, row["ordinal"], "failed", payload
                                    )
                                raise
                        row = self.journal.actions(execution)[row["ordinal"]]
                    elif row["state"] == "intent":
                        base = dict(row["payload"])
                        adoption = base.get("adoption")
                        if "replace" in base:
                            result = self._resume_replace(
                                execution, row, action, base, authorization, deadline
                            )
                            retained_reconciled = False
                        elif _is_takeover(row):
                            # The takeover is idempotent: converge again from
                            # whatever step was interrupted.
                            assert isinstance(adoption, dict)
                            result, moved = self._resume_takeover(
                                row, action, adoption, deadline
                            )
                            retained_reconciled = False
                            base["adoption"] = adoption | {
                                "completed_transfer": sorted(
                                    set(adoption.get("completed_transfer", ()))
                                    | set(moved)
                                )
                            }
                        else:
                            result, retained_reconciled = self._reconcile(
                                row, action, deadline
                            )
                        payload = (
                            base
                            if result is None
                            else self._receipt(
                                result,
                                base
                                | (
                                    {"retained_reconciled": True}
                                    if retained_reconciled
                                    else {}
                                ),
                            )
                        )
                        self.journal.record(
                            execution, row["ordinal"], "applied", payload
                        )
                        row = self.journal.actions(execution)[row["ordinal"]]
                    if action.resource.ref in deferred:
                        deferred_rows.append((row, action))
                    else:
                        self._ready(execution, row, action, authorization, deadline)
                # A WFFC claim is allowed to remain Pending only while its
                # declared consumer is being submitted. Once both exist, bind
                # the claim first and retain normal workload readiness checks.
                deferred_rows.sort(
                    key=lambda item: (
                        item[1].resource.ref.kind != "PersistentVolumeClaim"
                    )
                )
                for row, action in deferred_rows:
                    self._ready(execution, row, action, authorization, deadline)
                self.journal.set_state(execution, "ready")
            except ProviderError as error:
                self.journal.record_failure(execution, error.category)
                self.journal.set_state(
                    execution,
                    "cancelled"
                    if error.category == "cancelled"
                    else "blocked"
                    if error.ambiguous
                    else "failed",
                )
            return self.journal.summary(execution)

    def compensate(
        self,
        execution: str,
        plan: DeploymentPlan,
        snapshot: ObservedSnapshot,
        authorization: ExecutionAuthorization,
    ) -> dict[str, Any]:
        with self.telemetry.operation(
            "compensate",
            execution,
            plan_hash=plan.plan_hash,
            action_count=len(plan.actions),
        ) as operation:
            result = self._compensate(execution, plan, snapshot, authorization)
            operation.state = result["state"]
            return result

    def _compensate(
        self,
        execution: str,
        plan: DeploymentPlan,
        snapshot: ObservedSnapshot,
        authorization: ExecutionAuthorization,
    ) -> dict[str, Any]:
        """Reverse owned changes only; retained and deleted state is kept.

        A takeover adoption is reversed like an update: its pre-adoption
        content is re-applied, while ownership stays with this owner (the
        transferred field managers are not restored). Metadata-only adoptions of
        retained objects are never reversed, so volumes and their data are
        untouched.
        """
        deadline = time.monotonic() + self.limits.max_seconds
        with self.journal.exclusive():
            self._validate(plan, snapshot, authorization, deadline)
            self._owners = self._granted_owners(authorization)
            self.journal.start(
                execution,
                self._binding(plan, authorization),
                [uuid.uuid4().hex for _ in plan.actions],
            )
            if not authorization.compensation_resources:
                raise ValueError("compensation requires explicit resource scope")
            artifact = snapshot.discovery
            assert artifact is not None
            # Refresh all originally requested types; partial scope blocks all undo.
            fresh = capture_discovery(
                self.provider,
                DiscoveryRequest(
                    plan.target, artifact.coverage.requested, artifact.limits
                ),
                capture_id=uuid.uuid4().hex,
                captured_at=datetime.now(UTC).isoformat(),
                policy_revision=artifact.coverage.policy_revision,
                deadline=deadline,
            )
            if not fresh.execution_authoritative:
                raise ValueError("compensation requires complete fresh discovery")
            current_by_ref = {
                ResourceRef(**item.identity.__dict__): item for item in fresh.resources
            }
            rows = self.journal.actions(execution)
            if any(row["state"] == "intent" for row in rows):
                raise ValueError("reconcile ambiguous operations before compensation")
            eligible = []
            for row, action in reversed(list(zip(rows, plan.actions, strict=False))):
                ref = action.resource.ref
                if row["state"] in {
                    "pending",
                    "failed",
                    "compensated",
                } or action.operation in {
                    PlanOperation.NOOP,
                    PlanOperation.DELETE,
                    # A replaced object is restored from its backup file, by
                    # an operator (see docs/release_cli.md), never here.
                    PlanOperation.REPLACE,
                }:
                    continue
                if action.operation is PlanOperation.ADOPT and not _is_takeover(row):
                    continue
                current = current_by_ref.get(ref)
                if ref.kind in RETAINED_KINDS or (
                    current is not None and current.retained
                ):
                    continue
                if ref not in authorization.compensation_resources:
                    raise ValueError("compensation action outside authorization")
                if row["state"] == "compensating":
                    eligible.append((row, action, current))
                    continue
                self._verify_receipt(row, action, current)
                if (
                    current is None
                    or current.ownership is not Ownership.MANAGED
                    or _version(current).resource_version
                    != row["payload"]["resource_version"]
                ):
                    raise ValueError("compensation UID/version/ownership changed")
                if action.operation is PlanOperation.CREATE and any(
                    current.manifest["metadata"]["uid"] == owner.get("uid")
                    for child in fresh.resources
                    for owner in child.manifest["metadata"].get("ownerReferences", [])
                ):
                    raise ValueError("compensation would affect descendants")
                eligible.append((row, action, current))
            for row, action, current in eligible:
                if time.monotonic() >= deadline or timestamp(
                    authorization.expires_at, allow_future=True
                ) <= datetime.now(UTC):
                    raise ValueError("compensation deadline or authorization expired")
                payload = row["payload"]
                if row["state"] == "compensating":
                    if action.operation is PlanOperation.CREATE and current is None:
                        self.journal.record(
                            execution, row["ordinal"], "compensated", payload
                        )
                        continue
                    if (
                        current is not None
                        and current.ownership is Ownership.MANAGED
                        and current.manifest["metadata"]
                        .get("annotations", {})
                        .get(OPERATION_ANNOTATION)
                        == row["operation_id"] + "-undo"
                        and current.manifest["metadata"]["uid"] == payload["uid"]
                    ):
                        before = self._load(payload["before"], current.identity)
                        if _contains(
                            ResourceIntent.from_manifest(current.manifest).manifest,
                            _restore_manifest(before)
                            if _is_takeover(row)
                            else ResourceIntent.from_manifest(before.manifest).manifest,
                        ):
                            self.journal.record(
                                execution, row["ordinal"], "compensated", payload
                            )
                            continue
                    raise ValueError(
                        "ambiguous compensation requires observation; retry blocked"
                    )
                assert current is not None
                self.journal.record(execution, row["ordinal"], "compensating", payload)
                if action.operation is PlanOperation.CREATE:
                    self.provider.delete(
                        current.identity,
                        uid=payload["uid"],
                        resource_version=payload["resource_version"],
                        deadline=deadline,
                    )
                else:
                    if _is_takeover(row):
                        # Restore the previous spec; the object stays ours and
                        # the forced takeover is not repeated (force=false).
                        before_manifest = _restore_manifest(
                            self._load(payload["before"], current.identity)
                        )
                        before_manifest["metadata"]["uid"] = payload["uid"]
                        before_manifest["metadata"].setdefault("annotations", {})[
                            OWNER_ANNOTATION
                        ] = self.provider.owner_id
                    else:
                        before_manifest = self._load(
                            payload["before"], current.identity
                        ).manifest
                        before_manifest.pop("status", None)
                        before_manifest["metadata"].pop("managedFields", None)
                    before_manifest["metadata"]["resourceVersion"] = payload[
                        "resource_version"
                    ]
                    before_manifest["metadata"].setdefault("annotations", {})[
                        OPERATION_ANNOTATION
                    ] = row["operation_id"] + "-undo"
                    self.provider.write(
                        current.identity,
                        before_manifest,
                        create=False,
                        deadline=deadline,
                    )
                if self.after_response is not None:
                    self.after_response(row["ordinal"])
                self.journal.record(execution, row["ordinal"], "compensated", payload)
            self.journal.set_state(execution, "compensated-with-retention")
            return self.journal.summary(execution)


def _project(value: Any, template: Any) -> Any:
    """The part of ``value`` at the paths ``template`` declares."""
    if isinstance(value, dict) and isinstance(template, dict):
        return {
            key: _project(value[key], child)
            for key, child in template.items()
            if key in value
        }
    if (
        isinstance(value, list)
        and isinstance(template, list)
        and len(value) == len(template)
    ):
        return [
            _project(item, child) for item, child in zip(value, template, strict=True)
        ]
    return value


def _scaled(resource: DiscoveredResource) -> bool:
    """Whether a ``scale`` subresource entry (an autoscaler) owns ``spec.replicas``."""
    try:
        entries = field_manager_entries(resource.manifest)
    except ValueError:
        return False
    return any(
        entry.subresource == "scale"
        and "f:replicas" in (entry.fields.get("f:spec") or {})
        for entry in entries
    )


def _is_takeover(row: dict[str, Any]) -> bool:
    adoption = row["payload"].get("adoption")
    return (
        isinstance(adoption, dict)
        and adoption.get("mode") == AdoptionMode.TAKEOVER.value
    )


def _metadata_write(action: PlanAction) -> bool:
    """Metadata-only adoption, or an APPLY that changes only retained metadata."""
    return _mode(action) is AdoptionMode.METADATA_ONLY or (
        action.operation is PlanOperation.APPLY and bool(action.metadata_changes)
    )


def _spec_contained(actual: Any, expected: Any) -> bool:
    """Everything but labels/annotations is contained (retained objects)."""
    return manifest_contains(
        without_metadata_maps(actual), without_metadata_maps(expected)
    )


def _mode(action: PlanAction) -> AdoptionMode | None:
    return (
        action.adoption.mode
        if action.operation is PlanOperation.ADOPT and action.adoption is not None
        else None
    )


def _transferred(action: PlanAction, field_manager: str) -> list[str]:
    """Planned transferred managers other than the executor's own."""
    assert action.adoption is not None
    return sorted(set(action.adoption.transferred_managers) - {field_manager})


def _restore_manifest(before: DiscoveredResource) -> dict[str, Any]:
    """Pre-adoption content to re-apply, minus controller-owned bookkeeping."""
    manifest = ResourceIntent.from_manifest(before.manifest).manifest
    annotations = manifest.get("metadata", {}).get("annotations", {})
    annotations.pop("deployment.kubernetes.io/revision", None)
    if not annotations:
        manifest.get("metadata", {}).pop("annotations", None)
    return manifest


def _contains(actual: Any, expected: Any) -> bool:
    """SSA may add server defaults. Every explicitly desired value must survive."""
    return manifest_contains(actual, expected)
