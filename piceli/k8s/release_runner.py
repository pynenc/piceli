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
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from piceli.k8s.ops.bounds import timestamp
from piceli.k8s.ops.discovery import (
    RETAINED_KINDS,
    DiscoveryArtifact,
    DiscoveryLimits,
    DiscoveryRequest,
    PlanTarget,
    ResourceType,
    capture_discovery,
)
from piceli.k8s.ops.execution_journal import ExecutionJournal
from piceli.k8s.ops.executor import (
    ActionGrant,
    ExecutionAuthorization,
    ExecutionLimits,
    PlanExecutor,
)
from piceli.k8s.ops.plan import (
    DeploymentComposition,
    DeploymentPlan,
    ObservedSnapshot,
    Ownership,
    PlanAuthorization,
    ResourceIntent,
    ResourceRef,
    build_plan,
    field_drift,
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
from piceli.k8s.release_secrets import config_digest, generate, input_names
from piceli.k8s.release_spec import (
    ImageRef,
    NodeRef,
    ReleaseSpec,
    ReleaseSpecError,
    parse_adopt_entry,
)

POLICY_REVISION = "piceli.release-cli/v1"
_HASH = re.compile(r"[0-9a-f]{64}")
ProviderFactory = Callable[[ReleaseSpec], ProviderBinding]


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
) -> PlanAuthorization:
    return PlanAuthorization(
        target,
        tuple(ResourceRef(**item) for item in adopt),
        prune,
        tuple(inherited),
        field_manager,
        tuple(ResourceRef(**item) for item in replace),
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
    that no other object owns; absent objects are reported as not needed and
    every other case is refused. ``adopt_all_desired`` adopts every unmanaged
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
        if current is None:
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
                    "reason": refusal,
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
                    "reason": "exists and is not managed by this release's owner"
                    + ("; retained: replace is never allowed" if retained else ""),
                    "suggest": [f"--adopt {_label(ref)}"]
                    + ([] if retained else [f"--replace {_label(ref)}"]),
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
                    "reason": "retained object: its spec/data differ from the "
                    "composition, and only labels and annotations may change",
                    "suggest": [
                        "change the composition to match the live object",
                    ],
                }
            )
    if blocking:
        blocking.sort(key=lambda item: (item["kind"], item["name"]))
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
                + "; authorize them with --adopt or --replace Kind/name "
                "(repeatable), [release] adopt/replace or --adopt-all-desired, "
                "or delete them"
            )
        for item in blocking:
            if item["code"] != "resource-requires-adoption":
                parts.append(f"{item['kind']}/{item['name']}: {item['reason']}")
        raise ReleaseError(
            "; ".join(parts),
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
            raise ReleaseError("release history is malformed")
        entries = value.get("entries")
        if not isinstance(entries, list):
            raise ReleaseError("release history is malformed")
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
    ) -> None:
        self.spec = spec
        self.provider_factory = provider_factory
        self.state = spec.state_dir
        self.history = _History(self.state / "history.json")
        # Restorable copies of objects deleted by ``replace`` (owner-only).
        self.backups = self.state / "backups"

    # ------------------------------------------------------------------ state
    def _open(self) -> tuple[ReleaseCatalog, ExecutionJournal, SecretVersionStore]:
        private_directory(self.state)
        return (
            ReleaseCatalog(self.spec.catalog_path),
            ExecutionJournal(self.spec.journal_path),
            SecretVersionStore(self.spec.secret_store_path),
        )

    def _sidecar_path(self, name: str) -> Path:
        return self.state / "releases" / f"{name}.json"

    def _discovery_path(self, name: str) -> Path:
        return self.state / "releases" / f"{name}.discovery.json"

    def _plan_path(self, plan_hash: str) -> Path:
        if not _HASH.fullmatch(plan_hash):
            raise ReleaseError("plan hash must be 64 lowercase hex characters")
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
                    f"directory (release {record.name!r}); refusing to continue"
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
            composition = function(self.spec.context(images, refs, nodes))
            if not isinstance(composition, DeploymentComposition):
                raise ReleaseSpecError(
                    "the composition function must return a DeploymentComposition"
                )
            for component in composition.components:
                for resource in component.resources:
                    if not resource.ref.namespace:
                        raise ReleaseSpecError(
                            "cluster-scoped resources are not supported by "
                            f"releases: {resource.ref.kind}/{resource.ref.name}"
                        )
                    if resource.ref.namespace != self.spec.model.target.namespace:
                        raise ReleaseSpecError(
                            f"{resource.ref.kind}/{resource.ref.name} targets "
                            f"namespace {resource.ref.namespace!r}"
                        )
            return composition

        return factory

    def _preview_composition(
        self,
        factory: Callable[[Mapping[str, SecretVersionRef]], DeploymentComposition],
    ) -> tuple[DeploymentComposition, list[dict[str, Any]]]:
        """Build with placeholder references to name the release and check bindings."""
        names = [item for group in self._declared_inputs().values() for item in group]
        placeholders = {
            name: SecretVersionRef(
                "0" * 32, hashlib.sha256(name.encode()).hexdigest()[:32]
            )
            for name in names
        }
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
                            "declared secret input"
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
        unused = sorted(set(names) - bound)
        if unused:
            raise ReleaseSpecError(
                f"declared secret inputs are not bound by the composition: {unused}"
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
        else:
            identity = (
                "sha256:"
                + hashlib.sha256(
                    _canonical(
                        {name: image.identity for name, image in images.items()}
                    ).encode()
                ).hexdigest()
            )
        return ReleaseSource("oci", identity, artifact_digest=identity)

    # ------------------------------------------------------------ discovery
    def _discover(
        self, binding: ProviderBinding, kinds: set[ResourceType]
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
                + (f" ({'; '.join(failures)})" if failures else "")
            )
        return artifact

    # -------------------------------------------------------------- secrets
    def _private_inputs(
        self,
        catalog: ReleaseCatalog,
        store: SecretVersionStore,
        target: PlanTarget,
        rotate: Sequence[str],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Carry values over from earlier releases unless rotated or reconfigured."""
        unknown = sorted(set(rotate) - set(self.spec.model.secrets))
        if unknown:
            raise ReleaseError(f"cannot rotate undeclared secrets: {unknown}")
        records = sorted(
            catalog.records(),
            key=lambda record: self._sidecar(record.name).get("created_at", ""),
            reverse=True,
        )
        values: dict[str, str] = {}
        origin: dict[str, str] = {}
        for name, generator in sorted(self.spec.model.secrets.items()):
            wanted = input_names(name, generator)
            digest = config_digest(generator)
            carried = None
            if name not in rotate:
                for record in records:
                    if (
                        self._sidecar(record.name).get("secrets", {}).get(name)
                        != digest
                    ):
                        continue
                    inputs = record.archive.inputs()
                    if all(item in inputs for item in wanted):
                        carried = {
                            item: store.resolve(target, inputs[item]) for item in wanted
                        }
                        origin[name] = f"carried:{record.name}"
                        break
            if carried is None:
                carried = generate(name, generator)
                origin[name] = "rotated" if name in rotate else "generated"
            values.update(carried)
        return values, origin

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
        for entry in replace:
            parse_adopt_entry(entry, what="replace")
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
                    fingerprint = hashlib.sha256(
                        _canonical(
                            {
                                "images": {n: i.identity for n, i in images.items()},
                                "composition": material,
                                "secrets": {
                                    n: config_digest(g) for n, g in spec.secrets.items()
                                },
                                "rotation": uuid.uuid4().hex if rotate else None,
                            }
                        ).encode()
                    ).hexdigest()
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
                        )
                    intent = "apply"
                else:
                    if rotate:
                        raise ReleaseError("--rotate is not valid for a rollback")
                    name = self.resolve_rollback_target(rollback_to, catalog)
                    intent = "rollback"
                return self._plan_reapply(name, intent, binding, catalog, requested)
            finally:
                journal.close()
                store.close()
        finally:
            binding.close()

    def resolve_rollback_target(
        self, target: str, catalog: ReleaseCatalog | None = None
    ) -> str:
        catalog = catalog or ReleaseCatalog(self.spec.catalog_path)
        if target != "previous":
            try:
                catalog.get(target)
            except ValueError:
                raise ReleaseError(f"unknown release {target!r}") from None
            return target
        if not self.history.deployed():
            raise ReleaseError("no release has been applied yet")
        previous = self.history.previous()
        if previous is None:
            raise ReleaseError("no previous release to roll back to")
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
    ) -> PlanResult:
        settings = self.spec.model.release
        kinds = self._kinds(composition)
        if settings.prune:
            for record in catalog.records():
                kinds |= self._kinds(composition_from_archive(record.archive))
        artifact = self._discover(binding, kinds)
        snapshot = ObservedSnapshot.from_discovery(artifact)
        inherited = list(settings.inherited_owners)
        resolved = requested.resolve(
            composition, snapshot, inherited, settings.field_manager
        )
        adopt, replace = resolved.adopt, resolved.replace
        values, origin = self._private_inputs(catalog, store, binding.target, rotate)
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

        workflow = ReleaseWorkflow(
            catalog,
            binding.target.namespace,
            factory,
            snapshot,
            _plan_authorization(
                binding.target,
                prune=settings.prune,
                adopt=adopt,
                inherited=inherited,
                field_manager=settings.field_manager,
                replace=replace,
            ),
            grant,
            journal,
            store,
        )
        # Discovery is persisted before the session so an interrupted plan can
        # never leave a catalogued release without its evidence.
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
            session_id=uuid.uuid4().hex,
            execution_id=uuid.uuid4().hex,
        )
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
                    "prune": settings.prune,
                    # The plan authorization, so the session reopens exactly.
                    "adopt": adopt,
                    "inherited_owners": inherited,
                    "replace": replace,
                }
            )
            + "\n",
        )
        plan = record.archive.to_dict()["revision"]["desired_state"]
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
        )
        self._persist_plan(result, prune=settings.prune)
        return result

    def _plan_reapply(
        self,
        name: str,
        intent: str,
        binding: ProviderBinding,
        catalog: ReleaseCatalog,
        requested: _Ownership,
    ) -> PlanResult:
        settings = self.spec.model.release
        record = catalog.get(name)
        composition = composition_from_archive(record.archive)
        kinds = self._kinds(composition)
        if settings.prune:
            for other in catalog.records():
                kinds |= self._kinds(composition_from_archive(other.archive))
        artifact = self._discover(binding, kinds)
        snapshot = ObservedSnapshot.from_discovery(artifact)
        inherited = list(settings.inherited_owners)
        resolved = requested.resolve(
            composition, snapshot, inherited, settings.field_manager
        )
        adopt, replace = resolved.adopt, resolved.replace
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
            ),
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
        )
        self._persist_plan(
            result,
            prune=settings.prune,
            discovery=artifact.to_private_json(),
            adopt=adopt,
            inherited=inherited,
            replace=replace,
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
                }
            )
            + "\n",
        )

    def pending_plan(self, plan_hash: str) -> dict[str, Any]:
        path = self._plan_path(plan_hash)
        if not path.exists():
            raise ReleaseError(
                "no pending plan with this hash (unknown, expired or already "
                "applied); run `piceli release plan` again"
            )
        value = json.loads(path.read_text())
        if timestamp(value["expires_at"], allow_future=True) <= _now():
            path.unlink(missing_ok=True)
            raise ReleaseError("the approved plan expired; run plan again")
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
                f"release {record.name!r} has no stored discovery; re-plan it"
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
                f"release {record.name!r} was planned for another owner/field manager"
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
    ) -> dict[str, Any]:
        """Execute the persisted plan ``plan_hash`` (the approval)."""
        pending = self.pending_plan(plan_hash)
        if expected_intent is not None and pending["intent"] != expected_intent:
            raise ReleaseError(
                f"plan {plan_hash[:12]} is a {pending['intent']} plan, "
                f"not a {expected_intent} plan"
            )
        if expected_release is not None and pending["release"] != expected_release:
            raise ReleaseError(
                f"plan {plan_hash[:12]} targets {pending['release']!r}, "
                f"not {expected_release!r}"
            )
        name = pending["release"]
        binding = self.provider_factory(self.spec)
        try:
            catalog, journal, store = self._open()
            try:
                self._check_target(catalog, binding.target)
                record = catalog.get(name)
                executor = PlanExecutor(
                    binding.provider,
                    journal,
                    store,
                    limits=self._limits(),
                    backups=self.backups,
                )
                if pending["mode"] == "create":
                    workflow = self._session_workflow(
                        record, catalog, journal, store, binding.target
                    )
                    session = workflow.reopen(name)
                    if session.revision.plan.plan_hash != plan_hash:
                        raise ReleaseError("stored release does not match the plan")
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
                    )
                    composition = composition_from_archive(record.archive)
                    if (
                        build_plan(composition, snapshot, plan_authorization).plan_hash
                        != plan_hash
                    ):
                        raise ReleaseError("stored evidence does not match the plan")
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
                    }
                )
                try:
                    result = run()
                except ValueError as error:
                    self.history.update(execution_id, state="refused")
                    raise ReleaseError(f"execution refused: {error}") from None
                self._plan_path(plan_hash).unlink(missing_ok=True)
                self.history.update(execution_id, state=result.get("state"))
                if pending["mode"] == "create" and result.get("state") == "ready":
                    catalog.select(name)
                return {
                    "release": name,
                    "intent": pending["intent"],
                    "mode": pending["mode"],
                    "plan_hash": plan_hash,
                    "source": record.source.to_dict(),
                    "execution": _execution_summary(result),
                    "adopted": _adopted(journal, execution_id, result),
                    "selected": catalog.selected().name,
                }
            finally:
                journal.close()
                store.close()
        finally:
            binding.close()

    # --------------------------------------------------------- resume/stop
    def _latest(self, release: str | None) -> dict[str, Any]:
        entries = [
            entry
            for entry in self.history.entries()
            if entry.get("intent") in {"apply", "rollback", "resume"}
            and (release is None or entry["release"] == release)
        ]
        if not entries:
            raise ReleaseError("no execution recorded for this release")
        return entries[-1]

    def resume(self, release: str | None = None) -> dict[str, Any]:
        """Resume the created release's session execution (same grant, same ids)."""
        entry = self._latest(release)
        if entry["mode"] != "create":
            raise ReleaseError(
                "re-apply and rollback executions are not resumable; "
                "run plan/apply (or rollback) again"
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
                )
                try:
                    result = workflow.resume(executor, name)
                except ValueError as error:
                    raise ReleaseError(f"resume refused: {error}") from None
                # Recorded after the run: a resume keeps the execution id, so a
                # concurrent ``stop`` still finds it through the earlier entry.
                self.history.append(
                    entry
                    | {
                        "at": _now().isoformat(),
                        "intent": "resume",
                        "state": result.get("state"),
                    }
                )
                if result.get("state") == "ready":
                    catalog.select(name)
                return {
                    "release": name,
                    "intent": "resume",
                    "execution": _execution_summary(result),
                }
            finally:
                journal.close()
                store.close()
        finally:
            binding.close()

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
                    raise ReleaseError("the execution has not started") from None
                if current["state"] in {"ready", "cancelled"}:
                    raise ReleaseError(
                        f"the latest execution of {name!r} is already "
                        f"{current['state']}; nothing to stop"
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
                        raise ReleaseError("execution belongs to another owner")
                    if authorization["target"] != binding.target.__dict__:
                        raise ReleaseError("execution belongs to another target")
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
                releases.append(
                    {
                        "name": record.name,
                        "release_id": record.release_id,
                        "created_at": sidecar.get("created_at"),
                        "source": record.source.to_dict(),
                        "images": sidecar.get("images", {}),
                        "revision_id": session["revision_id"],
                        "action_count": session["action_count"],
                        "executions": executions,
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
