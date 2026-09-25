"""Pure, target-bound Kubernetes deployment composition and planning.

This module has no Kubernetes client imports. Discovery supplies an immutable
snapshot; planning rejects ambiguous ownership and stale execution inputs.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from piceli.k8s.ops.discovery import (
    RELEASE_NAMESPACE_ANNOTATION,
    DiscoveryArtifact,
    DiscoveryCoverage,
    DiscoveryProvenance,
    Ownership,
    PlanTarget,
    ResourceScope,
    ServerDryRun,
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
                "HTTPRoute",
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


def normalized_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Object content without status, runtime metadata and execution annotations."""
    return _normalized_manifest(manifest)


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
    # wildcard: over-reporting an owner only widens the reported drift.
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


# Field managers of the Kubernetes control plane. Their entries are never
# transferred by a takeover: they record controller bookkeeping (a
# Deployment's revision annotation, a claim's binding) that a manifest does
# not declare and that the controller keeps writing.
CONTROL_PLANE_MANAGERS = frozenset(
    {
        "kube-apiserver",
        "kube-controller-manager",
        "kube-scheduler",
        "kubelet",
        "cloud-controller-manager",
    }
)


def is_control_plane_manager(manager: str) -> bool:
    """Control-plane or controller manager, by name (see docs for the rule)."""
    return (
        manager in CONTROL_PLANE_MANAGERS
        or manager.startswith("k3s")
        or manager.endswith(("-controller", "-controller-manager"))
    )


def is_transferable(entry: FieldManagerEntry) -> bool:
    """A takeover transfers main-resource entries written by clients.

    Kept: any subresource entry (``status``, ``scale`` written by an
    autoscaler, ...) and control-plane/controller managers. Everything else —
    ``kubectl-client-side-apply``, ``kubectl-create``, ``kubectl-set``,
    ``kubectl-edit``, ``kubectl-rollout``, other tools' Apply or Update
    entries — is transferred.
    """
    return (
        not entry.subresource
        and entry.operation in {"Apply", "Update"}
        and not is_control_plane_manager(entry.manager)
    )


def transferable_managers(
    entries: Iterable[FieldManagerEntry], *, exclude: Iterable[str] = ()
) -> tuple[str, ...]:
    excluded = set(exclude)
    return tuple(
        sorted(
            {
                entry.manager
                for entry in entries
                if is_transferable(entry) and entry.manager not in excluded
            }
        )
    )


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
        annotations = metadata.get("annotations")
        resolved_scope = scope or (
            ResourceScope.CLUSTER
            if kind in _CLUSTER_SCOPED_KINDS
            # Any other kind is cluster-scoped when it names its release's
            # namespace in the annotation instead of in metadata.namespace
            # (how ``App.resource(..., scope="cluster")`` renders it).
            or (
                not metadata.get("namespace")
                and isinstance(annotations, Mapping)
                and RELEASE_NAMESPACE_ANNOTATION in annotations
            )
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
    # Server-side dry runs of the desired writes (see ServerDryRun). Evidence
    # like the resources: bound into the snapshot hash when present.
    server_dry_runs: tuple[ServerDryRun, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        resources = tuple(sorted(self.resources, key=lambda item: item.intent.ref))
        if len({item.intent.ref for item in resources}) != len(resources):
            raise ValueError("observed resources must be unique")
        for resource in resources:
            _validate_target_ref(self.target, resource.intent.ref)
        object.__setattr__(self, "resources", resources)
        dry_runs = tuple(sorted(self.server_dry_runs))
        versions = {
            ResourceRef(**item.intent.ref.__dict__): item.precondition.resource_version
            for item in resources
        }
        if len({item.resource for item in dry_runs}) != len(dry_runs) or any(
            versions.get(ResourceRef(**item.resource.__dict__)) != item.resource_version
            for item in dry_runs
        ):
            raise ValueError("server dry runs must match observed resources")
        object.__setattr__(self, "server_dry_runs", dry_runs)
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
        if dry_runs:
            # Only when present, so snapshots without evidence keep their hash.
            material["server_dry_runs"] = [
                {
                    "resource": item.resource.__dict__,
                    "desired_digest": item.desired_digest,
                    "resource_version": item.resource_version,
                    "artifact_digest": _digest(public_manifest(item.manifest)[0]),
                }
                for item in dry_runs
            ]
        object.__setattr__(self, "snapshot_hash", _digest(material))

    def server_dry_run(self, ref: ResourceRef) -> ServerDryRun | None:
        """The server dry run captured for ``ref``, if any."""
        return next(
            (
                item
                for item in self.server_dry_runs
                if ResourceRef(**item.resource.__dict__) == ref
            ),
            None,
        )

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
            server_dry_runs=artifact.server_dry_runs,
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
    * every other object is adopted by a **takeover**: the fields of every
      transferable field manager (see :func:`transferable_managers`) move to
      this field manager, then the desired manifest is applied without force,
      so fields the manifest does not declare are removed. A takeover may also
      be requested for an object this owner already manages, to reclaim fields
      that other clients (e.g. ``kubectl``) wrote since.

    ``field_manager`` is the executing field manager; it is never listed as a
    transferred manager.

    ``inherited_owner_ids`` are earlier owner ids whose retained objects may be
    re-stamped by an explicit adoption. They do not change ownership
    classification, which the provider performs during discovery.

    ``replace_resources`` names existing **unmanaged, non-retained** objects
    the plan may delete and recreate from the release (a ``replace`` action).
    Retained kinds and objects, objects already managed (including by an
    inherited owner) and objects owned by another object are refused.
    """

    target: PlanTarget
    adopt_resources: tuple[ResourceRef, ...] = ()
    prune_managed: bool = False
    inherited_owner_ids: tuple[str, ...] = ()
    field_manager: str | None = None
    replace_resources: tuple[ResourceRef, ...] = ()
    # What earlier releases of this owner declared, one merged intent per
    # object (see :func:`declared_union`). A same-owner ``apply`` removes the
    # map keys declared there that the composition no longer declares (see
    # :func:`planned_removals`); without it no field is ever removed.
    previous: tuple[ResourceIntent, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "adopt_resources", tuple(sorted(set(self.adopt_resources)))
        )
        previous = tuple(sorted(self.previous, key=lambda item: item.ref))
        if any(not isinstance(item, ResourceIntent) for item in previous) or len(
            {item.ref for item in previous}
        ) != len(previous):
            raise ValueError("previous declarations must be unique resource intents")
        object.__setattr__(self, "previous", previous)
        object.__setattr__(
            self, "replace_resources", tuple(sorted(set(self.replace_resources)))
        )
        both = set(self.adopt_resources) & set(self.replace_resources)
        if both:
            raise ValueError(
                f"a resource cannot be both adopted and replaced: {sorted(both)}"
            )
        if self.field_manager is not None and (
            not isinstance(self.field_manager, str) or not self.field_manager
        ):
            raise ValueError("field manager must be a non-empty string")
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
    # Delete an unmanaged, non-retained object and create it from the release.
    REPLACE = "replace"


class AdoptionMode(StrEnum):
    METADATA_ONLY = "metadata-only"
    TAKEOVER = "takeover"


@dataclass(frozen=True)
class Adoption:
    """How an ADOPT action moves ownership; part of the plan hash.

    ``transferred_managers`` are every transferable field manager of the live
    object (not only those owning declared fields). A takeover moves all of
    their fields to this field manager and then applies the desired manifest,
    which removes every transferred field the manifest does not declare. A
    metadata-only adoption never transfers anything.
    """

    mode: AdoptionMode
    previous_owner: str | None = None
    transferred_managers: tuple[str, ...] = ()
    # Metadata-only adoptions: desired labels/annotations (``labels/<key>``,
    # ``annotations/<key>``) the write sets besides the owner annotation.
    metadata_changes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.mode, AdoptionMode):
            raise ValueError("invalid adoption mode")
        object.__setattr__(
            self,
            "transferred_managers",
            tuple(sorted(set(self.transferred_managers))),
        )
        object.__setattr__(
            self, "metadata_changes", tuple(sorted(set(self.metadata_changes)))
        )
        if self.mode is AdoptionMode.METADATA_ONLY and self.transferred_managers:
            raise ValueError("metadata-only adoption cannot transfer field managers")
        if self.mode is AdoptionMode.TAKEOVER and self.metadata_changes:
            raise ValueError("a takeover applies the whole manifest")

    def summary(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "mode": self.mode.value,
            "previous_owner": self.previous_owner,
            "transferred_managers": list(self.transferred_managers),
        }
        if self.mode is AdoptionMode.TAKEOVER:
            value["removes_undeclared_fields"] = True
        if self.metadata_changes:
            value["metadata_changes"] = list(self.metadata_changes)
        return value


@dataclass(frozen=True)
class PlanAction:
    operation: PlanOperation
    resource: ResourceIntent
    dependencies: tuple[ResourceRef, ...]
    precondition: ResourcePrecondition
    # Set for every ADOPT built by ``build_plan``. ``None`` only for actions
    # restored from older plans; those never force field ownership.
    adoption: Adoption | None = None
    # An APPLY on a retained object whose only difference is metadata: the
    # executor writes these labels/annotations (and the owner annotation)
    # with a metadata-only patch, never spec or data.
    metadata_changes: tuple[str, ...] = ()
    # JSON pointers of map keys an earlier release declared, the composition
    # dropped and no other field manager owns: a same-owner APPLY sends them
    # as explicit nulls in its merge patch (three-way removal).
    removals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.adoption is not None and self.operation is not PlanOperation.ADOPT:
            raise ValueError("only adopt actions carry adoption details")
        object.__setattr__(
            self, "metadata_changes", tuple(sorted(set(self.metadata_changes)))
        )
        if self.metadata_changes and self.operation is not PlanOperation.APPLY:
            raise ValueError("only apply actions carry metadata-only changes")
        object.__setattr__(self, "removals", tuple(sorted(set(self.removals))))
        if self.removals and (
            self.operation is not PlanOperation.APPLY or self.metadata_changes
        ):
            raise ValueError("only full apply actions carry field removals")
        for pointer in self.removals:
            _removal_parts(pointer)

    def summary(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "operation": self.operation.value,
            "resource": self.resource.ref.__dict__,
            "artifact_digest": self.resource.artifact_digest,
            "dependencies": [dependency.__dict__ for dependency in self.dependencies],
            "precondition": self.precondition.__dict__,
            "manifest": self.resource.redacted_manifest(),
        }
        if self.adoption is not None:
            value["adoption"] = self.adoption.summary()
        if self.metadata_changes:
            value["metadata_only"] = list(self.metadata_changes)
        if self.removals:
            value["removes"] = list(self.removals)
        if self.operation is PlanOperation.REPLACE:
            value["replace"] = {
                "deletes_uid": self.precondition.uid,
                "propagation": replace_propagation(self.resource.ref.kind),
                "backup": "written to the state directory before the delete",
            }
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
    # Secret-bound values are compared only through PrivateEvidence.
    if desired.secret_bindings:
        return False
    return _literal_equal(desired.ref, desired.manifest, observed.manifest, defaulted)


def _literal_equal(
    ref: ResourceRef,
    left: dict[str, Any],
    right: dict[str, Any],
    defaulted: Iterable[DefaultedField],
) -> bool:
    for defaulted_field in defaulted:
        if defaulted_field.resource == ref and not _has_json_pointer(
            left, defaulted_field.json_pointer
        ):
            _remove_json_pointer(right, defaulted_field.json_pointer)
    return left == right


def usable_dry_run(
    desired: ResourceIntent, evidence: ServerDryRun | None
) -> ServerDryRun | None:
    """``evidence`` when it was captured for exactly this desired manifest."""
    if (
        evidence is None
        or desired.secret_bindings
        or evidence.desired_digest != desired.digest
        or "<redacted>" in evidence.manifest_json
    ):
        return None
    return evidence


def server_equivalent(
    desired: ResourceIntent,
    current: ObservedResource,
    evidence: ServerDryRun | None,
) -> bool:
    """The executor's write of ``desired`` would leave ``current`` unchanged.

    ``evidence`` is the API server's dry run of that exact write against the
    observed resourceVersion, so it includes server defaults, canonical
    values and allocated fields; it is compared with the live object, with
    status, runtime metadata and Piceli's execution annotations excluded.
    Without usable evidence the answer is ``False``: a no-op is never guessed.
    """
    usable = usable_dry_run(desired, evidence)
    return (
        usable is not None
        and usable.resource_version == current.precondition.resource_version
        and _normalized_manifest(usable.manifest) == current.intent.manifest
    )


def dry_run_confirms(
    snapshot: ObservedSnapshot, desired: ResourceIntent, live: Mapping[str, Any]
) -> bool:
    """The planned dry run of ``desired`` matches ``live`` (a re-read object).

    The executor uses it for a no-op planned from server evidence whose
    declared values the server canonicalizes (``cpu: 0.5`` is stored as
    ``500m``), where a literal containment check would fail.
    """
    usable = usable_dry_run(desired, snapshot.server_dry_run(desired.ref))
    return usable is not None and _normalized_manifest(
        usable.manifest
    ) == _normalized_manifest(live)


def private_comparable(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """``manifest`` in the form the API server stores it, for private comparison.

    A Secret's ``stringData`` is stored base64-encoded in ``data``; every
    other kind is returned unchanged.
    """
    value = copy.deepcopy(dict(manifest))
    if value.get("kind") != "Secret" or value.get("apiVersion") != "v1":
        return value
    string_data = value.pop("stringData", None)
    if isinstance(string_data, Mapping):
        data = dict(value.get("data") or {})
        for key, item in string_data.items():
            if not isinstance(item, str):
                raise ValueError("Secret stringData values must be strings")
            data[key] = base64.b64encode(item.encode()).decode()
        value["data"] = data
    return value


@dataclass(frozen=True)
class PrivateEvidence:
    """Which secret-bound objects already hold their desired private content.

    Computed in-process by :func:`private_evidence` from resolved secret
    versions and the private discovery evidence. It holds only object
    references: no value, digest or fingerprint of a value is kept, rendered
    or serialised. A plan reveals one bit per object, ``no-op`` or ``apply``.
    """

    matching: frozenset[ResourceRef] = frozenset()


def private_evidence(
    composition: DeploymentComposition,
    snapshot: ObservedSnapshot,
    resolve: Callable[[SecretVersionRef], Any],
) -> PrivateEvidence:
    """Compare resolved secret-bound desired objects with the live objects.

    Only managed objects with complete private content are compared. Both
    sides are reduced to an HMAC-SHA256 under a random key that lives only for
    this call and are compared in constant time; an unresolvable version
    counts as a difference. Server defaults are handled like the literal
    comparison (discovery's defaulted fields, a Secret's ``type`` and
    ``stringData``); anything else the server adds makes the object ``apply``.
    """
    observed = {resource.intent.ref: resource for resource in snapshot.resources}
    key = secrets.token_bytes(32)

    def keyed(value: Any) -> bytes:
        return hmac.new(key, _canonical_json(value).encode(), hashlib.sha256).digest()

    matching: set[ResourceRef] = set()
    for component in composition.components:
        for resource in component.resources:
            current = observed.get(resource.ref)
            if (
                not resource.secret_bindings
                or current is None
                or current.ownership is not Ownership.MANAGED
                or resource.ref in snapshot.incomplete_content
            ):
                continue
            try:
                manifest = resource.manifest
                for binding in resource.secret_bindings:
                    replace_pointer(
                        manifest, binding.json_pointer, resolve(binding.reference)
                    )
                desired = private_comparable(manifest)
            except (ValueError, KeyError, TypeError):
                continue
            live = current.intent.manifest
            if (
                resource.ref.kind == "Secret"
                and "type" not in desired
                and live.get("type") == "Opaque"
            ):
                live.pop("type")  # the API server's default
            for defaulted_field in snapshot.defaulted_fields:
                if defaulted_field.resource == resource.ref and not _has_json_pointer(
                    desired, defaulted_field.json_pointer
                ):
                    _remove_json_pointer(live, defaulted_field.json_pointer)
            if hmac.compare_digest(keyed(desired), keyed(live)):
                matching.add(resource.ref)
    return PrivateEvidence(frozenset(matching))


def _removal_parts(pointer: str) -> list[str]:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError(f"field removal must be a JSON pointer: {pointer!r}")
    parts = [
        part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")
    ]
    if (
        parts[0] in {"", "apiVersion", "kind", "status"}
        or (
            parts[0] == "metadata"
            and (len(parts) < 3 or parts[1] not in _METADATA_MAPS)
        )
        or (
            parts[0] == "metadata"
            and parts[1] == "annotations"
            and parts[2] in _EXECUTION_ANNOTATIONS
        )
    ):
        raise ValueError(f"field removal cannot remove {pointer!r}")
    return parts


def _escape_pointer(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _dropped_keys(
    previous: Mapping[str, Any],
    desired: Mapping[str, Any],
    live: Mapping[str, Any],
    path: tuple[str, ...],
) -> list[tuple[str, ...]]:
    """Map keys ``previous`` declares, ``desired`` does not and ``live`` has.

    Recurses only through maps (a merge patch replaces lists whole, so a
    dropped list item is already removed). A dropped map is removed key by key,
    so keys that only another writer added survive.
    """
    dropped: list[tuple[str, ...]] = []
    for key in sorted(previous):
        if key not in live:
            continue
        child = path + (key,)
        if not path and key in {"apiVersion", "kind", "status"}:
            continue
        if path == ("metadata",) and key not in _METADATA_MAPS:
            continue
        if path == ("metadata", "annotations") and key in _EXECUTION_ANNOTATIONS:
            continue
        before, now = previous[key], live[key]
        if key in desired:
            wanted = desired[key]
            if (
                isinstance(before, Mapping)
                and isinstance(wanted, Mapping)
                and isinstance(now, Mapping)
            ):
                dropped.extend(_dropped_keys(before, wanted, now, child))
        elif isinstance(before, Mapping) and isinstance(now, Mapping):
            dropped.extend(_dropped_keys(before, {}, now, child))
        else:
            dropped.append(child)
    return dropped


def _sparse(value: Mapping[str, Any], parts: tuple[str, ...]) -> dict[str, Any]:
    """The value at ``parts`` of ``value``, nested under the same keys."""
    node: Any = value
    for part in parts:
        node = node[part]
    for part in reversed(parts):
        node = {part: node}
    return dict(node)


def planned_removals(
    previous: ResourceIntent | None,
    desired: ResourceIntent,
    current: ObservedResource,
    field_manager: str | None,
) -> tuple[str, ...]:
    """Three-way removal: keys an earlier release declared and this one dropped.

    A key is removed only when an earlier release of this owner declared it
    (``previous``), the desired manifest no longer declares it, it is live, and
    no other field manager owns it (``managedFields``; the status subresource
    and this release's own ``field_manager`` excepted). Retained objects are
    never rewritten, so they get no removals.
    """
    if previous is None or current.retained:
        return ()
    live = current.intent.manifest
    removals = []
    for parts in _dropped_keys(previous.manifest, desired.manifest, live, ()):
        if overlapping_managers(
            current.field_managers,
            _sparse(live, parts),
            exclude=() if field_manager is None else (field_manager,),
        ):
            continue
        removals.append("/" + "/".join(_escape_pointer(part) for part in parts))
    return tuple(sorted(removals))


def removal_patch(
    manifest: Mapping[str, Any], removals: Iterable[str]
) -> dict[str, Any]:
    """``manifest`` with an explicit ``null`` (merge-patch delete) per removal."""
    patch = copy.deepcopy(dict(manifest))
    for pointer in removals:
        parts = _removal_parts(pointer)
        node = patch
        for part in parts[:-1]:
            child = node.get(part)
            if child is None:
                child = node[part] = {}
            if not isinstance(child, dict):
                raise ValueError(f"field removal crosses a non-map value: {pointer!r}")
            node = child
        if parts[-1] in node and node[parts[-1]] is not None:
            raise ValueError(f"field removal names a declared field: {pointer!r}")
        node[parts[-1]] = None
    return patch


def has_removed_field(manifest: Mapping[str, Any], removals: Iterable[str]) -> bool:
    """Whether any removed field is still present in ``manifest``."""
    value = dict(manifest)
    return any(_has_json_pointer(value, pointer) for pointer in removals)


def without_removed_fields(
    manifest: Mapping[str, Any], removals: Iterable[str]
) -> dict[str, Any]:
    """``manifest`` without the removed fields (what the write leaves)."""
    value = copy.deepcopy(dict(manifest))
    for pointer in removals:
        _remove_json_pointer(value, pointer)
    return value


def declared_union(intents: Iterable[ResourceIntent]) -> tuple[ResourceIntent, ...]:
    """Merge earlier declarations of each object into one intent per object.

    Maps are merged key by key; for any other value the last one wins (only
    which keys were declared matters for removals).
    """

    def merge(left: Any, right: Any) -> Any:
        if isinstance(left, dict) and isinstance(right, dict):
            merged = dict(left)
            for key, value in right.items():
                merged[key] = merge(left.get(key), value)
            return merged
        return right

    merged: dict[ResourceRef, dict[str, Any]] = {}
    for intent in intents:
        merged[intent.ref] = merge(merged.get(intent.ref), intent.manifest)
    return tuple(
        ResourceIntent(ref, _canonical_json(manifest))
        for ref, manifest in sorted(merged.items())
    )


def _propagation_deletes_protected(
    resource: ObservedResource, snapshot: ObservedSnapshot
) -> bool:
    """A background delete of ``resource`` would reach a protected child."""
    if replace_propagation(resource.intent.ref.kind) != "Background":
        return False
    uid = resource.precondition.uid
    return any(
        uid in child.owner_uids and child.retained for child in snapshot.resources
    )


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
    """Unmanaged objects, managed non-retained objects (a takeover reclaims
    foreign fields), or retained objects of an explicitly inherited owner."""
    return (
        resource.ownership is Ownership.UNMANAGED
        or not resource.retained
        or (
            resource.owner is not None
            and resource.owner in authorization.inherited_owner_ids
        )
    )


def _without_pointers(
    manifest: dict[str, Any], pointers: Iterable[str]
) -> dict[str, Any]:
    for pointer in pointers:
        _remove_json_pointer(manifest, pointer)
    return manifest


# Kinds whose dependents (ReplicaSets, Pods, Jobs) are deleted with them by a
# replace; every other kind's dependents are orphaned. A StatefulSet is
# orphaned so a PVC retention policy can never delete claims.
_BACKGROUND_REPLACE_KINDS = frozenset(
    {"Deployment", "ReplicaSet", "DaemonSet", "Job", "CronJob"}
)


def replace_propagation(kind: str) -> str:
    """Deletion propagation a replace uses for ``kind``."""
    return "Background" if kind in _BACKGROUND_REPLACE_KINDS else "Orphan"


#: Kinds whose spec is immutable in parts (see :func:`immutable_changes`): a
#: release may also replace one it already manages, when named explicitly.
REPLACEABLE_MANAGED_KINDS = frozenset({"Job", "StatefulSet"})

# Spec fields the API server refuses to change on an existing object.
_IMMUTABLE_SPEC = {
    "Job": ("template", "completions", "completionMode", "selector"),
    "StatefulSet": (
        "selector",
        "serviceName",
        "podManagementPolicy",
        "volumeClaimTemplates",
    ),
}


def immutable_changes(
    desired: ResourceIntent,
    current: ObservedResource,
    removals: Iterable[str] = (),
) -> tuple[str, ...]:
    """Immutable spec fields (``spec.<field>``) that ``desired`` would change.

    A field changes when the desired value is not contained in the live one
    (the server may add defaults), or when a planned removal falls inside it.
    Kinds without immutable fields, and objects without a live spec, never
    report a change. Pure: no cluster access.
    """
    fields = _IMMUTABLE_SPEC.get(desired.ref.kind, ())
    live = current.intent.manifest.get("spec")
    wanted = desired.manifest.get("spec")
    if not fields or not isinstance(live, dict) or not isinstance(wanted, dict):
        return ()
    removed = set()
    for pointer in removals:
        parts = _removal_parts(pointer)
        if len(parts) > 1 and parts[0] == "spec":
            removed.add(parts[1])
    return tuple(
        f"spec.{field}"
        for field in fields
        if field in removed
        or (field in wanted and not manifest_contains(live.get(field), wanted[field]))
    )


def replace_refusal(resource: ObservedResource) -> str | None:
    """Why an observed object cannot be replaced, or ``None``.

    Unmanaged objects may be replaced; so may managed objects of a kind in
    :data:`REPLACEABLE_MANAGED_KINDS` (a Job or StatefulSet whose immutable
    fields change).
    """
    if resource.retained or resource.intent.ref.kind in _RETAINED_KINDS:
        return "retained objects are never deleted; adopt it instead"
    if (
        resource.ownership is not Ownership.UNMANAGED
        and resource.intent.ref.kind not in REPLACEABLE_MANAGED_KINDS
    ):
        return (
            "the object is already managed; replace applies only to unmanaged "
            "objects and to managed "
            + " or ".join(sorted(REPLACEABLE_MANAGED_KINDS))
            + " objects"
        )
    if resource.owner_uids:
        return "the object is owned by another object (ownerReferences)"
    return None


def adoption_for(
    desired: ResourceIntent,
    current: ObservedResource,
    field_manager: str | None = None,
) -> Adoption:
    """Choose the adoption mode from the live object; refuse unsafe ones."""
    if current.retained:
        # Only labels and annotations may differ: they are written by the
        # metadata-only patch. Spec and data must already match.
        if not retained_content_contained(desired, current):
            raise ValueError(
                "retained resource can only be adopted when the live object "
                "already contains the desired manifest (only metadata labels "
                f"and annotations may differ): {desired.ref}"
            )
        return Adoption(
            AdoptionMode.METADATA_ONLY,
            current.owner,
            metadata_changes=_public_metadata_changes(desired, current),
        )
    return Adoption(
        AdoptionMode.TAKEOVER,
        current.owner,
        transferable_managers(
            current.field_managers,
            exclude=() if field_manager is None else (field_manager,),
        ),
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


_METADATA_MAPS = ("labels", "annotations")


def without_metadata_maps(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Object content minus ``metadata.labels`` and ``metadata.annotations``.

    This is the part of a retained object that a metadata-only write never
    changes (spec, data, every other metadata field).
    """
    value = json.loads(_canonical_json(manifest))
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        for key in _METADATA_MAPS:
            metadata.pop(key, None)
    return dict(value)


def metadata_changes(
    desired: Mapping[str, Any], live: Mapping[str, Any]
) -> tuple[str, ...]:
    """Desired labels/annotations whose live value differs.

    Returned as ``labels/<key>`` and ``annotations/<key>``. A metadata-only
    write sets exactly these keys; it never removes a live key.
    """
    changes = []
    desired_metadata = desired.get("metadata")
    live_metadata = live.get("metadata")
    for section in _METADATA_MAPS:
        wanted = (
            desired_metadata.get(section)
            if isinstance(desired_metadata, Mapping)
            else None
        )
        actual = (
            live_metadata.get(section) if isinstance(live_metadata, Mapping) else None
        )
        if not isinstance(wanted, Mapping):
            continue
        actual = actual if isinstance(actual, Mapping) else {}
        for key, value in wanted.items():
            if section == "annotations" and key in _EXECUTION_ANNOTATIONS:
                continue
            if actual.get(key) != value:
                changes.append(f"{section}/{key}")
    return tuple(sorted(changes))


def metadata_patch(
    desired: Mapping[str, Any], changes: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """The ``{labels: {...}, annotations: {...}}`` values of ``changes``."""
    metadata = desired.get("metadata")
    patch: dict[str, dict[str, Any]] = {}
    for change in changes:
        section, _, key = change.partition("/")
        source = metadata.get(section) if isinstance(metadata, Mapping) else None
        if section not in _METADATA_MAPS or not isinstance(source, Mapping):
            raise ValueError(f"invalid metadata change: {change!r}")
        if key not in source:
            raise ValueError(f"invalid metadata change: {change!r}")
        patch.setdefault(section, {})[key] = source[key]
    return patch


def retained_content_contained(
    desired: ResourceIntent, current: ObservedResource
) -> bool:
    """The live retained object already holds everything but desired metadata.

    Private values are compared by the executor after resolution; the public
    plan never reveals whether a secret value matches.
    """
    expected = _without_pointers(
        desired.manifest,
        (binding.json_pointer for binding in desired.secret_bindings),
    )
    return manifest_contains(
        without_metadata_maps(current.intent.manifest),
        without_metadata_maps(expected),
    )


def _public_metadata_changes(
    desired: ResourceIntent, current: ObservedResource
) -> tuple[str, ...]:
    expected = _without_pointers(
        desired.manifest,
        (binding.json_pointer for binding in desired.secret_bindings),
    )
    return metadata_changes(expected, current.intent.manifest)


AUTOSCALER_KINDS = frozenset({("autoscaling", "HorizontalPodAutoscaler")})
_REPLICAS = {"spec": {"replicas": 0}}


def _group(api_version: str) -> str:
    return api_version.split("/")[0] if "/" in api_version else ""


@dataclass(frozen=True, order=True)
class AutoscaledReplicas:
    """How a plan treats ``spec.replicas`` of a workload an autoscaler targets.

    ``mode`` is one of:

    * ``initial``: the workload does not exist yet; the declared value (if
      any) is its initial size;
    * ``held``: the workload exists and no autoscaler has written the field
      yet (Piceli still owns it); the plan declares the **live** value, so a
      new release never changes the count and never removes the field;
    * ``yielded``: an autoscaler (a ``scale`` subresource or controller
      manager) owns the field; the plan does not declare it at all.
    """

    resource: ResourceRef
    autoscalers: tuple[str, ...]
    mode: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource": self.resource.__dict__,
            "field": "/spec/replicas",
            "autoscalers": list(self.autoscalers),
            "mode": self.mode,
        }


def _autoscaler_targets(
    composition: DeploymentComposition, snapshot: ObservedSnapshot
) -> dict[tuple[str, str, str, str], set[str]]:
    """``(group, kind, namespace, name)`` of each autoscaled workload -> HPAs.

    Autoscalers come from the composition and from the observed snapshot
    (when discovery covers their kind), so one created by another tool is
    honoured too.
    """
    autoscalers: dict[ResourceRef, dict[str, Any]] = {
        resource.intent.ref: resource.intent.manifest
        for resource in snapshot.resources
        if (_group(resource.intent.ref.api_version), resource.intent.ref.kind)
        in AUTOSCALER_KINDS
    }
    for component in composition.components:
        for intent in component.resources:
            if (_group(intent.ref.api_version), intent.ref.kind) in AUTOSCALER_KINDS:
                autoscalers[intent.ref] = intent.manifest
    targets: dict[tuple[str, str, str, str], set[str]] = {}
    for ref, manifest in autoscalers.items():
        spec = manifest.get("spec")
        target = spec.get("scaleTargetRef") if isinstance(spec, Mapping) else None
        if not isinstance(target, Mapping):
            continue
        key = (
            _group(str(target.get("apiVersion", ""))),
            str(target.get("kind", "")),
            ref.namespace,
            str(target.get("name", "")),
        )
        targets.setdefault(key, set()).add(f"{ref.kind}/{ref.name}")
    return targets


def autoscaled_replicas(
    composition: DeploymentComposition,
    snapshot: ObservedSnapshot,
    field_manager: str | None,
) -> tuple[DeploymentComposition, tuple[AutoscaledReplicas, ...]]:
    """Leave ``spec.replicas`` of autoscaled workloads to their autoscaler.

    A HorizontalPodAutoscaler writes ``spec.replicas`` through the ``scale``
    subresource. Declaring the field in a release would reset the count on
    every apply (a fight) and show a perpetual diff. For each workload an
    autoscaler targets (see :class:`AutoscaledReplicas` for the modes):

    * not live: the declared value is kept as the initial size;
    * live, and a manager other than ``field_manager`` that a takeover keeps
      (a subresource entry or a controller) owns the field: it is dropped
      from the desired manifest;
    * live otherwise: the live value is declared, so nothing changes and no
      three-way removal resets it.

    Idempotent: applying it to its own result changes nothing. Returns the
    composition to plan and one report per autoscaled workload.
    """
    targets = _autoscaler_targets(composition, snapshot)
    if not targets:
        return composition, ()
    observed = {resource.intent.ref: resource for resource in snapshot.resources}
    report: list[AutoscaledReplicas] = []
    components = []
    for component in composition.components:
        resources = []
        for intent in component.resources:
            ref = intent.ref
            autoscalers = targets.get(
                (_group(ref.api_version), ref.kind, ref.namespace, ref.name)
            )
            if not autoscalers:
                resources.append(intent)
                continue
            current = observed.get(ref)
            manifest = intent.manifest
            spec = manifest.get("spec")
            live_spec = None if current is None else current.intent.manifest.get("spec")
            if current is None or not isinstance(spec, dict):
                mode = "initial"
            elif any(
                entry.manager != field_manager
                and not is_transferable(entry)
                and _fields_overlap(entry.fields, _REPLICAS)
                for entry in current.field_managers
            ):
                mode = "yielded"
                spec.pop("replicas", None)
            elif isinstance(live_spec, Mapping) and "replicas" in live_spec:
                mode = "held"
                spec["replicas"] = live_spec["replicas"]
            else:
                mode = "initial"
            report.append(AutoscaledReplicas(ref, tuple(sorted(autoscalers)), mode))
            if manifest != intent.manifest:
                intent = ResourceIntent(
                    ref,
                    _canonical_json(manifest),
                    intent.dependencies,
                    intent.secret_bindings,
                )
            resources.append(intent)
        components.append(
            DeploymentComponent(
                component.name, tuple(resources), component.dependencies
            )
        )
    return DeploymentComposition(tuple(components)), tuple(sorted(report))


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
    composition, _ = autoscaled_replicas(composition, snapshot, field_manager)
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


_SCALABLE_KINDS = frozenset({"Deployment", "StatefulSet", "ReplicaSet"})


def autoscaled(composition: DeploymentComposition) -> frozenset[ResourceRef]:
    """Workloads a HorizontalPodAutoscaler of ``composition`` targets.

    Their ``spec.replicas`` belongs to the autoscaler: a plan never removes
    it (see :func:`build_plan`). Targets are matched by kind and name in the
    autoscaler's namespace.
    """
    resources = [
        resource
        for component in composition.components
        for resource in component.resources
    ]
    targets = set()
    for resource in resources:
        if resource.ref.kind != "HorizontalPodAutoscaler":
            continue
        target = resource.manifest.get("spec", {}).get("scaleTargetRef")
        if isinstance(target, dict):
            targets.add(
                (target.get("kind"), target.get("name"), resource.ref.namespace)
            )
    return frozenset(
        resource.ref
        for resource in resources
        if resource.ref.kind in _SCALABLE_KINDS
        and (resource.ref.kind, resource.ref.name, resource.ref.namespace) in targets
    )


def build_plan(
    composition: DeploymentComposition,
    snapshot: ObservedSnapshot,
    authorization: PlanAuthorization,
    *,
    private: PrivateEvidence | None = None,
) -> DeploymentPlan:
    """Build a deterministic, non-executable plan from supplied state only.

    ``private`` (see :func:`private_evidence`) lets a secret-bound object whose
    live content already matches plan as ``no-op``; without it such objects
    always plan as ``apply``.
    """
    if authorization.target != snapshot.target:
        raise ValueError("authorization target does not match observed target")
    composition, _ = autoscaled_replicas(
        composition, snapshot, authorization.field_manager
    )
    for ref in (*authorization.adopt_resources, *authorization.replace_resources):
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
    for ref in authorization.replace_resources:
        replaced = observed.get(ref)
        if ref not in desired or replaced is None:
            raise ValueError(
                f"replace authorization does not name an existing desired resource: {ref}"
            )
        refusal = replace_refusal(replaced)
        if refusal is not None:
            raise ValueError(f"cannot replace {ref}: {refusal}")
        if _propagation_deletes_protected(replaced, snapshot):
            raise ValueError(f"cannot replace {ref}: retained descendants exist")
    dependencies = _desired_dependencies(composition)
    levels = _topological_levels(dependencies)
    scaled = autoscaled(composition)
    actions: list[PlanAction] = []
    changes: tuple[str, ...]
    previous = {intent.ref: intent for intent in authorization.previous}
    for level in levels:
        for ref in level:
            current = observed.get(ref)
            removals: tuple[str, ...] = ()
            if current is None:
                if not snapshot.coverage.is_complete_for(ref.api_version, ref.kind):
                    raise ValueError(
                        f"cannot infer resource absence from incomplete discovery: {ref}"
                    )
                operation = PlanOperation.CREATE
                precondition = ResourcePrecondition(must_not_exist=True)
                adoption = None
                changes = ()
            else:
                if ref in snapshot.incomplete_content:
                    raise ValueError(
                        f"cannot compare resource with redacted or incomplete content: {ref}"
                    )
                precondition = current.precondition
                adoption = None
                changes = ()
                if ref in authorization.replace_resources:
                    operation = PlanOperation.REPLACE
                elif ref in authorization.adopt_resources:
                    operation = PlanOperation.ADOPT
                    adoption = adoption_for(
                        desired[ref], current, authorization.field_manager
                    )
                elif current.ownership is Ownership.UNMANAGED:
                    raise ValueError(f"resource requires explicit adoption: {ref}")
                elif not (
                    removals := tuple(
                        pointer
                        for pointer in planned_removals(
                            previous.get(ref),
                            desired[ref],
                            current,
                            authorization.field_manager,
                        )
                        # An autoscaler owns the replica count: never reset it.
                        if not (ref in scaled and pointer == "/spec/replicas")
                    )
                ) and (
                    _equivalent(desired[ref], current.intent, snapshot.defaulted_fields)
                    or server_equivalent(
                        desired[ref], current, snapshot.server_dry_run(ref)
                    )
                    or (private is not None and ref in private.matching)
                ):
                    operation = PlanOperation.NOOP
                else:
                    operation = PlanOperation.APPLY
                    changed = immutable_changes(desired[ref], current, removals)
                    if changed:
                        raise ValueError(
                            f"immutable fields of {ref} would change "
                            f"({', '.join(changed)}); name it with --replace "
                            f"{ref.kind}/{ref.name} to recreate it"
                        )
                    if current.retained:
                        # A retained object is never rewritten: only its
                        # labels and annotations may change, with a
                        # metadata-only write.
                        if not retained_content_contained(desired[ref], current):
                            raise ValueError(
                                "retained resource content differs from the live "
                                "object; only metadata labels and annotations can "
                                f"change on a retained object: {ref}"
                            )
                        changes = _public_metadata_changes(desired[ref], current)
            actions.append(
                PlanAction(
                    operation,
                    desired[ref],
                    tuple(sorted(dependencies[ref])),
                    precondition,
                    adoption,
                    changes,
                    removals
                    if operation is PlanOperation.APPLY and not changes
                    else (),
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
