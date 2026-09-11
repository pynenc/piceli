"""Explicitly authorized, recoverable execution of immutable Piceli plans."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from piceli.k8s.ops.bounds import positive, seconds, text, timestamp
from piceli.telemetry import NoopTelemetry
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
    DeploymentPlan,
    ObservedSnapshot,
    PlanAction,
    PlanOperation,
    ResourceIntent,
    ResourcePrecondition,
    ResourceRef,
)
from piceli.k8s.ops.secret_versions import (
    PRIVATE_VALUE,
    SecretBinding,
    SecretVersionRef,
    SecretVersionStore,
    replace_pointer,
)

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

    def __post_init__(self) -> None:
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

    def __post_init__(self) -> None:
        positive(self.max_actions, "actions", 4096)
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


def _reference(value: dict[str, str]) -> SecretVersionRef:
    return SecretVersionRef(**value)


def _reject_inline_secrets(intent: ResourceIntent) -> None:
    def check(raw: Any, public: Any) -> None:
        if isinstance(raw, dict) and isinstance(public, dict):
            for key in raw:
                check(raw[key], public[key])
        elif isinstance(raw, list) and isinstance(public, list):
            for left, right in zip(raw, public):
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
    ) -> None:
        self.provider = provider
        self.journal = journal
        self.secrets = secrets
        self.limits = limits or ExecutionLimits()
        # Fault injection occurs strictly after HTTP response and before receipt.
        self.after_response = after_response
        self.telemetry = telemetry or NoopTelemetry()

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
        if timestamp(authorization.expires_at, allow_future=True) <= datetime.now(
            timezone.utc
        ):
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
        if (
            datetime.now(timezone.utc) - timestamp(artifact.captured_at)
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
            elif (
                current.ownership is Ownership.UNMANAGED
                and action.operation is not PlanOperation.ADOPT
            ):
                raise ValueError("unmanaged resource requires exact adoption action")
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
                self._manifest(action.resource)
        self.provider.verify_target(deadline=deadline)

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
        if timestamp(authorization.expires_at, allow_future=True) <= datetime.now(
            timezone.utc
        ):
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
        if current is None or _version(current) != action.precondition:
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
        if (
            current.ownership is Ownership.UNMANAGED
            and action.operation is not PlanOperation.ADOPT
        ):
            raise ProviderError("ownership-precondition-failed")
        if action.operation is PlanOperation.DELETE and current.retained:
            raise ProviderError("retained-resource")
        if action.operation is PlanOperation.NOOP and not _contains(
            ResourceIntent.from_manifest(current.manifest).manifest,
            self._manifest(action.resource),
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
        if (
            metadata.get("annotations", {}).get(OWNER_ANNOTATION)
            != self.provider.owner_id
        ):
            raise ProviderError("ownership-precondition-failed")
        if not _contains(
            ResourceIntent.from_manifest(current.manifest).manifest,
            self._manifest(action.resource),
        ):
            raise ProviderError("retained-content-precondition-failed")
        return True

    def _reconcile(
        self, row: dict[str, Any], action: PlanAction, deadline: float
    ) -> tuple[DiscoveredResource | None, bool]:
        current = self.provider.get(_identity(action.resource.ref), deadline=deadline)
        if action.operation is PlanOperation.DELETE:
            if current is None:
                return None, False
            raise ProviderError("ambiguous-delete-blocked", ambiguous=True)
        if current is None:
            raise ProviderError("ambiguous-write-blocked", ambiguous=True)
        if self._retained_identical(current, action):
            return current, True
        metadata = current.manifest["metadata"]
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
            if current is not None:
                raise ProviderError("deleted-resource-reappeared")
            return
        if current is None or current.manifest["metadata"]["uid"] != payload["uid"]:
            raise ProviderError("recreated-object")
        after = self._load(payload["after"], current.identity)
        if (
            _owners(current) != _owners(after)
            or ResourceIntent.from_manifest(current.manifest).manifest
            != ResourceIntent.from_manifest(after.manifest).manifest
        ):
            raise ProviderError("applied-resource-drift")
        if current.ownership != after.ownership:
            raise ProviderError("ownership-precondition-failed")
        if (
            action.operation is not PlanOperation.NOOP
            and not payload.get("retained_reconciled", False)
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
        for _ in range(self.limits.max_polls):
            self._guard(execution, authorization, end)
            current = self.provider.get(_identity(action.resource.ref), deadline=end)
            self._verify_receipt(row, action, current)
            if action.operation is PlanOperation.DELETE or (
                current is not None
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
                for row, action in zip(self.journal.actions(execution), plan.actions):
                    self._guard(execution, authorization, deadline)
                    if row["state"] in {"compensated", "compensating"}:
                        raise ProviderError("compensation-already-started")
                    if row["state"] in {"pending", "failed"}:
                        current = self.provider.get(
                            _identity(action.resource.ref), deadline=deadline
                        )
                        self._check_current(current, action, snapshot)
                        payload = (
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
                                metadata.setdefault("annotations", {}).update(
                                    {
                                        OWNER_ANNOTATION: self.provider.owner_id,
                                        OPERATION_ANNOTATION: row["operation_id"],
                                    }
                                )
                                if current is not None:
                                    metadata.update(
                                        {
                                            "uid": action.precondition.uid,
                                            "resourceVersion": action.precondition.resource_version,
                                        }
                                    )
                                # Admission/field conflicts are surfaced without force ownership.
                                self.provider.write(
                                    _identity(action.resource.ref),
                                    manifest,
                                    create=action.operation is PlanOperation.CREATE,
                                    dry_run=True,
                                    deadline=deadline,
                                )
                            self._guard(execution, authorization, deadline)
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
                                else:
                                    assert manifest is not None
                                    result = self.provider.write(
                                        _identity(action.resource.ref),
                                        manifest,
                                        create=action.operation is PlanOperation.CREATE,
                                        deadline=deadline,
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
                        result, retained_reconciled = self._reconcile(
                            row, action, deadline
                        )
                        payload = (
                            row["payload"]
                            if result is None
                            else self._receipt(
                                result,
                                row["payload"]
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
                    key=lambda item: item[1].resource.ref.kind
                    != "PersistentVolumeClaim"
                )
                for row, action in deferred_rows:
                    self._ready(execution, row, action, authorization, deadline)
                self.journal.set_state(execution, "ready")
            except ProviderError as error:
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
        """Reverse owned changes only; retained, adopted and deleted state is kept."""
        deadline = time.monotonic() + self.limits.max_seconds
        with self.journal.exclusive():
            self._validate(plan, snapshot, authorization, deadline)
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
                captured_at=datetime.now(timezone.utc).isoformat(),
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
            for row, action in reversed(list(zip(rows, plan.actions))):
                ref = action.resource.ref
                if row["state"] in {
                    "pending",
                    "failed",
                    "compensated",
                } or action.operation in {
                    PlanOperation.NOOP,
                    PlanOperation.ADOPT,
                    PlanOperation.DELETE,
                }:
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
                ) <= datetime.now(timezone.utc):
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
                            ResourceIntent.from_manifest(before.manifest).manifest,
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


def _contains(actual: Any, expected: Any) -> bool:
    """SSA may add server defaults. Every explicitly desired value must survive."""
    if isinstance(actual, dict) and isinstance(expected, dict):
        return all(
            key in actual and _contains(actual[key], value)
            for key, value in expected.items()
        )
    return actual == expected
