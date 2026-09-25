"""Release lifecycle behind ``piceli release``: plan, apply, rollback, resume, stop.

This module is the testable core of the CLI. It adds no new engine: every
execution goes through :class:`~piceli.k8s.release.ReleaseWorkflow`, which in
turn uses the durable deployment session, the plan executor and the journal.

Two plan modes exist:

``create``
    The spec's fingerprint (images, composition output, secret generator
    settings) names a release that does not exist yet.
    :meth:`ReleaseWorkflow.create` materializes the secret inputs once, binds
    the discovery snapshot and the grant into an immutable session and
    catalogs it. ``apply`` runs that exact session bundle; ``resume`` resumes it.

``reapply``
    The release already exists (unchanged spec, or an explicit rollback
    target). Its archived composition is planned against fresh discovery and
    ``apply`` runs it through :meth:`ReleaseWorkflow.rollback`, which executes a
    new bundle and selects the release when it becomes ready.

Every plan is persisted privately under its plan hash. ``apply`` executes a
persisted plan only when the caller presents that hash, so what was reviewed is
exactly what runs; the plan hash is deterministic for a given discovery
snapshot, and the snapshot is part of the persisted plan.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from piceli.checks import (
    Check,
    CheckContext,
    CheckReport,
    PythonCheck,
    parse_checks,
    run_checks,
)
from piceli.k8s.ops.bounds import timestamp
from piceli.k8s.ops.discovery import (
    RELEASE_CLUSTER_KINDS,
    RELEASE_NAMESPACE_ANNOTATION,
    RELEASE_REFUSED_CLUSTER_KINDS,
    RETAINED_KINDS,
    DiscoveryArtifact,
    DiscoveryLimits,
    DiscoveryRequest,
    PlanTarget,
    ResourceScope,
    ResourceType,
    capture_discovery,
)
from piceli.k8s.ops.dry_run import capture_server_dry_runs, probe_candidates
from piceli.k8s.ops.execution_journal import ExecutionJournal
from piceli.k8s.ops.executor import (
    ActionGrant,
    ExecutionAuthorization,
    ExecutionLimits,
    PlanExecutor,
)
from piceli.k8s.ops.field_diff import plan_diffs
from piceli.k8s.ops.kubernetes_provider import ProviderError
from piceli.k8s.ops.plan import (
    REPLACEABLE_MANAGED_KINDS,
    DeploymentComponent,
    DeploymentComposition,
    DeploymentPlan,
    ObservedSnapshot,
    Ownership,
    PlanAuthorization,
    PrivateEvidence,
    ResourceIntent,
    ResourceRef,
    autoscaled_replicas,
    build_plan,
    declared_union,
    field_drift,
    immutable_changes,
    private_evidence,
    replace_refusal,
    retained_content_contained,
    transferable_managers,
)
from piceli.k8s.ops.provider_factory import ProviderBinding, build_provider
from piceli.k8s.ops.secret_versions import (
    SecretVersionRef,
    SecretVersionStore,
    private_directory,
)
from piceli.k8s.ops.session import composition_from_archive
from piceli.k8s.release import (
    ReleaseCatalog,
    ReleaseRecord,
    ReleaseSource,
    ReleaseWorkflow,
)
from piceli.k8s.release_secret_spec import (
    SecretError,
    check_rotation,
    consumed_outputs,
)
from piceli.k8s.release_secrets import (
    ImportSources,
    Materialized,
    config_digest,
    decode,
    describe,
    input_names,
    materialize,
)
from piceli.k8s.release_spec import (
    ImageRef,
    NodeRef,
    ReleaseSpec,
    ReleaseSpecError,
    parse_adopt_entry,
)

if TYPE_CHECKING:
    from piceli.k8s.secret_sources import Fetched

POLICY_REVISION = "piceli.release-cli/v1"
_HASH = re.compile(r"[0-9a-f]{64}")
ProviderFactory = Callable[[ReleaseSpec], ProviderBinding]
#: ``factory(spec, release name, {image name: stored image})`` → a check context.
CheckContextFactory = Callable[[ReleaseSpec, str, Mapping[str, Any]], CheckContext]


class ReleaseError(ValueError):
    """A release operation was refused; the message is safe to print.

    ``code`` names the refusal (see docs/release_cli.md) and ``details`` holds
    structured, printable data such as the list of blocking objects.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


#: Discovery and dry runs are captured at most this many times per plan.
OBSERVE_ATTEMPTS = 3


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _now() -> datetime:
    return datetime.now(UTC)


def _write_private(path: Path, text: str) -> None:
    private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def default_provider_factory(spec: ReleaseSpec) -> ProviderBinding:
    """Build and identity-check the provider named by the spec's ``[target]``."""
    release = spec.model.release
    return build_provider(
        spec.kubeconfig_target(),
        field_manager=release.field_manager,
        owner_id=release.owner,
        inherited_owner_ids=release.inherited_owners,
    )


def default_check_context(
    spec: ReleaseSpec, release: str, images: Mapping[str, Any]
) -> CheckContext:
    """The check context of a release: the spec's ``[target]``, never ambient."""
    target = spec.model.target
    return CheckContext(
        spec.resolve(target.kubeconfig),
        target.context,
        target.namespace,
        release,
        images,
        values=spec.model.values,
        base=spec.base,
        transport=target.transport,
        request_seconds=target.request_seconds,
        exec_policy=spec.kubeconfig_target().exec_policy,
    )


def _check_summary(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The short form of a check outcome kept in the release history."""
    if value is None:
        return None
    if value.get("skipped"):
        return {"skipped": True, "flag": value.get("flag")}
    return {
        "passed": value["passed"],
        "failed": list(value["failed"]),
        "count": len(value["results"]),
    }


def _grant(
    plan: DeploymentPlan,
    snapshot: ObservedSnapshot,
    *,
    authorization_id: str,
    expires_at: str,
    max_age: float,
    field_manager: str,
    owner_id: str,
    inherited_owner_ids: Sequence[str] = (),
) -> ExecutionAuthorization:
    """Deterministic grant: the same parameters always rebuild the same grant."""
    return ExecutionAuthorization(
        authorization_id,
        plan.target,
        snapshot.provenance,
        plan.plan_hash,
        snapshot.snapshot_hash,
        field_manager,
        owner_id,
        tuple(ActionGrant.for_action(action) for action in plan.actions),
        expires_at,
        # The approved plan hash covers every action, cluster-scoped ones
        # included (each is listed with ``cluster_scoped`` in the plan).
        cluster_resources=tuple(
            action.resource.ref
            for action in plan.actions
            if not action.resource.ref.namespace
        ),
        compensation_resources=tuple(
            action.resource.ref
            for action in plan.actions
            if action.resource.ref.kind not in RETAINED_KINDS
        ),
        max_evidence_age_seconds=max_age,
        inherited_owner_ids=tuple(inherited_owner_ids),
    )


def _plan_authorization(
    target: PlanTarget,
    *,
    prune: bool,
    adopt: Sequence[Mapping[str, str]] = (),
    inherited: Sequence[str] = (),
    field_manager: str,
    replace: Sequence[Mapping[str, str]] = (),
    previous: Sequence[ResourceIntent] = (),
) -> PlanAuthorization:
    return PlanAuthorization(
        target,
        tuple(ResourceRef(**item) for item in adopt),
        prune,
        tuple(inherited),
        field_manager,
        tuple(ResourceRef(**item) for item in replace),
        tuple(previous),
    )


def _previous_declared(
    catalog: ReleaseCatalog, names: Sequence[str]
) -> tuple[ResourceIntent, ...]:
    """What the named catalogued releases declared, merged per object.

    Records are immutable, so the same names always give the same result; a
    record that no longer exists contributes nothing.
    """
    intents: list[ResourceIntent] = []
    for name in names:
        try:
            record = catalog.get(name)
        except ValueError:
            continue
        for component in composition_from_archive(record.archive).components:
            intents.extend(component.resources)
    return declared_union(intents)


def _private(
    composition: DeploymentComposition,
    snapshot: ObservedSnapshot,
    store: SecretVersionStore,
) -> PrivateEvidence:
    """Private evidence for secret-bound objects (see ``private_evidence``)."""
    return private_evidence(
        composition,
        snapshot,
        lambda reference: store.resolve(snapshot.target, reference),
    )


def _declared_match(
    entry: str, declared: Sequence[ResourceRef], what: str
) -> ResourceRef:
    api_version, kind, name = parse_adopt_entry(entry, what=what)
    matches = [
        ref
        for ref in declared
        if ref.kind == kind
        and ref.name == name
        and (api_version is None or ref.api_version == api_version)
    ]
    if len(matches) != 1:
        raise ReleaseError(
            f"{what} {entry!r} does not name exactly one resource declared by "
            "the composition",
            code=f"{what}-entry-not-declared",
        )
    return matches[0]


def _label(ref: ResourceRef) -> str:
    return f"{ref.kind}/{ref.name}"


#: How a release spec or ``piceli release plan`` authorizes unmanaged objects.
RELEASE_AUTHORIZATION = (
    "authorize them with --adopt or --replace Kind/name (repeatable), "
    "[release] adopt/replace or --adopt-all-desired, or delete them"
)


def blocking_message(
    blocking: Sequence[Mapping[str, Any]], authorize: str = RELEASE_AUTHORIZATION
) -> str:
    """The refusal's sentence for ``blocking`` objects (each with ``suggest``).

    ``authorize`` says how the caller unblocks unmanaged objects: the release
    flags by default; a pipeline names its own declaration instead.
    """
    unmanaged = [
        item for item in blocking if item["code"] == "resource-requires-adoption"
    ]
    parts = []
    if unmanaged:
        parts.append(
            "existing objects are not managed by this release's owner: "
            + ", ".join(
                f"{item['kind']}/{item['name']} ({' or '.join(item['suggest'])})"
                for item in unmanaged
            )
            + "; "
            + authorize
        )
    for item in blocking:
        if item["code"] != "resource-requires-adoption":
            parts.append(f"{item['kind']}/{item['name']}: {item['message']}")
    return "; ".join(parts)


def scoped_composition(
    composition: DeploymentComposition, namespace: str
) -> DeploymentComposition:
    """Check a release composition's scope; stamp its cluster-scoped objects.

    Namespaced objects must target ``namespace``. Cluster-scoped objects must
    be of a kind in :data:`~piceli.k8s.ops.discovery.RELEASE_CLUSTER_KINDS`
    (ClusterRole, ClusterRoleBinding), which get the
    ``piceli.io/namespace: <namespace>`` annotation (typed apps render it),
    or of another kind already annotated with it (``app.resource(...,
    scope="cluster")``), except the kinds in
    :data:`~piceli.k8s.ops.discovery.RELEASE_REFUSED_CLUSTER_KINDS`. Only this
    namespace's release manages them. An object that already names another
    namespace is refused.

    :raises ReleaseSpecError: ``invalid-composition``.
    """
    components = []
    for component in composition.components:
        resources = []
        for resource in component.resources:
            ref = resource.ref
            if ref.namespace:
                if ref.namespace != namespace:
                    raise ReleaseSpecError(
                        f"{ref.kind}/{ref.name} targets namespace {ref.namespace!r}",
                        code="invalid-composition",
                    )
                resources.append(resource)
                continue
            manifest = resource.manifest
            metadata = manifest.setdefault("metadata", {})
            annotations = metadata.get("annotations") or {}
            declared = annotations.get(RELEASE_NAMESPACE_ANNOTATION)
            if (ref.api_version, ref.kind) not in RELEASE_CLUSTER_KINDS and (
                declared is None or ref.kind in RELEASE_REFUSED_CLUSTER_KINDS
            ):
                raise ReleaseSpecError(
                    "a release manages cluster-scoped objects only per namespace: "
                    "rbac.authorization.k8s.io/v1 ClusterRole and "
                    "ClusterRoleBinding, and other kinds declared with "
                    'app.resource(..., scope="cluster") (annotated '
                    f"{RELEASE_NAMESPACE_ANNOTATION}); never Namespace, "
                    f"CustomResourceDefinition or PersistentVolume: {ref.kind}/{ref.name}",
                    code="invalid-composition",
                )
            if declared is not None and declared != namespace:
                raise ReleaseSpecError(
                    f"{ref.kind}/{ref.name} is annotated "
                    f"{RELEASE_NAMESPACE_ANNOTATION}={declared!r}, but the release "
                    f"namespace is {namespace!r}",
                    code="invalid-composition",
                )
            if declared is None:
                if resource.secret_bindings:
                    raise ReleaseSpecError(
                        f"{ref.kind}/{ref.name}: cluster-scoped objects cannot "
                        "bind secret values",
                        code="invalid-composition",
                    )
                metadata["annotations"] = {
                    **annotations,
                    RELEASE_NAMESPACE_ANNOTATION: namespace,
                }
                resource = ResourceIntent.from_manifest(manifest, resource.dependencies)
            resources.append(resource)
        components.append(
            DeploymentComponent(
                component.name, tuple(resources), component.dependencies
            )
        )
    return DeploymentComposition(tuple(components))


def _check_scopes(
    composition: DeploymentComposition, artifact: DiscoveryArtifact
) -> None:
    """Refuse objects whose scope contradicts the API server's discovery.

    A typed resource declares its scope (``App.resource(..., scope=...)``);
    the server's discovery is authoritative, so a namespaced declaration of a
    cluster-scoped kind (or the reverse) is refused before planning.

    :raises ReleaseSpecError: ``resource-scope-mismatch``.
    """
    scopes = {
        (item.resource_type.api_version, item.resource_type.kind): item.scope
        for item in artifact.coverage.api_resources
    }
    for component in composition.components:
        for resource in component.resources:
            ref = resource.ref
            served = scopes.get((ref.api_version, ref.kind))
            if served is None:
                continue
            declared = (
                ResourceScope.NAMESPACED if ref.namespace else ResourceScope.CLUSTER
            )
            if declared is not served:
                raise ReleaseSpecError(
                    f"{ref.kind}/{ref.name} is declared {declared.value}, but the "
                    f"API server serves {ref.api_version} {ref.kind} as "
                    f"{served.value}; declare it with scope={served.value!r}",
                    code="resource-scope-mismatch",
                )


@dataclass(frozen=True)
class OwnershipResolution:
    """What a plan is authorized to adopt or replace, and what was not needed."""

    adopt: list[dict[str, str]]
    replace: list[dict[str, str]]
    adopt_not_needed: list[str]
    replace_not_needed: list[str]


def resolve_ownership(
    requested: Sequence[str],
    composition: DeploymentComposition,
    snapshot: ObservedSnapshot,
    inherited: Sequence[str],
    field_manager: str,
    *,
    replace: Sequence[str] = (),
    adopt_all_desired: bool = False,
) -> OwnershipResolution:
    """Map ``Kind/name`` entries to declared resources that need adoption or replace.

    An entry must name a resource the composition declares (a typo is an
    error, never a silent no-op). Adopt entries whose object is absent, or
    already managed with nothing to reclaim, need no adoption and are returned
    separately for the report, so a standing ``[release] adopt`` list keeps
    working after the first release. A managed, non-retained object that
    other clients have written to since (transferable foreign field managers)
    is taken over again, which reclaims those fields.

    Replace entries must name an existing **unmanaged, non-retained** object
    that no other object owns, or a managed Job or StatefulSet (whose
    immutable fields change); absent objects, and managed ones without an
    immutable change, are reported as not needed and
    every other case is refused. A managed object whose immutable fields
    would change and that is not named for replace blocks the plan
    (``immutable-field-changed``). ``adopt_all_desired`` adopts every unmanaged
    object the composition declares that is not replaced, and nothing else.

    Every object that blocks the plan is reported in one refusal, each with
    the flags that would unblock it.
    """
    declared = [
        resource.ref
        for component in composition.components
        for resource in component.resources
    ]
    intents: dict[ResourceRef, ResourceIntent] = {
        resource.ref: resource
        for component in composition.components
        for resource in component.resources
    }
    observed = {item.intent.ref: item for item in snapshot.resources}
    adopt: set[ResourceRef] = set()
    replaced: set[ResourceRef] = set()
    not_needed: set[str] = set()
    replace_not_needed: set[str] = set()
    blocking: list[dict[str, Any]] = []
    refused: set[ResourceRef] = set()
    for entry in dict.fromkeys(replace):
        ref = _declared_match(entry, declared, "replace")
        current = observed.get(ref)
        if current is None or (
            # A managed Job or StatefulSet is replaced only for a change the
            # API server cannot apply; a standing entry never reruns a Job.
            current.ownership is Ownership.MANAGED
            and ref.kind in REPLACEABLE_MANAGED_KINDS
            and not current.retained
            and not immutable_changes(intents[ref], current)
        ):
            replace_not_needed.add(_label(ref))
            continue
        refusal = replace_refusal(current)
        if refusal is not None:
            refused.add(ref)
            blocking.append(
                {
                    "kind": ref.kind,
                    "name": ref.name,
                    "code": "replace-refused",
                    "message": refusal,
                    "suggest": (
                        [f"--adopt {_label(ref)}"]
                        if current.ownership is Ownership.UNMANAGED
                        else [f"remove {_label(ref)} from --replace/[release] replace"]
                    ),
                }
            )
            continue
        replaced.add(ref)
    for entry in dict.fromkeys(requested):
        ref = _declared_match(entry, declared, "adopt")
        if ref in replaced:
            raise ReleaseError(
                f"{_label(ref)} is named by both adopt and replace; choose one",
                code="adopt-and-replace",
            )
        current = observed.get(ref)
        if current is not None and (
            current.ownership is Ownership.UNMANAGED
            or (current.retained and current.owner in set(inherited))
            or (
                not current.retained
                and transferable_managers(
                    current.field_managers, exclude=(field_manager,)
                )
            )
        ):
            adopt.add(ref)
        else:
            not_needed.add(_label(ref))
    if adopt_all_desired:
        adopt |= {
            ref
            for ref in declared
            if ref in observed
            and ref not in replaced
            and observed[ref].ownership is Ownership.UNMANAGED
        }
    for ref in declared:
        current = observed.get(ref)
        if current is None or ref in refused:
            continue
        if (
            ref not in adopt
            and ref not in replaced
            and current.ownership is Ownership.UNMANAGED
        ):
            retained = current.retained
            blocking.append(
                {
                    "kind": ref.kind,
                    "name": ref.name,
                    "code": "resource-requires-adoption",
                    "message": "exists and is not managed by this release's owner"
                    + ("; retained: replace is never allowed" if retained else ""),
                    "suggest": [f"--adopt {_label(ref)}"]
                    + ([] if retained else [f"--replace {_label(ref)}"]),
                }
            )
        elif (
            ref not in replaced
            and ref not in adopt
            and current.ownership is Ownership.MANAGED
            and (changed := immutable_changes(intents[ref], current))
        ):
            blocking.append(
                {
                    "kind": ref.kind,
                    "name": ref.name,
                    "code": "immutable-field-changed",
                    "message": "immutable fields would change ("
                    + ", ".join(changed)
                    + "); the API server refuses the update",
                    "suggest": [f"--replace {_label(ref)}"],
                }
            )
        elif (
            current.retained
            and (ref in adopt or current.ownership is Ownership.MANAGED)
            and not retained_content_contained(intents[ref], current)
        ):
            blocking.append(
                {
                    "kind": ref.kind,
                    "name": ref.name,
                    "code": "retained-content-differs",
                    "message": "retained object: its spec/data differ from the "
                    "composition, and only labels and annotations may change",
                    "suggest": [
                        "change the composition to match the live object",
                    ],
                }
            )
    if blocking:
        blocking.sort(key=lambda item: (item["kind"], item["name"]))
        raise ReleaseError(
            blocking_message(blocking),
            code=blocking[0]["code"]
            if len({item["code"] for item in blocking}) == 1
            else "plan-blocked",
            details={"blocking": blocking},
        )
    return OwnershipResolution(
        [ref.__dict__ for ref in sorted(adopt)],
        [ref.__dict__ for ref in sorted(replaced)],
        sorted(not_needed),
        sorted(replace_not_needed),
    )


def resolve_adoptions(
    requested: Sequence[str],
    composition: DeploymentComposition,
    snapshot: ObservedSnapshot,
    inherited: Sequence[str],
    field_manager: str,
) -> tuple[list[dict[str, str]], list[str]]:
    """Adoption-only view of :func:`resolve_ownership` (kept for callers)."""
    resolved = resolve_ownership(
        requested, composition, snapshot, inherited, field_manager
    )
    return resolved.adopt, resolved.adopt_not_needed


def _drift(
    composition: DeploymentComposition,
    snapshot: ObservedSnapshot,
    field_manager: str,
    adopt: Sequence[Mapping[str, str]],
) -> list[dict[str, Any]]:
    """Drift of objects this plan does not already take over."""
    adopted = [dict(item) for item in adopt]
    return [
        item
        for item in field_drift(composition, snapshot, field_manager)
        if item["resource"] not in adopted
    ]


def _autoscaled(
    composition: DeploymentComposition, snapshot: ObservedSnapshot, field_manager: str
) -> list[dict[str, Any]]:
    """What the plan does with autoscaled ``spec.replicas`` (see the plan module)."""
    _, report = autoscaled_replicas(composition, snapshot, field_manager)
    return [item.to_dict() for item in report]


def _summary(plan: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for action in plan["actions"]:
        counts[action["operation"]] = counts.get(action["operation"], 0) + 1
    return dict(sorted(counts.items()))


def _compact_actions(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "operation": action["operation"],
            "kind": action["resource"]["kind"],
            "name": action["resource"]["name"],
            "artifact_digest": action["artifact_digest"],
            **({"adoption": action["adoption"]} if "adoption" in action else {}),
            **(
                {"metadata_only": action["metadata_only"]}
                if "metadata_only" in action
                else {}
            ),
            **({"replace": action["replace"]} if "replace" in action else {}),
            **({"removes": action["removes"]} if "removes" in action else {}),
            **({"cluster_scoped": True} if not action["resource"]["namespace"] else {}),
        }
        for action in plan["actions"]
    ]


def _execution_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    states: dict[str, int] = {}
    for action in result.get("actions", []):
        states[action["state"]] = states.get(action["state"], 0) + 1
    summary = {
        "execution_id": result.get("execution_id"),
        "state": result.get("state"),
        "cancelled": result.get("cancelled", False),
        "actions": dict(sorted(states.items())),
    }
    if "failure_category" in result:
        summary["failure_category"] = result["failure_category"]
    return summary


def _adopted(
    journal: ExecutionJournal,
    execution_id: str,
    result: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Adoptions the journal recorded for this execution (public fields only)."""
    execution = str(result.get("rollback_execution_id") or execution_id)
    try:
        rows = journal.export_execution(execution)
    except ValueError:
        return []
    desired = rows["binding"].get("revision", {}).get("desired_state", {})
    planned = desired.get("actions", []) if isinstance(desired, dict) else []
    adopted = []
    for row, action in zip(rows["actions"], planned, strict=False):
        replace = row["payload"].get("replace")
        if isinstance(replace, dict) and row["state"] in {"applied", "ready"}:
            adopted.append(
                {
                    "kind": action["resource"]["kind"],
                    "name": action["resource"]["name"],
                    "mode": "replace",
                    "backup": replace.get("backup"),
                    "backup_sha256": replace.get("backup_sha256"),
                    "deleted_uid": replace.get("deleted_uid"),
                }
            )
            continue
        adoption = row["payload"].get("adoption")
        if isinstance(adoption, dict) and row["state"] in {"applied", "ready"}:
            adopted.append(
                {
                    "kind": action["resource"]["kind"],
                    "name": action["resource"]["name"],
                    **{
                        key: adoption[key]
                        for key in (
                            "mode",
                            "previous_owner",
                            "transferred_managers",
                            "completed_transfer",
                            "metadata_changes",
                        )
                        if key in adoption
                    },
                }
            )
    return adopted


@dataclass(frozen=True)
class _Ownership:
    """Requested ownership transitions of one plan (spec lists plus flags)."""

    adopt: tuple[str, ...] = ()
    replace: tuple[str, ...] = ()
    adopt_all_desired: bool = False

    def resolve(
        self,
        composition: DeploymentComposition,
        snapshot: ObservedSnapshot,
        inherited: Sequence[str],
        field_manager: str,
    ) -> OwnershipResolution:
        return resolve_ownership(
            self.adopt,
            composition,
            snapshot,
            inherited,
            field_manager,
            replace=self.replace,
            adopt_all_desired=self.adopt_all_desired,
        )

    def report(self, resolved: OwnershipResolution) -> dict[str, Any]:
        return {
            "adopt": sorted(f"{i['kind']}/{i['name']}" for i in resolved.adopt),
            "replace": sorted(f"{i['kind']}/{i['name']}" for i in resolved.replace),
            "adopt_all_desired": self.adopt_all_desired,
            "replace_not_needed": resolved.replace_not_needed,
        }


@dataclass
class PlanResult:
    """A persisted, approvable plan."""

    release: str
    mode: str
    intent: str
    plan: dict[str, Any] = field(repr=False)
    source: dict[str, str]
    images: dict[str, dict[str, str]]
    secrets: dict[str, str]
    expires_at: str
    drift: list[dict[str, Any]] = field(default_factory=list)
    adopt_not_needed: list[str] = field(default_factory=list)
    # What this plan was authorized to adopt/replace (bound to the plan hash
    # through the ADOPT/REPLACE actions).
    authorized: dict[str, Any] = field(default_factory=dict)
    # The post-deploy checks apply will run, and the rollback policy.
    checks: dict[str, Any] = field(default_factory=dict)
    # Field-level diffs of the changed objects (evidence, not in the hash)
    # and the objects the server dry run could not cover.
    diffs: list[dict[str, Any]] = field(default_factory=list)
    dry_run_unavailable: list[dict[str, Any]] = field(default_factory=list)
    # Workloads whose ``spec.replicas`` an autoscaler owns (see
    # ``autoscaled_replicas``); informational, bound through the actions.
    autoscaled: list[dict[str, Any]] = field(default_factory=list)

    @property
    def plan_hash(self) -> str:
        return str(self.plan["plan_hash"])

    @property
    def counts(self) -> dict[str, int]:
        return _summary(self.plan)

    @property
    def changes(self) -> bool:
        return any(op != "no-op" for op in self.counts)

    def to_dict(self, *, full: bool = False) -> dict[str, Any]:
        return {
            "release": self.release,
            "mode": self.mode,
            "intent": self.intent,
            "plan_hash": self.plan_hash,
            "expires_at": self.expires_at,
            "source": self.source,
            "images": self.images,
            "secrets": self.secrets,
            "summary": self.counts,
            "actions": _compact_actions(self.plan),
            "drift": self.drift,
            "adopt_not_needed": self.adopt_not_needed,
            "authorized": self.authorized,
            "checks": self.checks,
            "diffs": self.diffs,
            "dry_run_unavailable": self.dry_run_unavailable,
            "autoscaled": self.autoscaled,
            **({"plan": self.plan} if full else {}),
        }


class _History:
    """Small locked JSON log of executions started by this CLI."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        value = json.loads(self.path.read_text())
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ReleaseError(
                "release history is malformed", code="release-history-malformed"
            )
        entries = value.get("entries")
        if not isinstance(entries, list):
            raise ReleaseError(
                "release history is malformed", code="release-history-malformed"
            )
        return entries

    @contextmanager
    def _locked(self) -> Iterator[list[dict[str, Any]]]:
        private_directory(self.path.parent)
        with open(str(self.path) + ".lock", "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            entries = self.entries()
            yield entries
            _write_private(
                self.path, _canonical({"schema_version": 1, "entries": entries}) + "\n"
            )

    def append(self, entry: dict[str, Any]) -> None:
        with self._locked() as entries:
            entries.append(entry)

    def update(self, execution_id: str, **values: Any) -> None:
        with self._locked() as entries:
            for entry in reversed(entries):
                if entry["execution_id"] == execution_id:
                    entry.update(values)
                    return

    def deployed(self) -> list[str]:
        """Release names of successful executions, oldest first."""
        return [
            entry["release"] for entry in self.entries() if entry["state"] == "ready"
        ]

    def previous(self) -> str | None:
        """What was running before the latest change.

        After a completed change this is the last ready release that differs
        from the current one; when the latest execution did not become ready
        (failed, cancelled, blocked or still running) it is the last ready
        release itself, so a rollback restores the last known good state.
        """
        entries = self.entries()
        deployed = self.deployed()
        if not deployed:
            return None
        if entries[-1]["state"] != "ready":
            return deployed[-1]
        return next((n for n in reversed(deployed) if n != deployed[-1]), None)


class ReleaseRunner:
    """Run release commands for one spec; each call opens its own provider."""

    def __init__(
        self,
        spec: ReleaseSpec,
        *,
        provider_factory: ProviderFactory = default_provider_factory,
        check_context_factory: CheckContextFactory = default_check_context,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.spec = spec
        #: Human progress while an execution applies and waits for readiness
        #: (``applying 3/7: Deployment/web``, ``waiting for Deployment/web to
        #: be ready (12s)``): callers print it on stderr. Kinds and names only.
        self.progress = progress
        self.provider_factory = provider_factory
        self.check_context_factory = check_context_factory
        #: Called after every execution journal commit (before the IO it
        #: records): shared state writes the state through here.
        self.checkpoint: Callable[[], None] | None = None
        self.state = spec.state_dir
        self.history = _History(self.state / "history.json")
        # Restorable copies of objects deleted by ``replace`` (owner-only).
        self.backups = self.state / "backups"

    # ------------------------------------------------------------------ state
    def _open(self) -> tuple[ReleaseCatalog, ExecutionJournal, SecretVersionStore]:
        private_directory(self.state)
        journal = ExecutionJournal(self.spec.journal_path)
        journal.on_commit = self.checkpoint
        return (
            ReleaseCatalog(self.spec.catalog_path),
            journal,
            SecretVersionStore(self.spec.secret_store_path),
        )

    def _sidecar_path(self, name: str) -> Path:
        return self.state / "releases" / f"{name}.json"

    def _discovery_path(self, name: str) -> Path:
        return self.state / "releases" / f"{name}.discovery.json"

    def _plan_path(self, plan_hash: str) -> Path:
        if not _HASH.fullmatch(plan_hash):
            raise ReleaseError(
                "plan hash must be 64 lowercase hex characters",
                code="invalid-plan-hash",
            )
        return self.state / "plans" / f"{plan_hash}.json"

    def _sidecar(self, name: str) -> dict[str, Any]:
        path = self._sidecar_path(name)
        return json.loads(path.read_text()) if path.exists() else {}

    def _prune_expired_plans(self) -> None:
        directory = self.state / "plans"
        if not directory.is_dir():
            return
        for path in directory.glob("*.json"):
            try:
                expires = json.loads(path.read_text())["expires_at"]
                if timestamp(expires, allow_future=True) <= _now():
                    path.unlink(missing_ok=True)
            except (OSError, ValueError, KeyError):
                continue

    @staticmethod
    def _check_target(catalog: ReleaseCatalog, target: PlanTarget) -> None:
        """Trust on first use: a state directory serves one cluster and namespace."""
        for record in catalog.records():
            archived = record.archive.to_dict()["revision"]["desired_state"]["target"]
            if archived != target.__dict__:
                raise ReleaseError(
                    "cluster identity differs from the one recorded in this state "
                    f"directory (release {record.name!r}); refusing to continue",
                    code="cluster-identity-changed",
                )

    # ---------------------------------------------------------- composition
    def _nodes(self, binding: ProviderBinding) -> dict[str, NodeRef]:
        return {
            alias: NodeRef(node.name, str(node.uid))
            for alias, node in binding.identity.nodes.items()
        }

    def _declared_inputs(self) -> dict[str, tuple[str, ...]]:
        return {
            name: input_names(name, generator)
            for name, generator in self.spec.model.secrets.items()
        }

    def _factory(
        self,
        function: Callable[..., DeploymentComposition],
        images: Mapping[str, ImageRef],
        nodes: Mapping[str, NodeRef],
    ) -> Callable[[Mapping[str, SecretVersionRef]], DeploymentComposition]:
        def factory(refs: Mapping[str, SecretVersionRef]) -> DeploymentComposition:
            from piceli.app.app import App

            context = self.spec.context(images, refs, nodes)
            try:
                composition = function(context)
                if isinstance(composition, App):
                    # Returning the App keeps its access declarations visible
                    # to `piceli access` / `piceli status`; render it here.
                    composition = composition.composition(context)
            except ValueError:
                raise  # model validation keeps its own code and message
            except Exception as error:  # the user's composition raised
                from piceli.cli_contract import describe_user_error

                raise ReleaseSpecError(
                    "evaluating composition "
                    f"{self.spec.model.release.composition!r} failed: "
                    f"{describe_user_error(error)}",
                    code="invalid-composition",
                ) from None
            if not isinstance(composition, DeploymentComposition):
                raise ReleaseSpecError(
                    "the composition function must return an App or a DeploymentComposition",
                    code="invalid-composition",
                )
            return scoped_composition(composition, self.spec.model.target.namespace)

        return factory

    def _placeholders(self) -> dict[str, SecretVersionRef]:
        """Deterministic stand-in references for every exposed secret input."""
        return {
            name: SecretVersionRef(
                "0" * 32, hashlib.sha256(name.encode()).hexdigest()[:32]
            )
            for group in self._declared_inputs().values()
            for name in group
        }

    def _preview_composition(
        self,
        factory: Callable[[Mapping[str, SecretVersionRef]], DeploymentComposition],
    ) -> tuple[DeploymentComposition, list[dict[str, Any]]]:
        """Build with placeholder references to name the release and check bindings."""
        placeholders = self._placeholders()
        names = list(placeholders)
        by_ref = {ref: name for name, ref in placeholders.items()}
        composition = factory(placeholders)
        bound: set[str] = set()
        material = []
        for component in composition.components:
            resources = []
            for resource in component.resources:
                bindings = []
                for binding in resource.secret_bindings:
                    if binding.reference not in by_ref:
                        raise ReleaseSpecError(
                            "the composition binds a reference that is not a "
                            "declared secret input",
                            code="invalid-composition",
                        )
                    bound.add(by_ref[binding.reference])
                    bindings.append(
                        {
                            "pointer": binding.json_pointer,
                            "input": by_ref[binding.reference],
                        }
                    )
                resources.append(
                    {
                        "resource": resource.ref.__dict__,
                        "manifest": resource.redacted_manifest(),
                        "dependencies": [
                            item.__dict__ for item in resource.dependencies
                        ],
                        "bindings": bindings,
                    }
                )
            material.append(
                {
                    "name": component.name,
                    "dependencies": list(component.dependencies),
                    "resources": resources,
                }
            )
        # Outputs a template consumes may stay unbound (they reach the cluster
        # through the template); every other declared input must be bound.
        unused = sorted(set(names) - bound - consumed_outputs(self.spec.model.secrets))
        if unused:
            raise ReleaseSpecError(
                f"declared secret inputs are not bound by the composition: {unused}",
                code="invalid-composition",
            )
        return composition, material

    @staticmethod
    def _kinds(composition: DeploymentComposition) -> set[ResourceType]:
        return {
            ResourceType(resource.ref.api_version, resource.ref.kind)
            for component in composition.components
            for resource in component.resources
        }

    def _source(self, images: Mapping[str, ImageRef]) -> ReleaseSource:
        if len(images) == 1:
            identity = next(iter(images.values())).identity
            return ReleaseSource("oci", identity, artifact_digest=identity)
        # Several images: record the whole set (name -> digest); the identity
        # is the digest of that canonical map.
        return ReleaseSource.image_set(
            {name: image.identity for name, image in images.items()}
        )

    # ------------------------------------------------------------ discovery
    def _discover(
        self,
        binding: ProviderBinding,
        kinds: set[ResourceType],
        composition: DeploymentComposition | None = None,
    ) -> DiscoveryArtifact:
        for item in self.spec.model.discovery.kinds:
            api_version, _, kind = item.rpartition("/")
            kinds.add(ResourceType(api_version, kind))
        settings = self.spec.model.discovery
        artifact = capture_discovery(
            binding.provider,
            DiscoveryRequest(
                binding.target,
                tuple(sorted(kinds)),
                DiscoveryLimits(
                    max_seconds=settings.max_seconds,
                    call_seconds=min(settings.call_seconds, settings.max_seconds),
                ),
            ),
            capture_id=uuid.uuid4().hex,
            captured_at=_now().isoformat(),
            policy_revision=POLICY_REVISION,
        )
        if not artifact.execution_authoritative:
            failures = [
                f"{item.resource_type.api_version}/{item.resource_type.kind}: "
                f"{item.kind.value}"
                for item in artifact.coverage.failures
            ]
            raise ReleaseError(
                "discovery is incomplete, refusing to plan"
                + (f" ({'; '.join(failures)})" if failures else ""),
                code="discovery-incomplete",
            )
        if composition is not None:
            _check_scopes(composition, artifact)
        return artifact

    def _dry_runs(
        self,
        binding: ProviderBinding,
        composition: DeploymentComposition,
        artifact: DiscoveryArtifact,
    ) -> tuple[DiscoveryArtifact, list[dict[str, Any]]]:
        """Attach server dry runs of the desired writes (never persisted)."""
        artifact, unavailable = capture_server_dry_runs(
            binding.provider,
            artifact,
            composition,
            deadline=time.monotonic() + self.spec.model.discovery.max_seconds,
        )
        return artifact, [item.to_dict() for item in unavailable]

    def _observe(
        self,
        binding: ProviderBinding,
        composition: DeploymentComposition,
        kinds: set[ResourceType],
    ) -> tuple[DiscoveryArtifact, list[dict[str, Any]]]:
        """Discovery plus the server dry runs, consistent with each other.

        A dry run is preconditioned on the discovered resourceVersion, so an
        object written in between (a controller's status update while a
        rollout finishes) answers ``conflict``. Discovery and the dry runs are
        then captured again, a few times, before planning without evidence.
        """
        for attempt in range(OBSERVE_ATTEMPTS):
            artifact, unavailable = self._dry_runs(
                binding, composition, self._discover(binding, kinds, composition)
            )
            if attempt + 1 == OBSERVE_ATTEMPTS or not any(
                item["reason"] == "conflict" for item in unavailable
            ):
                break
            time.sleep(0.5 * (attempt + 1))
        return artifact, unavailable

    # -------------------------------------------------------------- secrets
    def _private_inputs(
        self,
        catalog: ReleaseCatalog,
        store: SecretVersionStore,
        binding: ProviderBinding,
        rotate: Sequence[str],
        external: Mapping[str, Fetched] | None = None,
    ) -> Materialized:
        """Carry values over from earlier releases unless rotated or reconfigured.

        Called only after the plan was validated; imports read their source
        here (a live Secret through the release's own provider). External
        sources were read before the release was named (``external``).
        """
        records = sorted(
            catalog.records(),
            key=lambda record: self._sidecar(record.name).get("created_at", ""),
            reverse=True,
        )

        def carry(
            name: str, part: str, digest: str, wanted: tuple[str, ...]
        ) -> tuple[dict[str, str], str] | None:
            for record in records:
                sidecar = self._sidecar(record.name)
                recorded = sidecar.get("secret_parts", {}).get(name, {}).get(part)
                if recorded is None and part == "":
                    recorded = sidecar.get("secrets", {}).get(name)  # 0.2.0 releases
                if recorded != digest:
                    continue
                refs = self._stored_refs(record, sidecar, store)
                if all(item in refs for item in wanted):
                    return (
                        {
                            item: store.resolve(binding.target, refs[item])
                            for item in wanted
                        },
                        record.name,
                    )
            return None

        def read_secret(name: str, key: str) -> bytes:
            return binding.provider.read_secret_key(name, key)

        return materialize(
            self.spec.model.secrets,
            rotate=rotate,
            carry=carry,
            sources=ImportSources(self.spec.resolve, read_secret),
            external=external,
        )

    def _external_sources(self, *, create: bool) -> dict[str, Fetched]:
        """Read every external secret source now (values stay in memory).

        The keyed digests name the release, so a changed value makes a new
        release and an unchanged one re-plans the existing release. ``create``
        creates the private digest key when missing (``plan``); without it
        (``diff``, read-only) a missing key means no release can match.
        """
        from secrets import token_bytes

        from piceli.k8s.release_secret_spec import is_external
        from piceli.k8s.secret_sources import (
            KEY_FILE,
            ExternalSources,
            fetch_all,
            source_key,
        )

        specs = {
            name: spec
            for name, spec in self.spec.model.secrets.items()
            if is_external(spec)
        }
        if not specs:
            return {}
        key = source_key(self.state / KEY_FILE, create=create)
        return fetch_all(
            specs,
            key if key is not None else token_bytes(32),
            ExternalSources(self.spec.resolve),
        )

    @staticmethod
    def _stored_refs(
        record: ReleaseRecord, sidecar: Mapping[str, Any], store: SecretVersionStore
    ) -> dict[str, SecretVersionRef]:
        """Session inputs plus the release's unbound (internal) secret versions."""
        refs = dict(record.archive.inputs())
        for item, version in sidecar.get("secret_refs", {}).items():
            refs.setdefault(item, SecretVersionRef(store.store_id, version))
        return refs

    # ----------------------------------------------------------------- plan
    def plan(
        self,
        *,
        rotate: Sequence[str] = (),
        rollback_to: str | None = None,
        adopt: Sequence[str] = (),
        replace: Sequence[str] = (),
        adopt_all_desired: bool = False,
    ) -> PlanResult:
        """Capture discovery and persist an approvable plan.

        Without ``rollback_to`` the spec decides the release; with it, an
        existing catalogued release (a name or ``previous``) is re-planned.
        ``adopt`` and ``replace`` add ``Kind/name`` entries to the spec's
        ``[release] adopt``/``replace`` lists for this plan only;
        ``adopt_all_desired`` adopts every unmanaged declared object.
        """
        spec = self.spec.model
        for entry in adopt:
            parse_adopt_entry(entry)
        unknown = sorted(set(rotate) - set(spec.secrets))
        if unknown:
            raise ReleaseError(
                f"cannot rotate undeclared secrets: {unknown}",
                code="unknown-rotate-secret",
            )
        for entry in replace:
            parse_adopt_entry(entry, what="replace")
        for check in spec.checks:
            if isinstance(check, PythonCheck):
                check.resolve(self.spec.base)  # refuse an unimportable check now
        requested = _Ownership(
            tuple(dict.fromkeys((*spec.release.adopt, *adopt))),
            tuple(dict.fromkeys((*spec.release.replace, *replace))),
            adopt_all_desired,
        )
        images = self.spec.images()
        function = self.spec.load_composition()
        binding = self.provider_factory(self.spec)
        try:
            catalog, journal, store = self._open()
            try:
                self._check_target(catalog, binding.target)
                self._prune_expired_plans()
                factory = self._factory(function, images, self._nodes(binding))
                if rollback_to is None:
                    composition, material = self._preview_composition(factory)
                    if rotate:
                        # Refuse --rotate of a template, static or external
                        # value before any source is read.
                        check_rotation(spec.secrets, rotate)
                    external = self._external_sources(create=True)
                    fingerprint = self._fingerprint(images, material, rotate, external)
                    name = f"{spec.release.name}-{fingerprint[:12]}"
                    existing = {record.name for record in catalog.records()}
                    if name not in existing:
                        return self._plan_create(
                            name,
                            fingerprint,
                            composition,
                            factory,
                            images,
                            binding,
                            catalog,
                            journal,
                            store,
                            rotate,
                            requested,
                            external,
                        )
                    intent = "apply"
                else:
                    if rotate:
                        raise ReleaseError(
                            "--rotate is not valid for a rollback",
                            code="rotate-not-valid-for-rollback",
                        )
                    name = self.resolve_rollback_target(rollback_to, catalog)
                    intent = "rollback"
                return self._plan_reapply(
                    name, intent, binding, catalog, store, requested
                )
            finally:
                journal.close()
                store.close()
        finally:
            binding.close()

    def _fingerprint(
        self,
        images: Mapping[str, ImageRef],
        material: list[dict[str, Any]],
        rotate: Sequence[str] = (),
        external: Mapping[str, Fetched] | None = None,
    ) -> str:
        """The release fingerprint; its first 12 characters name the release.

        External secret values contribute their keyed digests (only when the
        spec has external sources, so other fingerprints are unchanged).
        """
        document: dict[str, Any] = {
            "images": {n: i.identity for n, i in images.items()},
            "composition": material,
            "secrets": {
                n: config_digest(g) for n, g in self.spec.model.secrets.items()
            },
            "rotation": uuid.uuid4().hex if rotate else None,
        }
        if external:
            document["sources"] = {n: item.digest for n, item in external.items()}
        return hashlib.sha256(_canonical(document).encode()).hexdigest()

    def diff(
        self,
        *,
        adopt: Sequence[str] = (),
        replace: Sequence[str] = (),
        adopt_all_desired: bool = False,
    ) -> dict[str, Any]:
        """What ``plan`` would change, as field diffs; read-only.

        Captures discovery and the server dry runs like ``plan`` but builds
        the plan in memory only: no plan, release, secret or discovery file is
        written, and the cluster receives only reads and ``dryRun=All``
        requests. Secret-bound objects are compared privately only for an
        unchanged release (a new release's secret inputs are not
        materialized), and their values are never shown.
        """
        spec = self.spec.model
        for entry in adopt:
            parse_adopt_entry(entry)
        for entry in replace:
            parse_adopt_entry(entry, what="replace")
        requested = _Ownership(
            tuple(dict.fromkeys((*spec.release.adopt, *adopt))),
            tuple(dict.fromkeys((*spec.release.replace, *replace))),
            adopt_all_desired,
        )
        settings = spec.release
        images = self.spec.images()
        function = self.spec.load_composition()
        binding = self.provider_factory(self.spec)
        try:
            catalog = ReleaseCatalog(self.spec.catalog_path)
            self._check_target(catalog, binding.target)
            factory = self._factory(function, images, self._nodes(binding))
            composition, material = self._preview_composition(factory)
            records = {record.name: record for record in catalog.records()}
            external = self._external_sources(create=False)
            fingerprint = self._fingerprint(images, material, external=external)
            name = f"{settings.name}-{fingerprint[:12]}"
            existing = records.get(name)
            if existing is not None:
                # An unchanged release: its archived composition carries the
                # real secret versions, so bound objects can be compared.
                composition = composition_from_archive(existing.archive)
            kinds = self._kinds(composition)
            if settings.prune:
                for record in records.values():
                    kinds |= self._kinds(composition_from_archive(record.archive))
            artifact, unavailable = self._observe(binding, composition, kinds)
            snapshot = ObservedSnapshot.from_discovery(artifact)
            inherited = list(settings.inherited_owners)
            resolved = requested.resolve(
                composition, snapshot, inherited, settings.field_manager
            )
            private = None
            if existing is not None and self.spec.secret_store_path.exists():
                store = SecretVersionStore(self.spec.secret_store_path)
                try:
                    private = _private(composition, snapshot, store)
                finally:
                    store.close()
            plan = build_plan(
                composition,
                snapshot,
                _plan_authorization(
                    binding.target,
                    prune=settings.prune,
                    adopt=resolved.adopt,
                    inherited=inherited,
                    field_manager=settings.field_manager,
                    replace=resolved.replace,
                    previous=_previous_declared(catalog, sorted(records)),
                ),
                private=private,
            )
        finally:
            binding.close()
        summary = plan.summary()
        counts = _summary(summary)
        return {
            "release": settings.name,
            "summary": counts,
            "changes": any(operation != "no-op" for operation in counts),
            "actions": _compact_actions(summary),
            "diffs": plan_diffs(plan, snapshot),
            "dry_run_unavailable": unavailable,
            "autoscaled": _autoscaled(composition, snapshot, settings.field_manager),
        }

    def placeholder_preview(
        self, *, skip_dry_run: Callable[[ResourceIntent], bool]
    ) -> dict[str, Any]:
        """The structure and ownership of a release whose images do not exist yet.

        The spec's images are placeholders, so the result is evidence only:
        nothing is persisted (no plan, release, secret or discovery file), no
        secret is generated, imported or read, and the cluster receives only
        reads plus ``dryRun=All`` patches of managed objects for which
        ``skip_dry_run`` is false (objects carrying a placeholder are never
        sent). Ownership is resolved exactly as :meth:`plan` does, so an
        object that needs adoption or replacement raises the same
        :class:`ReleaseError` with its ``blocking`` list.
        """
        spec = self.spec.model
        settings = spec.release
        requested = _Ownership(
            tuple(dict.fromkeys(settings.adopt)),
            tuple(dict.fromkeys(settings.replace)),
        )
        images = self.spec.images()
        function = self.spec.load_composition()
        binding = self.provider_factory(self.spec)
        try:
            catalog = ReleaseCatalog(self.spec.catalog_path)
            self._check_target(catalog, binding.target)
            factory = self._factory(function, images, self._nodes(binding))
            composition, _material = self._preview_composition(factory)
            records = sorted(record.name for record in catalog.records())
            kinds = self._kinds(composition)
            if settings.prune:
                for record in catalog.records():
                    kinds |= self._kinds(composition_from_archive(record.archive))
            artifact = self._discover(binding, kinds, composition)
            skipped = sorted(
                {
                    (resource.ref.kind, resource.ref.name)
                    for resource, _current in probe_candidates(composition, artifact)
                    if skip_dry_run(resource)
                }
            )
            artifact, unavailable = capture_server_dry_runs(
                binding.provider,
                artifact,
                composition,
                deadline=time.monotonic() + spec.discovery.max_seconds,
                exclude=skip_dry_run,
            )
            snapshot = ObservedSnapshot.from_discovery(artifact)
            inherited = list(settings.inherited_owners)
            resolved = requested.resolve(
                composition, snapshot, inherited, settings.field_manager
            )
            plan = build_plan(
                composition,
                snapshot,
                _plan_authorization(
                    binding.target,
                    prune=settings.prune,
                    adopt=resolved.adopt,
                    inherited=inherited,
                    field_manager=settings.field_manager,
                    replace=resolved.replace,
                    previous=_previous_declared(catalog, records),
                ),
            )
        finally:
            binding.close()
        summary = plan.summary()
        return {
            "summary": _summary(summary),
            "actions": _compact_actions(summary),
            "drift": _drift(
                composition, snapshot, settings.field_manager, resolved.adopt
            ),
            "authorized": requested.report(resolved),
            "adopt_not_needed": resolved.adopt_not_needed,
            "dry_run_skipped": [
                {"kind": kind, "name": name, "reason": "dry-run-placeholder-image"}
                for kind, name in skipped
            ],
            "dry_run_unavailable": [item.to_dict() for item in unavailable],
        }

    def resolve_rollback_target(
        self, target: str, catalog: ReleaseCatalog | None = None
    ) -> str:
        catalog = catalog or ReleaseCatalog(self.spec.catalog_path)
        if target != "previous":
            try:
                catalog.get(target)
            except ValueError:
                raise ReleaseError(
                    f"unknown release {target!r}", code="unknown-release"
                ) from None
            return target
        if not self.history.deployed():
            raise ReleaseError(
                "no release has been applied yet", code="no-release-applied"
            )
        previous = self.history.previous()
        if previous is None:
            raise ReleaseError(
                "no previous release to roll back to", code="no-previous-release"
            )
        return previous

    def _plan_create(
        self,
        name: str,
        fingerprint: str,
        composition: DeploymentComposition,
        factory: Callable[[Mapping[str, SecretVersionRef]], DeploymentComposition],
        images: Mapping[str, ImageRef],
        binding: ProviderBinding,
        catalog: ReleaseCatalog,
        journal: ExecutionJournal,
        store: SecretVersionStore,
        rotate: Sequence[str],
        requested: _Ownership,
        external: Mapping[str, Fetched] | None = None,
    ) -> PlanResult:
        settings = self.spec.model.release
        kinds = self._kinds(composition)
        if settings.prune:
            for record in catalog.records():
                kinds |= self._kinds(composition_from_archive(record.archive))
        artifact, unavailable = self._observe(binding, composition, kinds)
        snapshot = ObservedSnapshot.from_discovery(artifact)
        inherited = list(settings.inherited_owners)
        resolved = requested.resolve(
            composition, snapshot, inherited, settings.field_manager
        )
        adopt, replace = resolved.adopt, resolved.replace
        previous_releases = sorted(record.name for record in catalog.records())
        plan_authorization = _plan_authorization(
            binding.target,
            prune=settings.prune,
            adopt=adopt,
            inherited=inherited,
            field_manager=settings.field_manager,
            replace=replace,
            previous=_previous_declared(catalog, previous_releases),
        )
        window = settings.approval_window_seconds
        expires_at = (_now() + timedelta(seconds=window)).isoformat()
        authorization_id = uuid.uuid4().hex

        def grant(
            plan: DeploymentPlan, captured: ObservedSnapshot
        ) -> ExecutionAuthorization:
            return _grant(
                plan,
                captured,
                authorization_id=authorization_id,
                expires_at=expires_at,
                max_age=window,
                field_manager=settings.field_manager,
                owner_id=settings.owner,
                inherited_owner_ids=inherited,
            )

        # Validate the plan and its grant with the placeholder composition
        # first: a refused plan must not generate, import or store any secret.
        preview = build_plan(composition, snapshot, plan_authorization)
        grant(preview, snapshot)
        secrets = self._private_inputs(catalog, store, binding, rotate, external)
        origin = secrets.origin
        placeholders = self._placeholders()
        bound_refs = {
            item.reference
            for component in composition.components
            for resource in component.resources
            for item in resource.secret_bindings
        }
        bound = {n for n, ref in placeholders.items() if ref in bound_refs}
        values = {n: v for n, v in secrets.values.items() if n in bound}
        session_id = uuid.uuid4().hex
        internal: dict[str, SecretVersionRef] = {}
        try:
            for item, value in sorted(secrets.values.items()):
                if item not in bound:
                    internal[item] = store.put(binding.target, value)

            def session_factory(
                refs: Mapping[str, SecretVersionRef],
            ) -> DeploymentComposition:
                exposed = {n: r for n, r in internal.items() if n in placeholders}
                return factory({**exposed, **refs})

            workflow = ReleaseWorkflow(
                catalog,
                binding.target.namespace,
                session_factory,
                snapshot,
                plan_authorization,
                grant,
                journal,
                store,
            )
            # Discovery is persisted before the session so an interrupted plan
            # can never leave a catalogued release without its evidence.
            _write_private(self._discovery_path(name), artifact.to_private_json())
            source = self._source(images)
            try:
                previously_selected: str | None = catalog.selected().name
            except ValueError:
                previously_selected = None
            record = workflow.create(
                name=name,
                source=source,
                private_inputs=values,
                session_id=session_id,
                execution_id=uuid.uuid4().hex,
            )
        except BaseException:
            # The release was not recorded: its versions would be orphans.
            store.discard(binding.target, internal.values(), session_id=session_id)
            raise
        if previously_selected is not None:
            # ``create`` selects the new record; selection should keep meaning
            # "last release that became ready", so restore it until apply.
            catalog.select(previously_selected)
        _write_private(
            self._sidecar_path(name),
            _canonical(
                {
                    "created_at": _now().isoformat(),
                    "fingerprint": fingerprint,
                    "images": {n: i.to_dict() for n, i in images.items()},
                    "secrets": {
                        n: config_digest(g) for n, g in self.spec.model.secrets.items()
                    },
                    # Private bookkeeping: opaque versions of unbound outputs,
                    # carry-over digests, and public generator metadata.
                    "secret_refs": {n: r.version for n, r in internal.items()},
                    "secret_parts": secrets.parts,
                    "secret_meta": secrets.meta,
                    "secret_origin": origin,
                    "prune": settings.prune,
                    # The plan authorization, so the session reopens exactly.
                    "adopt": adopt,
                    "inherited_owners": inherited,
                    "replace": replace,
                    # Earlier releases whose declarations the plan's field
                    # removals are computed from (records are immutable).
                    "previous_releases": previous_releases,
                    # A pipeline's sources (commit, dirty, --ref); not hashed.
                    **(
                        {"provenance": dict(provenance)}
                        if (provenance := getattr(self.spec, "provenance", None))
                        else {}
                    ),
                }
            )
            + "\n",
        )
        plan = record.archive.to_dict()["revision"]["desired_state"]
        # The stored plan (real secret versions, so bound objects are compared
        # privately); the placeholder preview only validated the grant.
        stored = composition_from_archive(record.archive)
        planned = build_plan(
            stored,
            snapshot,
            plan_authorization,
            private=_private(stored, snapshot, store),
        )
        result = PlanResult(
            name,
            "create",
            "apply",
            plan,
            source.to_dict(),
            {n: i.to_dict() for n, i in images.items()},
            origin,
            expires_at,
            _drift(composition, snapshot, settings.field_manager, adopt),
            resolved.adopt_not_needed,
            requested.report(resolved),
            self._checks_policy(),
            plan_diffs(planned, snapshot),
            unavailable,
            _autoscaled(stored, snapshot, settings.field_manager),
        )
        self._persist_plan(result, prune=settings.prune)
        return result

    def _plan_reapply(
        self,
        name: str,
        intent: str,
        binding: ProviderBinding,
        catalog: ReleaseCatalog,
        store: SecretVersionStore,
        requested: _Ownership,
    ) -> PlanResult:
        settings = self.spec.model.release
        record = catalog.get(name)
        composition = composition_from_archive(record.archive)
        kinds = self._kinds(composition)
        if settings.prune:
            for other in catalog.records():
                kinds |= self._kinds(composition_from_archive(other.archive))
        artifact, unavailable = self._observe(binding, composition, kinds)
        snapshot = ObservedSnapshot.from_discovery(artifact)
        inherited = list(settings.inherited_owners)
        resolved = requested.resolve(
            composition, snapshot, inherited, settings.field_manager
        )
        adopt, replace = resolved.adopt, resolved.replace
        previous_releases = sorted(other.name for other in catalog.records())
        plan = build_plan(
            composition,
            snapshot,
            _plan_authorization(
                binding.target,
                prune=settings.prune,
                adopt=adopt,
                inherited=inherited,
                field_manager=settings.field_manager,
                replace=replace,
                previous=_previous_declared(catalog, previous_releases),
            ),
            private=_private(composition, snapshot, store),
        )
        expires_at = (
            _now() + timedelta(seconds=settings.approval_window_seconds)
        ).isoformat()
        sidecar = self._sidecar(name)
        result = PlanResult(
            name,
            "reapply",
            intent,
            plan.summary(),
            record.source.to_dict(),
            sidecar.get("images", {}),
            {},
            expires_at,
            _drift(composition, snapshot, settings.field_manager, adopt),
            resolved.adopt_not_needed,
            requested.report(resolved),
            self._checks_policy(),
            plan_diffs(plan, snapshot),
            unavailable,
            _autoscaled(composition, snapshot, settings.field_manager),
        )
        self._persist_plan(
            result,
            prune=settings.prune,
            discovery=artifact.to_private_json(),
            adopt=adopt,
            inherited=inherited,
            replace=replace,
            previous_releases=previous_releases,
        )
        return result

    def _persist_plan(
        self,
        result: PlanResult,
        *,
        prune: bool,
        discovery: str | None = None,
        adopt: Sequence[Mapping[str, str]] = (),
        inherited: Sequence[str] = (),
        replace: Sequence[Mapping[str, str]] = (),
        previous_releases: Sequence[str] = (),
    ) -> None:
        _write_private(
            self._plan_path(result.plan_hash),
            _canonical(
                {
                    "schema_version": 1,
                    "release": result.release,
                    "mode": result.mode,
                    "intent": result.intent,
                    "prune": prune,
                    "expires_at": result.expires_at,
                    "discovery": discovery,
                    "adopt": list(adopt),
                    "inherited_owners": list(inherited),
                    "replace": list(replace),
                    "previous_releases": list(previous_releases),
                    # What apply checks is what was reviewed, not a later spec.
                    "checks": [check.public_dict() for check in self.spec.model.checks],
                    "rollback_on_failed_checks": (
                        self.spec.model.release.rollback_on_failed_checks
                    ),
                }
            )
            + "\n",
        )

    def _checks_policy(self) -> dict[str, Any]:
        return {
            "names": [check.label for check in self.spec.model.checks],
            "rollback_on_failed_checks": (
                self.spec.model.release.rollback_on_failed_checks
            ),
        }

    def pending_plan(self, plan_hash: str) -> dict[str, Any]:
        path = self._plan_path(plan_hash)
        if not path.exists():
            raise ReleaseError(
                "no pending plan with this hash (unknown, expired or already "
                "applied); run `piceli release plan` again",
                code="plan-not-found",
            )
        value = json.loads(path.read_text())
        if timestamp(value["expires_at"], allow_future=True) <= _now():
            path.unlink(missing_ok=True)
            raise ReleaseError(
                "the approved plan expired; run plan again", code="plan-expired"
            )
        return dict(value)

    # ---------------------------------------------------------------- apply
    def _limits(self) -> ExecutionLimits:
        execution = self.spec.model.execution
        return ExecutionLimits(
            max_actions=execution.max_actions,
            max_seconds=execution.max_seconds,
            readiness_seconds=min(execution.readiness_seconds, execution.max_seconds),
            poll_seconds=min(execution.poll_seconds, execution.readiness_seconds),
            max_polls=execution.max_polls,
            write_settle_seconds=execution.write_settle_seconds,
        )

    def _session_workflow(
        self,
        record: ReleaseRecord,
        catalog: ReleaseCatalog,
        journal: ExecutionJournal,
        store: SecretVersionStore,
        target: PlanTarget,
    ) -> ReleaseWorkflow:
        """Reopen a created release exactly as it was planned.

        The composition is rebuilt from the archive (not from current code) and
        the grant from the archived parameters, so reopen reproduces the stored
        revision or fails.
        """
        path = self._discovery_path(record.name)
        if not path.exists():
            raise ReleaseError(
                f"release {record.name!r} has no stored discovery; re-plan it",
                code="stored-discovery-missing",
            )
        snapshot = ObservedSnapshot.from_discovery(
            DiscoveryArtifact.from_private_json(path.read_text())
        )
        archived = record.archive.to_dict()["revision"]["authorization"]
        settings = self.spec.model.release
        if (
            archived["owner_id"] != settings.owner
            or archived["field_manager"] != settings.field_manager
        ):
            raise ReleaseError(
                f"release {record.name!r} was planned for another owner/field manager",
                code="release-owner-mismatch",
            )
        sidecar = self._sidecar(record.name)
        prune = bool(sidecar.get("prune", False))

        def grant(
            plan: DeploymentPlan, captured: ObservedSnapshot
        ) -> ExecutionAuthorization:
            return _grant(
                plan,
                captured,
                authorization_id=archived["authorization_id"],
                expires_at=archived["expires_at"],
                max_age=archived["max_evidence_age_seconds"],
                field_manager=archived["field_manager"],
                owner_id=archived["owner_id"],
                inherited_owner_ids=archived.get("inherited_owner_ids", ()),
            )

        return ReleaseWorkflow(
            catalog,
            target.namespace,
            lambda _refs: composition_from_archive(record.archive),
            snapshot,
            _plan_authorization(
                target,
                prune=prune,
                adopt=sidecar.get("adopt", ()),
                inherited=sidecar.get("inherited_owners", ()),
                field_manager=archived["field_manager"],
                replace=sidecar.get("replace", ()),
                previous=_previous_declared(
                    catalog, sidecar.get("previous_releases", ())
                ),
            ),
            grant,
            journal,
            store,
        )

    def apply(
        self,
        plan_hash: str,
        *,
        expected_intent: str | None = None,
        expected_release: str | None = None,
        skip_checks: bool = False,
        trigger: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute the persisted plan ``plan_hash`` (the approval), then check it.

        When the execution is ready, the plan's ``[[checks]]`` run; the
        release is ``ready`` (and selected) only when they pass, otherwise
        ``checks-failed``. With ``rollback_on_failed_checks`` the last other
        ready release is re-planned and re-applied automatically (see
        :meth:`_auto_rollback`). ``skip_checks`` skips them and is recorded.
        ``trigger`` marks an automatic rollback (internal; never rolls back).
        """
        pending = self.pending_plan(plan_hash)
        if expected_intent is not None and pending["intent"] != expected_intent:
            raise ReleaseError(
                f"plan {plan_hash[:12]} is a {pending['intent']} plan, "
                f"not a {expected_intent} plan",
                code="plan-intent-mismatch",
            )
        if expected_release is not None and pending["release"] != expected_release:
            raise ReleaseError(
                f"plan {plan_hash[:12]} targets {pending['release']!r}, "
                f"not {expected_release!r}",
                code="plan-release-mismatch",
            )
        name = pending["release"]
        checks = parse_checks(pending.get("checks", ()))
        binding = self.provider_factory(self.spec)
        try:
            catalog, journal, store = self._open()
            try:
                self._check_target(catalog, binding.target)
                record = catalog.get(name)
                try:
                    before: str | None = catalog.selected().name
                except ValueError:
                    before = None
                executor = PlanExecutor(
                    binding.provider,
                    journal,
                    store,
                    limits=self._limits(),
                    backups=self.backups,
                    progress=self.progress,
                )
                if pending["mode"] == "create":
                    workflow = self._session_workflow(
                        record, catalog, journal, store, binding.target
                    )
                    session = workflow.reopen(name)
                    if session.revision.plan.plan_hash != plan_hash:
                        raise ReleaseError(
                            "stored release does not match the plan",
                            code="stored-release-mismatch",
                        )
                    execution_id = session.bundle.execution_id

                    def run() -> dict[str, Any]:
                        return workflow.apply(executor, name)
                else:
                    snapshot = ObservedSnapshot.from_discovery(
                        DiscoveryArtifact.from_private_json(pending["discovery"])
                    )
                    plan_authorization = _plan_authorization(
                        binding.target,
                        prune=bool(pending["prune"]),
                        adopt=pending.get("adopt", ()),
                        inherited=pending.get("inherited_owners", ()),
                        field_manager=self.spec.model.release.field_manager,
                        replace=pending.get("replace", ()),
                        previous=_previous_declared(
                            catalog, pending.get("previous_releases", ())
                        ),
                    )
                    composition = composition_from_archive(record.archive)
                    if (
                        build_plan(
                            composition,
                            snapshot,
                            plan_authorization,
                            private=_private(composition, snapshot, store),
                        ).plan_hash
                        != plan_hash
                    ):
                        raise ReleaseError(
                            "stored evidence does not match the plan",
                            code="stored-evidence-mismatch",
                        )
                    settings = self.spec.model.release
                    authorization_id = uuid.uuid4().hex
                    expires_at = pending["expires_at"]

                    def grant(
                        plan: DeploymentPlan, captured: ObservedSnapshot
                    ) -> ExecutionAuthorization:
                        return _grant(
                            plan,
                            captured,
                            authorization_id=authorization_id,
                            expires_at=expires_at,
                            max_age=settings.approval_window_seconds,
                            field_manager=settings.field_manager,
                            owner_id=settings.owner,
                            inherited_owner_ids=plan_authorization.inherited_owner_ids,
                        )

                    workflow = ReleaseWorkflow(
                        catalog,
                        binding.target.namespace,
                        lambda _refs: composition,
                        snapshot,
                        plan_authorization,
                        grant,
                        journal,
                        store,
                    )
                    execution_id = uuid.uuid4().hex

                    def run() -> dict[str, Any]:
                        return workflow.rollback(
                            executor, name, execution_id=execution_id
                        )

                self.history.append(
                    {
                        "at": _now().isoformat(),
                        "release": name,
                        "intent": pending["intent"],
                        "mode": pending["mode"],
                        "plan_hash": plan_hash,
                        "execution_id": execution_id,
                        "state": "running",
                        **({"skip_checks": True} if skip_checks else {}),
                        **dict(trigger or {}),
                    }
                )
                try:
                    result = run()
                except ValueError as error:
                    self.history.update(
                        execution_id, state="rejected", reason="execution-refused"
                    )
                    raise ReleaseError(
                        f"execution refused: {error}", code="execution-refused"
                    ) from None
                self._plan_path(plan_hash).unlink(missing_ok=True)
                state, report = self._verify(
                    name, execution_id, result, checks, skip_checks=skip_checks
                )
                self.history.update(
                    execution_id, state=state, checks=_check_summary(report)
                )
                if state == "ready" and pending["mode"] == "create":
                    catalog.select(name)
                elif state == "checks-failed" and before is not None:
                    # A re-apply selects its release when ready; a release
                    # whose checks failed must not stay selected.
                    catalog.select(before)
                outcome = {
                    "release": name,
                    "intent": pending["intent"],
                    "mode": pending["mode"],
                    "plan_hash": plan_hash,
                    "source": record.source.to_dict(),
                    "execution": _execution_summary(result),
                    "release_state": state,
                    "checks": report,
                    "adopted": _adopted(journal, execution_id, result),
                    "selected": self._selected(catalog),
                    **({"trigger": dict(trigger)} if trigger else {}),
                }
            finally:
                journal.close()
                store.close()
        finally:
            binding.close()
        if (
            state == "checks-failed"
            and pending.get("rollback_on_failed_checks")
            and trigger is None
        ):
            outcome["rollback"] = self._auto_rollback(name, execution_id)
        return outcome

    @staticmethod
    def _selected(catalog: ReleaseCatalog) -> str | None:
        try:
            return catalog.selected().name
        except ValueError:
            return None

    # --------------------------------------------------------------- checks
    def _verify(
        self,
        name: str,
        execution_id: str,
        result: Mapping[str, Any],
        checks: Sequence[Check],
        *,
        skip_checks: bool,
    ) -> tuple[str, dict[str, Any] | None]:
        """The release state after an execution: run the checks when it is ready.

        Returns ``(state, check report)``: the execution state when it did not
        become ready or has no checks, else ``ready`` or ``checks-failed``.
        The full report is also kept in ``state_dir/checks/<execution>.json``.
        """
        state = str(result.get("state"))
        if state != "ready" or not checks:
            return state, None
        if skip_checks:
            return state, {
                "skipped": True,
                "flag": "--skip-checks",
                "declared": [check.label for check in checks],
            }
        images = self._sidecar(name).get("images", {})
        with self.check_context_factory(self.spec, name, images) as context:
            report: CheckReport = run_checks(checks, context)
        value = report.to_dict()
        _write_private(
            self.state / "checks" / f"{execution_id}.json",
            _canonical(
                {
                    "schema_version": 1,
                    "release": name,
                    "execution_id": execution_id,
                    "at": _now().isoformat(),
                    **value,
                }
            )
            + "\n",
        )
        return ("ready" if report.passed else "checks-failed"), value

    def _auto_rollback(self, failed: str, execution_id: str) -> dict[str, Any]:
        """Re-plan and re-apply the last other ready release, without approval.

        ``[release] rollback_on_failed_checks = true`` (bound into the plan
        that failed its checks) is the standing approval. The rollback is an
        ordinary journaled re-apply execution whose history entry carries
        ``trigger = "checks-failed"``; it runs its own checks but never
        triggers another rollback. The failed execution's history entry
        records the outcome under ``rollback``.
        """
        target = next(
            (item for item in reversed(self.history.deployed()) if item != failed),
            None,
        )
        value: dict[str, Any]
        if target is None:
            value = {
                "state": "unavailable",
                "reason": "checks-rollback-unavailable",
                "detail": "no earlier ready release to roll back to",
            }
            self.history.update(execution_id, rollback=value)
            return value
        trigger = {
            "trigger": "checks-failed",
            "rolled_back_from": failed,
            "failed_execution_id": execution_id,
        }
        try:
            planned = self.plan(rollback_to=target)
            outcome = self.apply(
                planned.plan_hash,
                expected_intent="rollback",
                expected_release=target,
                trigger=trigger,
            )
        except (ValueError, OSError, ProviderError) as error:
            value = {
                "state": "failed",
                "reason": "checks-rollback-failed",
                "target": target,
                "detail": str(error),
            }
        else:
            done = outcome["release_state"] == "ready"
            value = {
                "state": "rolled-back" if done else "failed",
                "target": target,
                "plan_hash": planned.plan_hash,
                "execution": outcome["execution"],
                "release_state": outcome["release_state"],
                "checks": outcome["checks"],
                "selected": outcome["selected"],
                **({} if done else {"reason": "checks-rollback-failed"}),
            }
        self.history.update(
            execution_id,
            rollback={
                key: value[key] for key in ("state", "target", "reason") if key in value
            }
            | (
                {"execution_id": value["execution"]["execution_id"]}
                if "execution" in value
                else {}
            ),
        )
        return value

    def check(self, release: str | None = None) -> dict[str, Any]:
        """Run the spec's checks now against a release (default: the selected one).

        Changes nothing: no state is written and no rollback is triggered.
        """
        checks = self.spec.model.checks
        catalog = ReleaseCatalog(self.spec.catalog_path)
        if release is None:
            try:
                release = catalog.selected().name
            except ValueError:
                raise ReleaseError("no release is selected yet") from None
        else:
            try:
                catalog.get(release)
            except ValueError:
                raise ReleaseError(f"unknown release {release!r}") from None
        images = self._sidecar(release).get("images", {})
        with self.check_context_factory(self.spec, release, images) as context:
            report = run_checks(checks, context)
        return {"release": release, "intent": "check", "checks": report.to_dict()}

    # --------------------------------------------------------- resume/stop
    def _latest(self, release: str | None) -> dict[str, Any]:
        entries = [
            entry
            for entry in self.history.entries()
            if entry.get("intent") in {"apply", "rollback", "resume"}
            and (release is None or entry["release"] == release)
        ]
        if not entries:
            raise ReleaseError(
                "no execution recorded for this release", code="no-execution-recorded"
            )
        return entries[-1]

    def resume(
        self, release: str | None = None, *, skip_checks: bool = False
    ) -> dict[str, Any]:
        """Resume the created release's session execution (same grant, same ids).

        A resumed execution that becomes ready runs the spec's checks, with the
        same outcome and automatic rollback as :meth:`apply`.
        """
        entry = self._latest(release)
        if entry["mode"] != "create":
            raise ReleaseError(
                "re-apply and rollback executions are not resumable; "
                "run plan/apply (or rollback) again",
                code="not-resumable",
            )
        name = entry["release"]
        binding = self.provider_factory(self.spec)
        try:
            catalog, journal, store = self._open()
            try:
                self._check_target(catalog, binding.target)
                workflow = self._session_workflow(
                    catalog.get(name), catalog, journal, store, binding.target
                )
                executor = PlanExecutor(
                    binding.provider,
                    journal,
                    store,
                    limits=self._limits(),
                    backups=self.backups,
                    progress=self.progress,
                )
                try:
                    result = workflow.resume(executor, name)
                except ValueError as error:
                    raise ReleaseError(
                        f"resume refused: {error}", code="resume-refused"
                    ) from None
                state, report = self._verify(
                    name,
                    entry["execution_id"],
                    result,
                    self.spec.model.checks,
                    skip_checks=skip_checks,
                )
                # Recorded after the run: a resume keeps the execution id, so a
                # concurrent ``stop`` still finds it through the earlier entry.
                self.history.append(
                    {
                        key: value
                        for key, value in entry.items()
                        if key not in {"checks", "rollback", "skip_checks"}
                    }
                    | {
                        "at": _now().isoformat(),
                        "intent": "resume",
                        "state": state,
                        "checks": _check_summary(report),
                        **({"skip_checks": True} if skip_checks else {}),
                    }
                )
                if state == "ready":
                    catalog.select(name)
                outcome = {
                    "release": name,
                    "intent": "resume",
                    "execution": _execution_summary(result),
                    "release_state": state,
                    "checks": report,
                }
            finally:
                journal.close()
                store.close()
        finally:
            binding.close()
        if (
            state == "checks-failed"
            and self.spec.model.release.rollback_on_failed_checks
            and "trigger" not in entry
        ):
            outcome["rollback"] = self._auto_rollback(name, entry["execution_id"])
        return outcome

    def stop(self, release: str | None = None) -> dict[str, Any]:
        """Cancel the latest execution of a release, owner-checked."""
        entry = self._latest(release)
        name = entry["release"]
        binding = self.provider_factory(self.spec)
        try:
            catalog, journal, store = self._open()
            try:
                self._check_target(catalog, binding.target)
                executor = PlanExecutor(binding.provider, journal, store)
                try:
                    current = journal.summary(entry["execution_id"])
                except ValueError:
                    raise ReleaseError(
                        "the execution has not started", code="execution-not-started"
                    ) from None
                if current["state"] in {"ready", "cancelled"}:
                    raise ReleaseError(
                        f"the latest execution of {name!r} is already "
                        f"{current['state']}; nothing to stop",
                        code="nothing-to-stop",
                    )
                if entry["mode"] == "create":
                    workflow = self._session_workflow(
                        catalog.get(name), catalog, journal, store, binding.target
                    )
                    result = workflow.stop(executor, name)
                else:
                    execution = journal.export_execution(entry["execution_id"])
                    authorization = execution["binding"]["revision"]["authorization"]
                    if authorization["owner_id"] != binding.provider.owner_id:
                        raise ReleaseError(
                            "execution belongs to another owner",
                            code="execution-other-owner",
                        )
                    if authorization["target"] != binding.target.__dict__:
                        raise ReleaseError(
                            "execution belongs to another target",
                            code="execution-other-target",
                        )
                    result = executor.cancel(entry["execution_id"])
                self.history.update(entry["execution_id"], state=result.get("state"))
                return {
                    "release": name,
                    "intent": "stop",
                    "execution": _execution_summary(result),
                }
            finally:
                journal.close()
                store.close()
        finally:
            binding.close()

    # --------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        """Catalog, journal and history view; never contacts the cluster."""
        if not self.state.exists():
            return {"namespace": self.spec.model.target.namespace, "releases": []}
        catalog = ReleaseCatalog(self.spec.catalog_path)
        journal = (
            ExecutionJournal(self.spec.journal_path)
            if self.spec.journal_path.exists()
            else None
        )
        try:
            entries = self.history.entries()
            deployed = self.history.deployed()
            try:
                selected: str | None = catalog.selected().name
            except ValueError:
                selected = None
            releases = []
            for record in catalog.records():
                sidecar = self._sidecar(record.name)
                session = record.archive.report()
                executions = []
                ids = [session["execution_id"]] + [
                    entry["execution_id"]
                    for entry in entries
                    if entry["release"] == record.name
                    and entry["execution_id"] != session["execution_id"]
                ]
                for execution_id in dict.fromkeys(ids):
                    try:
                        summary = (
                            _execution_summary(journal.summary(execution_id))
                            if journal is not None
                            else None
                        )
                    except ValueError:
                        summary = None
                    executions.append(
                        summary
                        or {"execution_id": execution_id, "state": "not-started"}
                    )
                checked = [
                    entry
                    for entry in entries
                    if entry["release"] == record.name and entry.get("checks")
                ]
                releases.append(
                    {
                        "name": record.name,
                        "release_id": record.release_id,
                        "created_at": sidecar.get("created_at"),
                        "source": record.source.to_dict(),
                        "images": sidecar.get("images", {}),
                        **(
                            {"provenance": sidecar["provenance"]}
                            if "provenance" in sidecar
                            else {}
                        ),
                        "revision_id": session["revision_id"],
                        "action_count": session["action_count"],
                        "executions": executions,
                        # The latest check outcome: passed/failed names, or
                        # skipped; full reports are in state_dir/checks/.
                        "checks": (
                            checked[-1]["checks"]
                            | {
                                "execution_id": checked[-1]["execution_id"],
                                "state": checked[-1]["state"],
                            }
                            if checked
                            else None
                        ),
                    }
                )
            releases.sort(key=lambda item: item["created_at"] or "")
            pending = []
            plans = self.state / "plans"
            if plans.is_dir():
                for path in sorted(plans.glob("*.json")):
                    value = json.loads(path.read_text())
                    pending.append(
                        {
                            "plan_hash": path.stem,
                            "release": value["release"],
                            "intent": value["intent"],
                            "expires_at": value["expires_at"],
                        }
                    )
            return {
                "namespace": self.spec.model.target.namespace,
                "checks": self._checks_policy(),
                "selected": selected,
                "deployed": deployed[-1] if deployed else None,
                "previous": self.history.previous(),
                "latest": entries[-1] if entries else None,
                "releases": releases,
                "pending_plans": pending,
                "history": entries[-20:],
            }
        finally:
            if journal is not None:
                journal.close()

    # -------------------------------------------------------------- secrets
    def _secret_record(
        self, catalog: ReleaseCatalog, name: str, release: str | None
    ) -> tuple[ReleaseRecord, dict[str, Any], dict[str, Any]]:
        """The release holding ``name`` (``release``, else selected, else newest)."""
        if release is not None:
            try:
                record = catalog.get(release)
            except ValueError:
                raise ReleaseError(
                    f"unknown release {release!r}", code="unknown-release"
                ) from None
        else:
            try:
                record = catalog.selected()
            except ValueError:
                records = sorted(
                    catalog.records(),
                    key=lambda item: self._sidecar(item.name).get("created_at", ""),
                )
                if not records:
                    raise SecretError(
                        "secret-not-found", "no release has materialized secrets yet"
                    ) from None
                record = records[-1]
        sidecar = self._sidecar(record.name)
        meta = sidecar.get("secret_meta", {}).get(name)
        if meta is None and name in sidecar.get("secrets", {}):
            generator = self.spec.model.secrets.get(name)
            if generator is not None:  # a release made before metadata was kept
                meta = describe(name, generator)
        if meta is None:
            raise SecretError(
                "secret-not-found",
                f"release {record.name!r} has no secret {name!r}; declared: "
                f"{sorted(sidecar.get('secrets', {}))}",
            )
        return record, sidecar, meta

    def _secret_first(self, name: str, release: str) -> tuple[str, str | None]:
        """Follow carry-over back to the release that created the value."""
        seen: set[str] = set()
        while release not in seen:
            seen.add(release)
            origin = self._sidecar(release).get("secret_origin", {}).get(name)
            if not isinstance(origin, str) or not origin.startswith("carried:"):
                return release, origin
            release = origin.split(":", 1)[1]
        return release, None

    def secret_metadata(
        self, name: str, *, release: str | None = None
    ) -> dict[str, Any]:
        """Public facts about one generator's value; never the value itself.

        Reads only the local state directory; never contacts the cluster.
        """
        if not self.state.exists():
            raise SecretError("secret-not-found", "no release state exists yet")
        catalog = ReleaseCatalog(self.spec.catalog_path)
        record, sidecar, meta = self._secret_record(catalog, name, release)
        first, first_origin = self._secret_first(name, record.name)
        return {
            "secret": name,
            "release": record.name,
            "type": meta["type"],
            "encoding": meta["encoding"],
            "keys": sorted(key for key in meta["outputs"] if key),
            "internal": meta.get("internal", []),
            "origin": sidecar.get("secret_origin", {}).get(name),
            "first_release": first,
            "first_origin": first_origin,
            **{
                key: meta[key]
                for key in ("source", "rotate", "depends_on")
                if key in meta
            },
        }

    def reveal_secret(
        self, name: str, *, key: str | None = None, release: str | None = None
    ) -> dict[str, bytes]:
        """Decoded values of one generator's outputs (``key`` selects one).

        Only for the owner's explicit reveal; the caller must never log them.
        """
        if not self.state.exists():
            raise SecretError("secret-not-found", "no release state exists yet")
        catalog = ReleaseCatalog(self.spec.catalog_path)
        record, sidecar, meta = self._secret_record(catalog, name, release)
        wanted: dict[str, str] = dict(meta["outputs"])
        if key is not None:
            selected = {k: v for k, v in wanted.items() if key in {k, v}}
            if not selected:
                raise SecretError(
                    "secret-not-found",
                    f"secret {name!r} has no key {key!r}; keys: "
                    f"{sorted(k for k in wanted if k)}",
                )
            wanted = selected
        target = PlanTarget(
            **record.archive.to_dict()["revision"]["desired_state"]["target"]
        )
        store = SecretVersionStore(self.spec.secret_store_path)
        try:
            refs = self._stored_refs(record, sidecar, store)
            missing = sorted(output for output in wanted.values() if output not in refs)
            if missing:
                raise SecretError(
                    "secret-not-found",
                    f"release {record.name!r} stored no value for {missing}",
                )
            return {
                (k or name): decode(
                    str(store.resolve(target, refs[output])), meta["encoding"]
                )
                for k, output in sorted(wanted.items())
            }
        finally:
            store.close()
