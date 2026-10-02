"""Version 1 public service records; never carry credentials or private evidence.

These models describe the service boundary, not a second deployment engine.
Imports are side-effect free. Unknown state is explicit and independent of
presence. Resource versions and event cursors are opaque strings.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

API_VERSION: Literal["piceli.ui.v1"] = "piceli.ui.v1"


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Capability(Record):
    allowed: bool
    reason: str | None = None


class Target(Record):
    id: str
    name: str
    namespace: str
    cluster_uid: str | None = None
    namespace_uid: str | None = None


class Principal(Record):
    id: str
    name: str
    kind: Literal["local", "oidc", "service"] = "local"


class Capabilities(Record):
    api_version: Literal["piceli.ui.v1"] = API_VERSION
    mode: Literal["local", "cluster"] = "local"
    principal: Principal
    targets: list[Target]
    actions: dict[str, Capability]


class Freshness(Record):
    state: Literal["connected", "reconnecting", "stale", "unavailable"]
    observed_at: str | None = None
    reason: str | None = None


class SourceRevision(Record):
    kind: Literal["local", "git", "oci"]
    revision: str
    entrypoint: str


class Application(Record):
    id: str
    name: str
    target: Target
    definition_kind: Literal["inventory", "release", "pipeline"]
    ownership: Literal["native", "external", "inventory"]
    source: SourceRevision | None = None
    health: Literal["healthy", "progressing", "degraded", "suspended", "unknown"] = (
        "unknown"
    )
    relation: Literal["in_sync", "changes", "drifted", "unknown", "external"] = (
        "unknown"
    )
    operation: str = "idle"
    freshness: Freshness
    capabilities: dict[str, Capability] = Field(default_factory=dict)


class ResourceIdentity(Record):
    target_id: str
    api_version: str
    kind: str
    namespace: str
    name: str
    uid: str | None = None


class Resource(Record):
    id: str
    identity: ResourceIdentity
    resource_version: str | None = None
    presence: Literal["present", "missing", "unknown"]
    ownership: Literal["managed", "unmanaged", "external", "unknown"]
    health: Literal["healthy", "progressing", "degraded", "suspended", "unknown"] = (
        "unknown"
    )
    relation: Literal["in_sync", "changes", "drifted", "unknown", "external"] = (
        "unknown"
    )
    phase: str | None = None
    images: list[str] = Field(default_factory=list)
    ports: list[int] = Field(default_factory=list)
    containers: list[str] = Field(default_factory=list)
    owner_uids: list[str] = Field(default_factory=list)
    manifest: dict[str, JsonValue] | None = None
    conditions: list[dict[str, JsonValue]] = Field(default_factory=list)
    capabilities: dict[str, Capability] = Field(default_factory=dict)


class PartialError(Record):
    scope: str
    code: str


class ApplicationPage(Record):
    items: list[Application]
    cursor: str
    next_page: str | None = None


class ResourcePage(Record):
    items: list[Resource]
    cursor: str
    event_cursor: str | None = None
    freshness: Freshness
    partial: list[PartialError] = Field(default_factory=list)
    next_page: str | None = None


class FieldChange(Record):
    path: str
    op: Literal["add", "remove", "replace"]
    before: JsonValue = None
    after: JsonValue = None


class ResourceDiff(Record):
    resource: ResourceIdentity
    operation: str
    basis: Literal["server-dry-run", "client"]
    changes: list[FieldChange]
    unified: str
    not_compared: list[str] = Field(default_factory=list)


class PlanRequest(Record):
    intent: Literal["deploy", "rollback"] = "deploy"
    release: str | None = None


class EvaluationPreview(Record):
    """Exact authorization to evaluate a frozen source without deployment credentials."""

    id: str
    application_id: str
    digest: str
    source: SourceRevision
    renderer_digest: str
    input_digest: str
    files: list[str]
    intent: Literal["deploy", "rollback"]
    release: str | None = None
    expires_at: str
    limits: dict[str, int]
    warnings: list[str] = Field(default_factory=list)


class EvaluationRequest(Record):
    preview_id: str
    approved_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)


class PlanStep(Record):
    """Exact executor action order; dependency level is absent for prune actions."""

    ordinal: int = Field(ge=0)
    level: int | None = None
    resource: ResourceIdentity
    operation: str
    dependencies: list[ResourceIdentity] = Field(default_factory=list)


class DesiredResource(Record):
    """Public desired manifest; masked JSON pointers are not comparable values."""

    resource: ResourceIdentity
    manifest: dict[str, JsonValue]
    not_compared: list[str] = Field(default_factory=list)


class PlanRecord(Record):
    id: str
    application_id: str
    digest: str
    target: Target
    engine_digest: str | None = None
    source: SourceRevision | None = None
    intent: Literal["deploy", "rollback"]
    release: str
    expires_at: str
    summary: dict[str, int]
    diffs: list[ResourceDiff]
    actions: list[dict[str, JsonValue]] = Field(default_factory=list)
    checks: dict[str, JsonValue] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    evaluation_id: str | None = None
    plan_kind: Literal["release", "pipeline-preview", "pipeline-materialized"] = (
        "release"
    )
    precondition_digest: str | None = None
    authorization: Literal["manual", "policy"] = "manual"
    policy_digest: str | None = None
    steps: list[PlanStep] = Field(default_factory=list)
    desired_resources: list[DesiredResource] = Field(default_factory=list)
    desired_resources_complete: bool = False


class PlanSummary(Record):
    """Stored plan identity and counts, without manifests or private engine inputs."""

    id: str
    application_id: str
    digest: str
    target: Target
    source: SourceRevision | None = None
    intent: Literal["deploy", "rollback"]
    release: str
    created_at: str
    expires_at: str
    summary: dict[str, int]
    plan_kind: Literal["release", "pipeline-preview", "pipeline-materialized"] = (
        "release"
    )
    desired_resources_complete: bool = False


class PlanPage(Record):
    items: list[PlanSummary]
    cursor: str
    next_page: str | None = None


class Evaluation(Record):
    id: str
    application_id: str
    state: Literal["queued", "running", "succeeded", "failed", "interrupted"]
    plan_id: str | None = None
    error_code: str | None = None
    preview_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class OperationRequest(Record):
    plan_id: str
    approved_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)


class ClusterBuildPlanRequest(Record):
    commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    cache_key: str = Field(min_length=1, max_length=200)


class ClusterBuildOperationRequest(Record):
    plan_id: str
    approved_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)


class RecoveryRequest(Record):
    """A new attempt of the same approved plan, never implicit replanning."""

    approved_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)


class CancelRequest(Record):
    idempotency_key: str = Field(min_length=1, max_length=128)


class EnvironmentActionRequest(Record):
    verb: Literal["up", "down", "seed"]
    branch: str = Field(min_length=1, max_length=250)
    approved_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    source: str | None = Field(default=None, max_length=250)


class GitOpsApprovalRequest(Record):
    branch: str = Field(min_length=1, max_length=250)
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class GitOpsPromotionRequest(Record):
    branch: str = Field(min_length=1, max_length=250)
    commit: str = Field(pattern=r"^[0-9a-f]{7,40}$")


class CompositionSyncRequest(Record):
    """Sync one environment of the composition, or one of its components."""

    env: str = Field(pattern=r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
    component: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,126}[A-Za-z0-9])?$"
    )


class NamedEnvironmentApprovalRequest(Record):
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class NamedEnvironmentPromotionRequest(Record):
    branch: str = Field(min_length=1, max_length=250)
    commit: str = Field(pattern=r"^[0-9a-f]{7,40}$")


class ProfileSwitchRequest(Record):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")


class Stage(Record):
    name: str
    state: Literal[
        "pending", "running", "succeeded", "failed", "skipped", "interrupted"
    ]
    reason: str | None = None


class JournalAction(Record):
    """Last durable resource state and recorded write time, not event timestamps."""

    ordinal: int = Field(ge=0)
    resource: ResourceIdentity
    operation: str
    state: str
    written_at: str | None = None


class JournalEvent(Record):
    sequence: int = Field(ge=0)
    ordinal: int | None = None
    state: str


class JournalLog(Record):
    """A bounded, redacted pod log tail recorded in an execution diagnosis."""

    resource: ResourceIdentity
    pod: str
    container: str
    lines: list[str]


class ExecutionJournalRecord(Record):
    execution_id: str
    state: str
    actions: list[JournalAction] = Field(default_factory=list)
    events: list[JournalEvent] = Field(default_factory=list)
    logs: list[JournalLog] = Field(default_factory=list)
    truncated: bool = False


class Operation(Record):
    id: str
    application_id: str
    plan_id: str
    approved_digest: str
    actor: str
    trigger: Literal["ui", "cli", "git"]
    state: Literal[
        "queued",
        "running",
        "succeeded",
        "failed",
        "interrupted",
        "cancelling",
        "cancelled",
    ]
    created_at: str
    updated_at: str
    stages: list[Stage] = Field(default_factory=list)
    engine_execution_id: str | None = None
    error_code: str | None = None
    capabilities: dict[str, Capability] = Field(default_factory=dict)
    attempt: int = 1
    recovery_of: str | None = None
    engine_release: str | None = None
    pipeline_run_id: str | None = None
    receipts: list[dict[str, JsonValue]] = Field(default_factory=list)
    journal: ExecutionJournalRecord | None = None
    deployment_outcome: Literal["unknown", "succeeded", "failed"] = "unknown"
    checks_outcome: Literal["unknown", "succeeded", "failed", "not_configured"] = (
        "unknown"
    )


class OperationPage(Record):
    items: list[Operation]
    cursor: str
    next_page: str | None = None


class ReleaseSummary(Record):
    name: str
    selected: bool = False
    state: str
    created_at: str | None = None
    capabilities: dict[str, Capability] = Field(default_factory=dict)


class ReleasePage(Record):
    items: list[ReleaseSummary]
    cursor: str
    next_page: str | None = None


class ServiceError(Record):
    code: str
    message: str
    retryable: bool = False
    correlation_id: str


class Event(Record):
    cursor: str
    scope: str
    kind: Literal["upsert", "delete", "operation", "reset", "heartbeat"]
    subject_id: str | None = None


class LogRequest(Record):
    application_id: str
    resource_id: str
    container: str | None = None
    previous: bool = False
    tail_lines: int = Field(default=2000, ge=1, le=5000)


class LogBatch(Record):
    lines: list[str]
    cursor: str
    dropped: int = 0
    gap: bool = False


class LogSource(Record):
    pod: ResourceIdentity
    containers: list[str]
    phase: str | None = None
    started_at: str | None = None


class LogSourcePage(Record):
    items: list[LogSource]
    freshness: Freshness
    partial: list[PartialError] = Field(default_factory=list)


class AccessSession(Record):
    id: str
    application_id: str
    resource: ResourceIdentity
    principal_id: str
    binding_location: Literal["local_client", "server", "gateway"]
    state: Literal["pending", "connecting", "ready", "failed", "stopped", "expired"]
    endpoint: str | None = None
    expires_at: str
    reason: str | None = None
    local_port: int | None = None
    remote_port: int | None = None


class AccessStartRequest(Record):
    resource_id: str
    resource_uid: str
    local_port: int = Field(ge=1, le=65535)
    remote_port: int = Field(ge=1, le=65535)
    duration_seconds: int = Field(default=900, ge=30, le=3600)


class AccessPage(Record):
    items: list[AccessSession]


class RemoteAccessStartRequest(Record):
    resource_id: str
    resource_uid: str
    remote_port: int = Field(ge=1, le=65535)
    duration_seconds: int = Field(default=900, ge=30, le=3600)


class RemoteAccessTicket(Record):
    session: AccessSession
    pairing_secret: str


class RemoteAccessClaimRequest(Record):
    pairing_secret: str = Field(min_length=40, max_length=128)


class RemoteAccessLease(Record):
    session: AccessSession
    target: Target
    lease_secret: str


class RemoteAccessHeartbeatRequest(Record):
    lease_secret: str = Field(min_length=40, max_length=128)
    state: Literal["connecting", "ready", "failed"]
    local_port: int | None = Field(default=None, ge=1, le=65535)


class RemoteAccessReleaseRequest(Record):
    lease_secret: str = Field(min_length=40, max_length=128)
