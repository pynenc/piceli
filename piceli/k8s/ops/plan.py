"""Pure, target-bound Kubernetes deployment composition and planning.

This module has no Kubernetes client imports. Discovery supplies an immutable
snapshot; planning rejects ambiguous ownership and stale execution inputs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from piceli.k8s.ops.discovery import (
    DiscoveryArtifact,
    DiscoveryCoverage,
    DiscoveryProvenance,
    Ownership,
    PlanTarget,
    ResourceScope,
    public_manifest,
)
from piceli.k8s.ops.secret_versions import (
    PRIVATE_VALUE,
    SecretBinding,
    SecretVersionRef,
    replace_pointer,
)

if TYPE_CHECKING:
    from piceli.k8s.k8s_objects.base import K8sObject

PLAN_SCHEMA_VERSION = 3
RETAIN_ANNOTATION = "piceli.io/retained"

_RUNTIME_METADATA = {
    "creationTimestamp",
    "deletionGracePeriodSeconds",
    "deletionTimestamp",
    "generation",
    "managedFields",
    "resourceVersion",
    "selfLink",
    "uid",
}
_EXECUTION_ANNOTATIONS = {"piceli.io/operation", "piceli.io/owner"}
_CLUSTER_SCOPED_KINDS = {
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "Namespace",
    "PersistentVolume",
    "StorageClass",
}
_RETAINED_KINDS = {"Namespace", "PersistentVolume", "PersistentVolumeClaim", "Secret"}
_KIND_LEVEL = {
    kind: level
    for level, kinds in enumerate(
        (
            ("Namespace",),
            (
                "CustomResourceDefinition",
                "StorageClass",
                "Role",
                "ClusterRole",
                "ServiceAccount",
            ),
            ("RoleBinding", "ClusterRoleBinding"),
            ("Secret", "ConfigMap", "PersistentVolume"),
            ("PersistentVolumeClaim",),
            ("Deployment", "StatefulSet", "DaemonSet"),
            ("Service",),
            ("Job", "CronJob"),
            (
                "Ingress",
                "NetworkPolicy",
                "PodDisruptionBudget",
                "HorizontalPodAutoscaler",
            ),
        )
    )
    for kind in kinds
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _normalized_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    normalized = json.loads(_canonical_json(manifest))
    normalized.pop("status", None)
    metadata = normalized.get("metadata")
    if isinstance(metadata, dict):
        for key in _RUNTIME_METADATA:
            metadata.pop(key, None)
        annotations = metadata.get("annotations")
        if isinstance(annotations, dict):
            for key in _EXECUTION_ANNOTATIONS:
                annotations.pop(key, None)
            if not annotations:
                metadata.pop("annotations", None)
    return normalized


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _remove_json_pointer(value: dict[str, Any], pointer: str) -> None:
    if not pointer.startswith("/"):
        raise ValueError(f"defaulted field must be a JSON pointer: {pointer!r}")
    parts = [
        part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")
    ]
    current: Any = value
    for part in parts[:-1]:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return
    if not parts:
        return
    if isinstance(current, dict):
        current.pop(parts[-1], None)
    elif (
        isinstance(current, list)
        and parts[-1].isdigit()
        and int(parts[-1]) < len(current)
    ):
        current.pop(int(parts[-1]))


def _has_json_pointer(value: dict[str, Any], pointer: str) -> bool:
    if not pointer.startswith("/"):
        raise ValueError(f"defaulted field must be a JSON pointer: {pointer!r}")
    parts = [
        part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")
    ]
    current: Any = value
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False
    return bool(parts)


_OWNER_ANNOTATION = "piceli.io/owner"


@dataclass(frozen=True, order=True)
class FieldManagerEntry:
    """One ``metadata.managedFields`` entry, as the API server reported it."""

    manager: str
    operation: str
    subresource: str
    fields_json: str = field(repr=False)

    @property
    def fields(self) -> dict[str, Any]:
        value = json.loads(self.fields_json)
        return value if isinstance(value, dict) else {}


def field_manager_entries(manifest: Mapping[str, Any]) -> tuple[FieldManagerEntry, ...]:
    """Parse ``managedFields`` strictly; malformed ownership evidence is refused."""
    metadata = manifest.get("metadata")
    entries = metadata.get("managedFields") if isinstance(metadata, Mapping) else None
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise ValueError("invalid field ownership evidence")
    result = []
    for entry in entries:
        if (
            not isinstance(entry, Mapping)
            or not isinstance(entry.get("manager", ""), str)
            or not isinstance(entry.get("operation", ""), str)
            or not isinstance(entry.get("subresource", ""), str)
            or not isinstance(entry.get("fieldsV1", {}), Mapping)
        ):
            raise ValueError("invalid field ownership evidence")
        result.append(
            FieldManagerEntry(
                str(entry.get("manager", "")),
                str(entry.get("operation", "")),
                str(entry.get("subresource", "")),
                _canonical_json(entry.get("fieldsV1", {})),
            )
        )
    return tuple(sorted(result))


def _key_matches(item: Any, key: Mapping[str, Any]) -> bool:
    # A key field the desired item omits (e.g. a defaulted port protocol) is a
    # wildcard: over-reporting an owner only widens the reviewed displacement.
    return isinstance(item, Mapping) and all(
        name not in item or item[name] == value for name, value in key.items()
    )


def _fields_overlap(fields: Mapping[str, Any], value: Any) -> bool:
    """Whether a FieldsV1 set owns any field that ``value`` explicitly sets."""
    for raw_key, child in fields.items():
        if raw_key == ".":
            continue
        prefix, _, name = raw_key.partition(":")
        children: list[Any] = []
        if prefix == "f" and isinstance(value, Mapping) and name in value:
            children = [value[name]]
        elif prefix == "k" and isinstance(value, list):
            try:
                key = json.loads(name)
            except ValueError:
                continue
            if isinstance(key, Mapping):
                children = [item for item in value if _key_matches(item, key)]
        elif prefix == "v" and isinstance(value, list):
            try:
                member = json.loads(name)
            except ValueError:
                continue
            children = [member] if member in value else []
        elif prefix == "i" and isinstance(value, list) and name.isdigit():
            children = [value[int(name)]] if int(name) < len(value) else []
        for item in children:
            if not isinstance(child, Mapping) or not child:
                return True
            if _fields_overlap(child, item):
                return True
    return False


def overlapping_managers(
    entries: Iterable[FieldManagerEntry],
    desired: Mapping[str, Any],
    *,
    exclude: Iterable[str] = (),
) -> tuple[str, ...]:
    """Managers (status subresource excluded) owning any explicitly desired field."""
    excluded = set(exclude)
    return tuple(
        sorted(
            {
                entry.manager
                for entry in entries
                if entry.subresource != "status"
                and entry.manager not in excluded
                and _fields_overlap(entry.fields, desired)
            }
        )
    )


@dataclass(frozen=True, order=True)
class ResourceRef:
    api_version: str
    kind: str
    namespace: str
    name: str

    @classmethod
    def from_manifest(
        cls, manifest: Mapping[str, Any], *, scope: ResourceScope | None = None
    ) -> ResourceRef:
        metadata = manifest.get("metadata")
        if not isinstance(metadata, Mapping) or not metadata.get("name"):
            raise ValueError("resource metadata.name is required")
        api_version = manifest.get("apiVersion")
        kind = manifest.get("kind")
        if not isinstance(api_version, str) or not api_version:
            raise ValueError("resource apiVersion is required")
        if not isinstance(kind, str) or not kind:
            raise ValueError("resource kind is required")
        resolved_scope = scope or (
            ResourceScope.CLUSTER
            if kind in _CLUSTER_SCOPED_KINDS
            else ResourceScope.NAMESPACED
        )
        namespace = (
            ""
            if resolved_scope is ResourceScope.CLUSTER
            else str(metadata.get("namespace") or "default")
        )
        return cls(api_version, kind, namespace, str(metadata["name"]))


@dataclass(frozen=True)
class ResourceIntent:
    ref: ResourceRef
    _manifest_json: str = field(repr=False)
    dependencies: tuple[ResourceRef, ...] = ()
    secret_bindings: tuple[SecretBinding, ...] = field(default=(), repr=False)

    def with_secret(
        self, json_pointer: str, reference: SecretVersionRef
    ) -> ResourceIntent:
        """Replace a value with an opaque version; public summaries omit the ref."""
        binding = SecretBinding(json_pointer, reference)
        for existing in self.secret_bindings:
            if (
                existing.json_pointer == json_pointer
                or existing.json_pointer.startswith(json_pointer + "/")
                or json_pointer.startswith(existing.json_pointer + "/")
            ):
                raise ValueError("private bindings cannot overlap")
        manifest = self.manifest
        replace_pointer(manifest, json_pointer, PRIVATE_VALUE)
        return ResourceIntent(
            self.ref,
            _canonical_json(manifest),
            self.dependencies,
            self.secret_bindings + (binding,),
        )

    @classmethod
    def from_manifest(
        cls,
        manifest: Mapping[str, Any],
        dependencies: Iterable[ResourceRef] = (),
        *,
        scope: ResourceScope | None = None,
    ) -> ResourceIntent:
        normalized = _normalized_manifest(manifest)
        return cls(
            ResourceRef.from_manifest(normalized, scope=scope),
            _canonical_json(normalized),
            tuple(sorted(set(dependencies))),
        )

    @classmethod
    def from_k8s_object(
        cls, resource: K8sObject, dependencies: Iterable[ResourceRef] = ()
    ) -> ResourceIntent:
        return cls.from_manifest(resource.spec, dependencies)

    @property
    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    @property
    def digest(self) -> str:
        return _digest(self.manifest)

    @property
    def artifact_digest(self) -> str:
        return _digest(self.redacted_manifest())

    def redacted_manifest(self) -> dict[str, Any]:
        return public_manifest(self.manifest)[0]


@dataclass(frozen=True)
class DeploymentComponent:
    name: str
    resources: tuple[ResourceIntent, ...]
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("component name is required")
        object.__setattr__(
            self, "resources", tuple(sorted(self.resources, key=lambda item: item.ref))
        )
        object.__setattr__(self, "dependencies", tuple(sorted(set(self.dependencies))))


@dataclass(frozen=True)
class DeploymentComposition:
    components: tuple[DeploymentComponent, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "components",
            tuple(sorted(self.components, key=lambda item: item.name)),
        )
        components = {component.name: component for component in self.components}
        if len(components) != len(self.components):
            raise ValueError("component names must be unique")
        refs: set[ResourceRef] = set()
        for component in self.components:
            missing = set(component.dependencies) - components.keys()
            if missing:
                raise ValueError(
                    f"component {component.name!r} has unknown dependencies: {sorted(missing)}"
                )
            for resource in component.resources:
                if resource.ref in refs:
                    raise ValueError(
                        f"resource {resource.ref} is declared more than once"
                    )
                refs.add(resource.ref)
        _validate_component_cycles(components)


def _validate_component_cycles(components: Mapping[str, DeploymentComponent]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise ValueError(f"component dependency cycle includes {name!r}")
        if name in visited:
            return
        visiting.add(name)
        for dependency in components[name].dependencies:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in components:
        visit(name)


@dataclass(frozen=True)
class ResourcePrecondition:
    uid: str | None = None
    resource_version: str | None = None
    must_not_exist: bool = False

    def __post_init__(self) -> None:
        if self.must_not_exist and (self.uid or self.resource_version):
            raise ValueError(
                "absence preconditions cannot include uid/resource_version"
            )
        if not self.must_not_exist and (not self.uid or not self.resource_version):
            raise ValueError("existing resources require uid and resource_version")


@dataclass(frozen=True)
class ObservedResource:
    intent: ResourceIntent
    precondition: ResourcePrecondition
    ownership: Ownership
    retained: bool
    owner_uids: tuple[str, ...] = ()
    # Live ownership evidence used for adoption and drift reports. It is
    # derived from the same discovery manifest, so it is deliberately left out
    # of the snapshot hash (existing archives keep their identity); plans bind
    # what they derive from it into the plan hash instead.
    owner: str | None = None
    field_managers: tuple[FieldManagerEntry, ...] = ()

    def __post_init__(self) -> None:
        annotations = self.intent.manifest.get("metadata", {}).get("annotations", {})
        if not isinstance(self.retained, bool) or (
            (
                self.intent.ref.kind in _RETAINED_KINDS
                or annotations.get(RETAIN_ANNOTATION) == "true"
            )
            and not self.retained
        ):
            raise ValueError("retention protection cannot be disabled")
        if self.precondition.must_not_exist or not isinstance(
            self.ownership, Ownership
        ):
            raise ValueError("invalid observed resource precondition or ownership")

    @classmethod
    def from_manifest(
        cls,
        manifest: Mapping[str, Any],
        *,
        ownership: Ownership,
        scope: ResourceScope | None = None,
        retained: bool | None = None,
    ) -> ObservedResource:
        metadata = manifest.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ValueError("resource metadata is required")
        uid = metadata.get("uid")
        resource_version = metadata.get("resourceVersion")
        if (
            not isinstance(uid, str)
            or not uid
            or not isinstance(resource_version, str)
            or not resource_version
        ):
            raise ValueError(
                "observed resources require metadata.uid and metadata.resourceVersion"
            )
        annotations = metadata.get("annotations")
        retained_annotation = (
            isinstance(annotations, Mapping)
            and str(annotations.get(RETAIN_ANNOTATION, "")).lower() == "true"
        )
        owner_refs = metadata.get("ownerReferences") or ()
        owner_uids = tuple(
            sorted(
                str(owner["uid"])
                for owner in owner_refs
                if isinstance(owner, Mapping) and owner.get("uid")
            )
        )
        intent = ResourceIntent.from_manifest(manifest, scope=scope)
        owner = (
            annotations.get(_OWNER_ANNOTATION)
            if isinstance(annotations, Mapping)
            else None
        )
        retained_value = intent.ref.kind in _RETAINED_KINDS or retained_annotation
        if retained is False and retained_value:
            raise ValueError(
                f"retention protection cannot be disabled for {intent.ref.kind}"
            )
        return cls(
            intent,
            ResourcePrecondition(uid, resource_version),
            ownership,
            retained_value if retained is None else retained,
            owner_uids,
            owner if isinstance(owner, str) and owner else None,
            field_manager_entries(manifest),
        )


@dataclass(frozen=True, order=True)
class DefaultedField:
    resource: ResourceRef
    json_pointer: str


@dataclass(frozen=True)
class ObservedSnapshot:
    target: PlanTarget
    coverage: DiscoveryCoverage
    resources: tuple[ObservedResource, ...] = ()
    defaulted_fields: tuple[DefaultedField, ...] = ()
    incomplete_content: tuple[ResourceRef, ...] = ()
    snapshot_hash: str = field(init=False)
    captured_at: str | None = None
    provenance: DiscoveryProvenance = field(default_factory=DiscoveryProvenance)
    discovery: DiscoveryArtifact | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        resources = tuple(sorted(self.resources, key=lambda item: item.intent.ref))
        if len({item.intent.ref for item in resources}) != len(resources):
            raise ValueError("observed resources must be unique")
        for resource in resources:
            _validate_target_ref(self.target, resource.intent.ref)
        object.__setattr__(self, "resources", resources)
        object.__setattr__(
            self, "defaulted_fields", tuple(sorted(set(self.defaulted_fields)))
        )
        incomplete_content = tuple(sorted(set(self.incomplete_content)))
        if not set(incomplete_content).issubset(
            {resource.intent.ref for resource in resources}
        ):
            raise ValueError("incomplete content must reference observed resources")
        object.__setattr__(self, "incomplete_content", incomplete_content)
        material = {
            "captured_at": self.captured_at,
            "provenance": self.provenance.__dict__,
            "target": self.target.__dict__,
            "coverage": self.coverage.identity_dict(),
            "resources": [
                {
                    "ref": item.intent.ref.__dict__,
                    "uid": item.precondition.uid,
                    "resource_version": item.precondition.resource_version,
                    "ownership": item.ownership.value,
                    "retained": item.retained,
                    "owners": item.owner_uids,
                    "artifact_digest": item.intent.artifact_digest,
                }
                for item in resources
            ],
            "defaulted_fields": [
                {
                    "resource": item.resource.__dict__,
                    "json_pointer": item.json_pointer,
                }
                for item in self.defaulted_fields
            ],
            "incomplete_content": [item.__dict__ for item in incomplete_content],
        }
        object.__setattr__(self, "snapshot_hash", _digest(material))

    @classmethod
    def from_discovery(cls, artifact: DiscoveryArtifact) -> ObservedSnapshot:
        scopes = {
            item.resource_type: item.scope for item in artifact.coverage.api_resources
        }
        resources: list[ObservedResource] = []
        incomplete: list[ResourceRef] = []
        identity_to_ref: dict[object, ResourceRef] = {}
        for resource in artifact.resources:
            resource_type = (resource.identity.api_version, resource.identity.kind)
            scope = next(
                (
                    api_scope
                    for api, api_scope in scopes.items()
                    if (api.api_version, api.kind) == resource_type
                ),
                None,
            )
            if scope is None:
                raise ValueError(
                    f"resource scope was not discovered: {resource.identity}"
                )
            observed = ObservedResource.from_manifest(
                resource.manifest,
                ownership=resource.ownership,
                scope=scope,
                retained=resource.retained,
            )
            if observed.intent.ref.__dict__ != resource.identity.__dict__:
                raise ValueError("discovered identity does not match resource manifest")
            resources.append(observed)
            identity_to_ref[resource.identity] = observed.intent.ref
            if not resource.content_complete:
                incomplete.append(observed.intent.ref)
        try:
            defaults = tuple(
                DefaultedField(identity_to_ref[item.resource], item.json_pointer)
                for item in artifact.defaulted_fields
            )
        except KeyError as error:
            raise ValueError(
                "defaulted field references a resource absent from discovery"
            ) from error
        return cls(
            artifact.target,
            artifact.coverage,
            tuple(resources),
            defaults,
            tuple(incomplete),
            captured_at=artifact.captured_at,
            provenance=artifact.provenance,
            discovery=artifact,
        )


@dataclass(frozen=True)
class PlanAuthorization:
    """What a plan may propose beyond managing its own objects.

    ``adopt_resources`` names existing objects the plan may take over. The
    plan chooses how, from the observed object, and records it in the action
    (and so in the plan hash):

    * retained objects (``Namespace``, ``PersistentVolume``,
      ``PersistentVolumeClaim``, ``Secret`` or ``piceli.io/retained: "true"``)
      are adopted **metadata-only**: only the owner annotation is written, and
      only when the desired manifest is contained in the live object;
    * every other object is adopted by a **takeover**: one forced server-side
      apply, after which the field managers it displaced are removed from
      ``managedFields``.

    ``inherited_owner_ids`` are earlier owner ids whose retained objects may be
    re-stamped by an explicit adoption. They do not change ownership
    classification, which the provider performs during discovery.
    """

    target: PlanTarget
    adopt_resources: tuple[ResourceRef, ...] = ()
    prune_managed: bool = False
    inherited_owner_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "adopt_resources", tuple(sorted(set(self.adopt_resources)))
        )
        if isinstance(self.inherited_owner_ids, str) or any(
            not isinstance(item, str) or not item for item in self.inherited_owner_ids
        ):
            raise ValueError("inherited owner ids must be a tuple of ids")
        object.__setattr__(
            self, "inherited_owner_ids", tuple(sorted(set(self.inherited_owner_ids)))
        )


class PlanOperation(StrEnum):
    CREATE = "create"
    ADOPT = "adopt"
    APPLY = "apply"
    NOOP = "no-op"
    DELETE = "delete"


class AdoptionMode(StrEnum):
    METADATA_ONLY = "metadata-only"
    TAKEOVER = "takeover"


@dataclass(frozen=True)
class Adoption:
    """How an ADOPT action moves ownership; part of the plan hash.

    ``displaced_managers`` are the field managers that own at least one field
    the desired manifest sets. A takeover forces those fields and then removes
    these managers' ``managedFields`` entries; a metadata-only adoption never
    displaces anyone.
    """

    mode: AdoptionMode
    previous_owner: str | None = None
    displaced_managers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.mode, AdoptionMode):
            raise ValueError("invalid adoption mode")
        object.__setattr__(
            self, "displaced_managers", tuple(sorted(set(self.displaced_managers)))
        )
        if self.mode is AdoptionMode.METADATA_ONLY and self.displaced_managers:
            raise ValueError("metadata-only adoption cannot displace field managers")

    def summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "previous_owner": self.previous_owner,
            "displaced_managers": list(self.displaced_managers),
        }


@dataclass(frozen=True)
class PlanAction:
    operation: PlanOperation
    resource: ResourceIntent
    dependencies: tuple[ResourceRef, ...]
    precondition: ResourcePrecondition
    # Set for every ADOPT built by ``build_plan``. ``None`` only for actions
    # restored from older plans; those never force field ownership.
    adoption: Adoption | None = None

    def __post_init__(self) -> None:
        if self.adoption is not None and self.operation is not PlanOperation.ADOPT:
            raise ValueError("only adopt actions carry adoption details")

    def summary(self) -> dict[str, Any]:
        value = {
            "operation": self.operation.value,
            "resource": self.resource.ref.__dict__,
            "artifact_digest": self.resource.artifact_digest,
            "dependencies": [dependency.__dict__ for dependency in self.dependencies],
            "precondition": self.precondition.__dict__,
            "manifest": self.resource.redacted_manifest(),
        }
        if self.adoption is not None:
            value["adoption"] = self.adoption.summary()
        return value


@dataclass(frozen=True)
class DeploymentPlan:
    target: PlanTarget
    snapshot_hash: str
    actions: tuple[PlanAction, ...]
    levels: tuple[tuple[ResourceRef, ...], ...]
    protected_resources: tuple[ResourceRef, ...] = ()
    schema_version: int = PLAN_SCHEMA_VERSION
    plan_hash: str = field(init=False)

    def __post_init__(self) -> None:
        material = {
            "schema_version": self.schema_version,
            "target": self.target.__dict__,
            "snapshot_hash": self.snapshot_hash,
            "actions": [action.summary() for action in self.actions],
            "levels": [
                [resource.__dict__ for resource in level] for level in self.levels
            ],
            "protected_resources": [
                resource.__dict__ for resource in self.protected_resources
            ],
        }
        object.__setattr__(self, "plan_hash", _digest(material))

    def validate_for(self, snapshot: ObservedSnapshot) -> None:
        """Reject a plan when target identity or resource versions changed."""
        if snapshot.target != self.target:
            raise ValueError(
                "plan target does not match the current cluster and namespace"
            )
        if snapshot.snapshot_hash != self.snapshot_hash:
            raise ValueError("observed snapshot changed after planning")
        current = {resource.intent.ref: resource for resource in snapshot.resources}
        for action in self.actions:
            observed = current.get(action.resource.ref)
            if action.precondition.must_not_exist:
                if observed is not None:
                    raise ValueError(
                        f"resource appeared after planning: {action.resource.ref}"
                    )
            elif observed is None or observed.precondition != action.precondition:
                raise ValueError(
                    f"resource UID/version changed after planning: {action.resource.ref}"
                )

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_hash": self.plan_hash,
            "target": self.target.__dict__,
            "snapshot_hash": self.snapshot_hash,
            "actions": [action.summary() for action in self.actions],
            "levels": [
                [resource.__dict__ for resource in level] for level in self.levels
            ],
            "protected_resources": [
                resource.__dict__ for resource in self.protected_resources
            ],
        }


def component_from_objects(
    name: str, resources: Iterable[K8sObject], *, dependencies: Iterable[str] = ()
) -> DeploymentComponent:
    return DeploymentComponent(
        name,
        tuple(ResourceIntent.from_k8s_object(resource) for resource in resources),
        tuple(dependencies),
    )


def _validate_target_ref(target: PlanTarget, ref: ResourceRef) -> None:
    if ref.namespace and ref.namespace != target.namespace:
        raise ValueError(
            f"resource {ref} is outside bound namespace {target.namespace!r}"
        )


def _desired_dependencies(
    composition: DeploymentComposition,
) -> dict[ResourceRef, set[ResourceRef]]:
    components = {component.name: component for component in composition.components}
    resources = {
        resource.ref: resource
        for component in composition.components
        for resource in component.resources
    }
    dependencies = {
        ref: set(resource.dependencies) for ref, resource in resources.items()
    }
    for component in composition.components:
        inherited = {
            resource.ref
            for component_name in component.dependencies
            for resource in components[component_name].resources
        }
        for resource in component.resources:
            dependencies[resource.ref].update(inherited)

    def transitively_depends_on(start: ResourceRef, target: ResourceRef) -> bool:
        pending = list(dependencies[start])
        seen: set[ResourceRef] = set()
        while pending:
            dependency = pending.pop()
            if dependency == target:
                return True
            if dependency not in seen:
                seen.add(dependency)
                pending.extend(dependencies.get(dependency, ()))
        return False

    previous: set[ResourceRef] = set()
    for level in sorted({_KIND_LEVEL.get(ref.kind, 4) for ref in resources}):
        current = {ref for ref in resources if _KIND_LEVEL.get(ref.kind, 4) == level}
        for ref in current:
            dependencies[ref].update(
                candidate
                for candidate in previous
                if not transitively_depends_on(candidate, ref)
            )
        previous.update(current)
    for ref, refs in dependencies.items():
        missing = refs - resources.keys()
        if missing:
            raise ValueError(
                f"resource {ref} has unknown dependencies: {sorted(missing)}"
            )
    return dependencies


def _topological_levels(
    dependencies: Mapping[ResourceRef, set[ResourceRef]],
) -> tuple[tuple[ResourceRef, ...], ...]:
    remaining = {ref: set(deps) for ref, deps in dependencies.items()}
    levels: list[tuple[ResourceRef, ...]] = []
    while remaining:
        ready = tuple(sorted(ref for ref, deps in remaining.items() if not deps))
        if not ready:
            raise ValueError("resource dependency graph contains a cycle")
        levels.append(ready)
        ready_set = set(ready)
        remaining = {
            ref: deps - ready_set
            for ref, deps in remaining.items()
            if ref not in ready_set
        }
    return tuple(levels)


def _equivalent(
    desired: ResourceIntent,
    observed: ResourceIntent,
    defaulted: Iterable[DefaultedField],
) -> bool:
    # Never expose equality of secret values through a public plan operation.
    if desired.secret_bindings:
        return False
    left = desired.manifest
    right = observed.manifest
    for defaulted_field in defaulted:
        if defaulted_field.resource == desired.ref and not _has_json_pointer(
            left, defaulted_field.json_pointer
        ):
            _remove_json_pointer(right, defaulted_field.json_pointer)
    return left == right


def _deletion_depth(
    resource: ObservedResource,
    by_uid: Mapping[str, ObservedResource],
    visiting: frozenset[str] = frozenset(),
) -> int:
    uid = resource.precondition.uid
    if uid in visiting:
        raise ValueError(
            f"observed owner reference graph contains a cycle at {resource.intent.ref}"
        )
    next_visiting = visiting | ({uid} if uid is not None else set())
    parents = [by_uid[uid] for uid in resource.owner_uids if uid in by_uid]
    return (
        0
        if not parents
        else 1
        + max(_deletion_depth(parent, by_uid, next_visiting) for parent in parents)
    )


def _adoptable(resource: ObservedResource, authorization: PlanAuthorization) -> bool:
    """Unmanaged objects, or retained objects of an explicitly inherited owner."""
    return resource.ownership is Ownership.UNMANAGED or (
        resource.retained
        and resource.owner is not None
        and resource.owner in authorization.inherited_owner_ids
    )


def _without_pointers(
    manifest: dict[str, Any], pointers: Iterable[str]
) -> dict[str, Any]:
    for pointer in pointers:
        _remove_json_pointer(manifest, pointer)
    return manifest


def adoption_for(desired: ResourceIntent, current: ObservedResource) -> Adoption:
    """Choose the adoption mode from the live object; refuse unsafe ones."""
    if current.retained:
        # Private values are compared by the executor after resolution; the
        # public plan never reveals whether a secret value matches.
        expected = _without_pointers(
            desired.manifest,
            (binding.json_pointer for binding in desired.secret_bindings),
        )
        if not manifest_contains(current.intent.manifest, expected):
            raise ValueError(
                "retained resource can only be adopted when the live object "
                f"already contains the desired manifest: {desired.ref}"
            )
        return Adoption(AdoptionMode.METADATA_ONLY, current.owner)
    return Adoption(
        AdoptionMode.TAKEOVER,
        current.owner,
        overlapping_managers(current.field_managers, desired.manifest),
    )


def manifest_contains(actual: Any, expected: Any) -> bool:
    """Every explicitly desired value is present; the server may add more.

    Lists must have the same length and each item must contain the desired
    item at the same position: the API server adds defaults inside list items
    (for example a container's ``imagePullPolicy``) but never reorders them.
    """
    if isinstance(actual, dict) and isinstance(expected, dict):
        return all(
            key in actual and manifest_contains(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            manifest_contains(left, right)
            for left, right in zip(actual, expected, strict=True)
        )
    return bool(actual == expected)


def field_drift(
    composition: DeploymentComposition,
    snapshot: ObservedSnapshot,
    field_manager: str,
) -> list[dict[str, Any]]:
    """Managed objects whose desired fields are also owned by another manager.

    After an adoption Piceli is the only owner of the fields it declares, so a
    later ``kubectl`` edit of one of them shows up here. This report is
    informational and not part of the plan hash.
    """
    observed = {resource.intent.ref: resource for resource in snapshot.resources}
    report = []
    for component in composition.components:
        for resource in component.resources:
            current = observed.get(resource.ref)
            # Retained objects are never rewritten, so co-owned fields there
            # (e.g. after a metadata-only adoption) are expected, not drift.
            if (
                current is None
                or current.ownership is not Ownership.MANAGED
                or current.retained
            ):
                continue
            managers = overlapping_managers(
                current.field_managers, resource.manifest, exclude=(field_manager,)
            )
            if managers:
                report.append(
                    {"resource": resource.ref.__dict__, "managers": list(managers)}
                )
    return sorted(report, key=lambda item: ResourceRef(**item["resource"]))


def build_plan(
    composition: DeploymentComposition,
    snapshot: ObservedSnapshot,
    authorization: PlanAuthorization,
) -> DeploymentPlan:
    """Build a deterministic, non-executable plan from supplied state only."""
    if authorization.target != snapshot.target:
        raise ValueError("authorization target does not match observed target")
    for ref in authorization.adopt_resources:
        _validate_target_ref(snapshot.target, ref)
    desired = {
        resource.ref: resource
        for component in composition.components
        for resource in component.resources
    }
    for ref in desired:
        _validate_target_ref(snapshot.target, ref)
    observed = {resource.intent.ref: resource for resource in snapshot.resources}
    adoptable = {
        ref
        for ref, resource in observed.items()
        if ref in desired and _adoptable(resource, authorization)
    }
    unused_adoptions = set(authorization.adopt_resources) - adoptable
    if unused_adoptions:
        raise ValueError(
            "adoption authorization does not match unmanaged desired resources: "
            f"{sorted(unused_adoptions)}"
        )
    dependencies = _desired_dependencies(composition)
    levels = _topological_levels(dependencies)
    actions: list[PlanAction] = []
    for level in levels:
        for ref in level:
            current = observed.get(ref)
            if current is None:
                if not snapshot.coverage.is_complete_for(ref.api_version, ref.kind):
                    raise ValueError(
                        f"cannot infer resource absence from incomplete discovery: {ref}"
                    )
                operation = PlanOperation.CREATE
                precondition = ResourcePrecondition(must_not_exist=True)
                adoption = None
            else:
                if ref in snapshot.incomplete_content:
                    raise ValueError(
                        f"cannot compare resource with redacted or incomplete content: {ref}"
                    )
                precondition = current.precondition
                adoption = None
                if ref in authorization.adopt_resources:
                    operation = PlanOperation.ADOPT
                    adoption = adoption_for(desired[ref], current)
                elif current.ownership is Ownership.UNMANAGED:
                    raise ValueError(f"resource requires explicit adoption: {ref}")
                elif _equivalent(
                    desired[ref], current.intent, snapshot.defaulted_fields
                ):
                    operation = PlanOperation.NOOP
                else:
                    operation = PlanOperation.APPLY
            actions.append(
                PlanAction(
                    operation,
                    desired[ref],
                    tuple(sorted(dependencies[ref])),
                    precondition,
                    adoption,
                )
            )

    protected: list[ResourceRef] = []
    if authorization.prune_managed:
        if not snapshot.coverage.complete:
            raise ValueError("pruning requires complete discovery coverage")
        candidates = [
            resource
            for ref, resource in observed.items()
            if ref not in desired and resource.ownership is Ownership.MANAGED
        ]
        deleting_uids = {
            resource.precondition.uid
            for resource in candidates
            if not resource.retained
        }
        for resource in candidates:
            if resource.retained:
                protected.append(resource.intent.ref)
                continue
            unsafe_children = [
                child.intent.ref
                for child in snapshot.resources
                if resource.precondition.uid in child.owner_uids
                and child.precondition.uid not in deleting_uids
            ]
            if unsafe_children:
                raise ValueError(
                    f"cannot delete {resource.intent.ref}; retained or unmanaged descendants exist: {sorted(unsafe_children)}"
                )
        by_uid = {
            resource.precondition.uid: resource
            for resource in snapshot.resources
            if resource.precondition.uid is not None
        }
        for current in sorted(
            (item for item in candidates if not item.retained),
            key=lambda item: (-_deletion_depth(item, by_uid), item.intent.ref),
        ):
            actions.append(
                PlanAction(
                    PlanOperation.DELETE, current.intent, (), current.precondition
                )
            )

    return DeploymentPlan(
        snapshot.target,
        snapshot.snapshot_hash,
        tuple(actions),
        levels,
        tuple(sorted(protected)),
    )
