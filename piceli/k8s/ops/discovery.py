"""Portable, bounded Kubernetes discovery contract.

The contract contains no Kubernetes client imports. Providers are injected by
callers; capture is bounded and produces a secret-safe, target-bound artifact.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from typing import Any, Mapping, Protocol

from piceli.k8s.ops.bounds import (
    bounded_call,
    object_keys,
    positive,
    seconds,
    strict_json,
    text,
    timestamp,
)

DISCOVERY_SCHEMA_VERSION = 2
RETAINED_KINDS = frozenset(
    {"Namespace", "PersistentVolume", "PersistentVolumeClaim", "Secret"}
)

_SENSITIVE_KEY = re.compile(
    r"(^|_)(authorization|credential|password|private_key|secret|token)(_|$)", re.I
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _normalized_key(value: str) -> str:
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"[^a-z0-9]+", "_", snake.lower()).strip("_")


def _reference_fields(path: tuple[str, ...]) -> frozenset[str]:
    # Only Kubernetes reference locations are public; arbitrary similarly named
    # application configuration still follows the sensitive-value rules.
    if not path:
        return frozenset()
    roots = {
        "Pod": ("spec",),
        "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
        **{
            kind: ("spec", "template", "spec")
            for kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job")
        },
    }
    root = roots.get(path[0])
    if root is None or path[1 : 1 + len(root)] != root:
        return frozenset()
    path = path[1 + len(root) :]
    if path == ("volumes", "*", "secret"):
        return frozenset({"secretName", "optional", "defaultMode", "items"})
    if path == ("volumes", "*", "projected", "sources", "*", "secret"):
        return frozenset({"name", "optional", "items"})
    container = bool(path) and path[0] in {
        "containers",
        "initContainers",
        "ephemeralContainers",
    }
    if container and path[1:] == ("*", "env", "*", "valueFrom", "secretKeyRef"):
        return frozenset({"name", "key", "optional"})
    if container and path[1:] == ("*", "envFrom", "*", "secretRef"):
        return frozenset({"name", "optional"})
    if path == ("imagePullSecrets", "*"):
        return frozenset({"name"})
    return frozenset()


def _redact(value: Any, *, path: tuple[str, ...] = ()) -> tuple[Any, bool]:
    if isinstance(value, list):
        redacted_items = [_redact(item, path=path + ("*",)) for item in value]
        return [item for item, _ in redacted_items], any(
            changed for _, changed in redacted_items
        )
    if not isinstance(value, dict):
        return value, False
    result: dict[str, Any] = {}
    changed = False
    sensitive_env = _SENSITIVE_KEY.search(_normalized_key(str(value.get("name", ""))))
    for key, child in value.items():
        normalized = _normalized_key(str(key))
        child_path = path + (key,)
        reference = _reference_fields(child_path)
        sensitive = bool(
            _SENSITIVE_KEY.search(normalized)
        ) and key not in _reference_fields(path)
        if reference and isinstance(child, dict) and set(child) <= reference:
            sensitive = False
        if key == "value" and sensitive_env:
            sensitive = True
        if sensitive:
            result[key] = (
                {name: "<redacted>" for name in child}
                if isinstance(child, dict)
                else "<redacted>"
            )
            changed = True
        else:
            result[key], child_changed = _redact(child, path=child_path)
            changed = changed or child_changed
    return result, changed


def public_manifest(manifest: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Shared redaction for public plans, discovery and executor validation."""
    public, changed = _redact(manifest, path=(manifest.get("kind", ""),))
    if manifest.get("kind") == "Secret":
        for key in ("data", "stringData"):
            body = public.get(key)
            if isinstance(body, dict):
                public[key] = dict.fromkeys(body, "<redacted>")
                changed = True
    return public, changed


class ResourceScope(StrEnum):
    NAMESPACED = "namespaced"
    CLUSTER = "cluster"


class Ownership(StrEnum):
    MANAGED = "managed"
    UNMANAGED = "unmanaged"


@dataclass(frozen=True)
class PlanTarget:
    cluster_id: str
    namespace: str

    def __post_init__(self) -> None:
        text(self.cluster_id, "cluster_id")
        text(self.namespace, "namespace")
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", self.namespace):
            raise ValueError("invalid namespace")


@dataclass(frozen=True, order=True)
class ResourceType:
    api_version: str
    kind: str

    def __post_init__(self) -> None:
        text(self.api_version, "api_version")
        text(self.kind, "kind")
        if not re.fullmatch(
            r"(?:[a-z0-9.-]+/)?[a-z][a-z0-9]*", self.api_version
        ) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", self.kind):
            raise ValueError("invalid resource type")


@dataclass(frozen=True, order=True)
class ResourceIdentity:
    api_version: str
    kind: str
    namespace: str
    name: str

    def __post_init__(self) -> None:
        ResourceType(self.api_version, self.kind)
        text(self.namespace, "namespace", empty=True)
        text(self.name, "name")
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", self.name):
            raise ValueError("invalid resource name")


@dataclass(frozen=True, order=True)
class ApiResource:
    resource_type: ResourceType
    scope: ResourceScope
    plural: str

    def __post_init__(self) -> None:
        if not isinstance(self.resource_type, ResourceType) or not isinstance(
            self.scope, ResourceScope
        ):
            raise ValueError("invalid API resource type or scope")
        if not isinstance(self.plural, str) or not re.fullmatch(
            r"[a-z][a-z0-9]*", self.plural
        ):
            raise ValueError("API resource plural name is required")


class DiscoveryFailureKind(StrEnum):
    RBAC_DENIED = "rbac-denied"
    API_UNAVAILABLE = "api-unavailable"
    LIMIT_EXCEEDED = "limit-exceeded"
    INVALID_PAGE = "invalid-page"
    PROVIDER_ERROR = "provider-error"
    DEADLINE_EXCEEDED = "deadline-exceeded"


@dataclass(frozen=True, order=True)
class DiscoveryFailure:
    resource_type: ResourceType
    kind: DiscoveryFailureKind

    def __post_init__(self) -> None:
        if not isinstance(self.resource_type, ResourceType) or not isinstance(
            self.kind, DiscoveryFailureKind
        ):
            raise ValueError("invalid discovery failure")


@dataclass(frozen=True)
class DiscoveryLimits:
    max_resource_types: int = 64
    page_size: int = 100
    max_pages: int = 256
    max_resources: int = 10_000
    max_artifact_bytes: int = 10_000_000
    max_seconds: float = 30.0
    call_seconds: float = 5.0

    def __post_init__(self) -> None:
        for name, maximum in (
            ("max_resource_types", 256),
            ("page_size", 1000),
            ("max_pages", 4096),
            ("max_resources", 100_000),
            ("max_artifact_bytes", 10_000_000),
        ):
            positive(getattr(self, name), name, maximum)
        seconds(self.max_seconds, "capture")
        seconds(self.call_seconds, "call", self.max_seconds)


@dataclass(frozen=True)
class DiscoveryRequest:
    target: PlanTarget
    resource_types: tuple[ResourceType, ...]
    limits: DiscoveryLimits = DiscoveryLimits()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.target, PlanTarget)
            or not isinstance(self.limits, DiscoveryLimits)
            or not isinstance(self.resource_types, tuple)
            or any(not isinstance(item, ResourceType) for item in self.resource_types)
        ):
            raise ValueError("invalid discovery request")
        resource_types = tuple(sorted(set(self.resource_types)))
        if len(resource_types) != len(self.resource_types):
            raise ValueError("duplicate requested type")
        if not resource_types:
            raise ValueError("at least one resource type is required")
        if len(resource_types) > self.limits.max_resource_types:
            raise ValueError("requested resource types exceed discovery limit")
        object.__setattr__(self, "resource_types", resource_types)


@dataclass(frozen=True)
class ResourceListRequest:
    target: PlanTarget
    api_resource: ApiResource
    page_size: int
    continuation: str | None = None


@dataclass(frozen=True)
class DiscoveredResource:
    identity: ResourceIdentity
    _manifest_json: str = field(repr=False)
    ownership: Ownership = Ownership.UNMANAGED
    retained: bool | None = None
    content_complete: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ResourceIdentity) or not isinstance(
            self.ownership, Ownership
        ):
            raise ValueError("invalid discovered identity or ownership")
        if not isinstance(self.content_complete, bool) or (
            self.retained is not None and not isinstance(self.retained, bool)
        ):
            raise ValueError("invalid content or retention flag")
        manifest = strict_json(self._manifest_json)
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be an object")
        metadata = manifest.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("resource metadata is required")
        for key in ("uid", "resourceVersion", "name"):
            text(metadata.get(key), key)
        if not isinstance(metadata.get("annotations", {}), dict) or not isinstance(
            metadata.get("ownerReferences", []), list
        ):
            raise ValueError("invalid resource metadata")
        for owner in metadata.get("ownerReferences", []):
            if not isinstance(owner, dict):
                raise ValueError("invalid owner reference")
            text(owner.get("uid"), "owner UID")
        if (
            manifest.get("apiVersion"),
            manifest.get("kind"),
            metadata.get("namespace", ""),
            metadata.get("name"),
        ) != (
            self.identity.api_version,
            self.identity.kind,
            self.identity.namespace,
            self.identity.name,
        ):
            raise ValueError("discovered identity does not match manifest")
        retained = (
            self.identity.kind in RETAINED_KINDS
            or metadata.get("annotations", {}).get("piceli.io/retained") == "true"
        )
        if retained and self.retained is False:
            raise ValueError("retention protection cannot be disabled")
        object.__setattr__(self, "retained", bool(retained or self.retained))
        if "<redacted>" in self._manifest_json and self.content_complete:
            raise ValueError("redacted content cannot be complete")

    @classmethod
    def from_manifest(
        cls,
        manifest: Mapping[str, Any],
        *,
        scope: ResourceScope,
        ownership: Ownership = Ownership.UNMANAGED,
        retained: bool | None = None,
        content_complete: bool = True,
    ) -> DiscoveredResource:
        metadata = manifest.get("metadata")
        if not isinstance(metadata, Mapping) or not metadata.get("name"):
            raise ValueError("discovered resource metadata.name is required")
        api_version = manifest.get("apiVersion")
        kind = manifest.get("kind")
        if not isinstance(api_version, str) or not isinstance(kind, str):
            raise ValueError("discovered resource apiVersion and kind are required")
        if not isinstance(scope, ResourceScope):
            raise ValueError("invalid resource scope")
        namespace = metadata.get("namespace", "")
        if (scope is ResourceScope.NAMESPACED) != bool(namespace):
            raise ValueError("manifest namespace does not match scope")
        return cls(
            ResourceIdentity(api_version, kind, namespace, str(metadata["name"])),
            _canonical_json(manifest),
            ownership,
            retained,
            content_complete,
        )

    @property
    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    def public_dict(self) -> dict[str, Any]:
        manifest, redacted = public_manifest(self.manifest)
        return {
            "identity": {
                "api_version": self.identity.api_version,
                "kind": self.identity.kind,
                "namespace": self.identity.namespace,
                "name": self.identity.name,
            },
            "manifest": manifest,
            "ownership": self.ownership.value,
            "retained": self.retained,
            "content_complete": self.content_complete and not redacted,
        }

    @classmethod
    def from_public_dict(cls, value: Mapping[str, Any]) -> DiscoveredResource:
        object_keys(
            value, {"identity", "manifest", "ownership", "retained", "content_complete"}
        )
        if not isinstance(value["retained"], bool):
            raise ValueError("wire retention must be explicit")
        identity = value["identity"]
        if not isinstance(identity, Mapping):
            raise ValueError("resource identity must be an object")
        manifest = value["manifest"]
        if not isinstance(manifest, Mapping):
            raise ValueError("resource manifest must be an object")
        object_keys(identity, {"api_version", "kind", "namespace", "name"})
        return cls(
            ResourceIdentity(**identity),
            _canonical_json(manifest),
            Ownership(str(value["ownership"])),
            value["retained"],
            value["content_complete"],
        )


@dataclass(frozen=True, order=True)
class ApiDefaultedField:
    resource: ResourceIdentity
    json_pointer: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.resource, ResourceIdentity)
            or not isinstance(self.json_pointer, str)
            or not self.json_pointer.startswith("/")
            or re.search(r"~(?![01])", self.json_pointer)
        ):
            raise ValueError("defaulted field must be a JSON pointer")
        if self.json_pointer.split("/")[1] in {
            "apiVersion",
            "kind",
            "metadata",
            "data",
            "stringData",
            "status",
        }:
            raise ValueError(
                "default pointer cannot mask identity, secrets or runtime state"
            )


@dataclass(frozen=True)
class DiscoveryPage:
    target: PlanTarget
    api_resource: ApiResource
    resources: tuple[DiscoveredResource, ...] = ()
    defaulted_fields: tuple[ApiDefaultedField, ...] = ()
    continuation: str | None = None
    failure: DiscoveryFailureKind | None = None
    list_resource_version: str | None = None


class ApplyProbeStatus(StrEnum):
    ACCEPTED = "accepted"
    CONFLICT = "conflict"
    DENIED = "denied"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ApplyProbeResult:
    resource: ResourceIdentity
    status: ApplyProbeStatus
    conflicting_fields: tuple[str, ...] = ()


class ReadinessStatus(StrEnum):
    READY = "ready"
    NOT_READY = "not-ready"
    DENIED = "denied"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ReadinessProbeResult:
    resource: ResourceIdentity
    status: ReadinessStatus
    observed_generation: int | None = None


class DiscoveryProvider(Protocol):
    """Pure interface boundary implemented by a caller-owned provider adapter."""

    @property
    def provider_id(self) -> str:
        ...

    def discover_api_resource(
        self, target: PlanTarget, resource_type: ResourceType
    ) -> ApiResource | DiscoveryFailureKind:
        ...

    def list_resources(self, request: ResourceListRequest) -> DiscoveryPage:
        ...

    def probe_server_side_apply(
        self, target: PlanTarget, resource: DiscoveredResource
    ) -> ApplyProbeResult:
        ...

    def probe_readiness(
        self, target: PlanTarget, resource: ResourceIdentity
    ) -> ReadinessProbeResult:
        ...


@dataclass(frozen=True)
class DiscoveryCoverage:
    provider_id: str
    capture_id: str
    policy_revision: str
    requested: tuple[ResourceType, ...]
    completed: tuple[ResourceType, ...]
    api_resources: tuple[ApiResource, ...]
    failures: tuple[DiscoveryFailure, ...] = ()

    def __post_init__(self) -> None:
        for value in (self.provider_id, self.capture_id, self.policy_revision):
            text(value, "provenance identity")
        requested = tuple(sorted(set(self.requested)))
        completed = tuple(sorted(set(self.completed)))
        failures = tuple(sorted(set(self.failures)))
        for items in (
            self.requested,
            self.completed,
            self.api_resources,
            self.failures,
        ):
            if len(set(items)) != len(items):
                raise ValueError("duplicate coverage entry")
        api_types = [item.resource_type for item in self.api_resources]
        if len(set(api_types)) != len(api_types) or not set(api_types) <= set(
            requested
        ):
            raise ValueError("API metadata must uniquely describe requested types")
        if not set(completed) <= set(api_types):
            raise ValueError("completed coverage requires API metadata")
        if not {item.resource_type for item in failures} <= set(requested):
            raise ValueError("failure was not requested")
        if not set(completed).issubset(requested):
            raise ValueError("completed resource types must have been requested")
        if set(completed) & {failure.resource_type for failure in failures}:
            raise ValueError("resource types cannot be both complete and failed")
        object.__setattr__(self, "requested", requested)
        object.__setattr__(self, "completed", completed)
        object.__setattr__(
            self, "api_resources", tuple(sorted(set(self.api_resources)))
        )
        object.__setattr__(self, "failures", failures)

    @property
    def complete(self) -> bool:
        return (
            bool(self.requested)
            and set(self.completed) == set(self.requested)
            and not self.failures
        )

    def is_complete_for(self, api_version: str, kind: str) -> bool:
        return ResourceType(api_version, kind) in self.completed

    def identity_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "capture_id": self.capture_id,
            "policy_revision": self.policy_revision,
            "requested": [item.__dict__ for item in self.requested],
            "completed": [item.__dict__ for item in self.completed],
            "api_resources": [
                {
                    "resource_type": item.resource_type.__dict__,
                    "scope": item.scope.value,
                    "plural": item.plural,
                }
                for item in self.api_resources
            ],
            "failures": [
                {
                    "resource_type": item.resource_type.__dict__,
                    "kind": item.kind.value,
                }
                for item in self.failures
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DiscoveryCoverage:
        object_keys(
            value,
            {
                "provider_id",
                "capture_id",
                "policy_revision",
                "requested",
                "completed",
                "api_resources",
                "failures",
            },
        )
        for key in ("requested", "completed", "api_resources", "failures"):
            if not isinstance(value[key], list):
                raise ValueError("coverage entries must be arrays")

        def resource_type(item: Mapping[str, Any]) -> ResourceType:
            object_keys(item, {"api_version", "kind"})
            return ResourceType(**item)

        for item in value["api_resources"]:
            object_keys(item, {"resource_type", "scope", "plural"})
        for item in value["failures"]:
            object_keys(item, {"resource_type", "kind"})

        requested = tuple(resource_type(item) for item in value["requested"])
        completed = tuple(resource_type(item) for item in value["completed"])
        api_resources = tuple(
            ApiResource(
                resource_type(item["resource_type"]),
                ResourceScope(str(item["scope"])),
                item["plural"],
            )
            for item in value["api_resources"]
        )
        failures = tuple(
            DiscoveryFailure(
                resource_type(item["resource_type"]),
                DiscoveryFailureKind(str(item["kind"])),
            )
            for item in value["failures"]
        )
        return cls(
            value["provider_id"],
            value["capture_id"],
            value["policy_revision"],
            requested,
            completed,
            api_resources,
            failures,
        )


class EvidenceSource(StrEnum):
    SYNTHETIC = "synthetic"
    LOOPBACK = "loopback"
    LIVE = "live"


@dataclass(frozen=True)
class DiscoveryProvenance:
    """Source classification is an assertion, never a portable execution grant."""

    source: EvidenceSource = EvidenceSource.SYNTHETIC
    endpoint_id: str = "fixture"
    cluster_uid: str = ""
    namespace_uid: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source, EvidenceSource):
            raise ValueError("invalid evidence source")
        text(self.endpoint_id, "endpoint identity")
        for value in (self.cluster_uid, self.namespace_uid):
            text(
                value,
                "server target UID",
                empty=self.source is EvidenceSource.SYNTHETIC,
            )


@dataclass(frozen=True)
class DiscoveryArtifact:
    target: PlanTarget
    captured_at: str
    limits: DiscoveryLimits
    coverage: DiscoveryCoverage
    resources: tuple[DiscoveredResource, ...]
    defaulted_fields: tuple[ApiDefaultedField, ...] = ()
    schema_version: int = DISCOVERY_SCHEMA_VERSION
    provenance: DiscoveryProvenance = DiscoveryProvenance()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != DISCOVERY_SCHEMA_VERSION
        ):
            raise ValueError("unsupported discovery schema version")
        timestamp(self.captured_at)
        if (
            not isinstance(self.target, PlanTarget)
            or not isinstance(self.coverage, DiscoveryCoverage)
            or not isinstance(self.limits, DiscoveryLimits)
            or not isinstance(self.provenance, DiscoveryProvenance)
        ):
            raise ValueError("invalid artifact types")
        if len(self.coverage.requested) > self.limits.max_resource_types:
            raise ValueError("artifact exceeds resource type limit")
        resources = tuple(sorted(self.resources, key=lambda item: item.identity))
        if len({resource.identity for resource in resources}) != len(resources):
            raise ValueError("discovered resource identities must be unique")
        if len(resources) > self.limits.max_resources:
            raise ValueError("artifact exceeds resource limit")
        apis = {api.resource_type: api for api in self.coverage.api_resources}
        for resource in resources:
            api = apis.get(
                ResourceType(resource.identity.api_version, resource.identity.kind)
            )
            if api is None or resource.identity.namespace != (
                self.target.namespace if api.scope is ResourceScope.NAMESPACED else ""
            ):
                raise ValueError(
                    "resource type or namespace is outside discovered scope"
                )
        identities = {resource.identity for resource in resources}
        if len({resource.manifest["metadata"]["uid"] for resource in resources}) != len(
            resources
        ):
            raise ValueError("duplicate resource UID")
        if any(item.resource not in identities for item in self.defaulted_fields):
            raise ValueError("default pointer references absent resource")
        by_identity = {resource.identity: resource.manifest for resource in resources}
        for item in self.defaulted_fields:
            current: Any = by_identity[item.resource]
            try:
                for part in item.json_pointer[1:].split("/"):
                    part = part.replace("~1", "/").replace("~0", "~")
                    current = (
                        current[int(part)]
                        if isinstance(current, list) and part.isdigit()
                        else current[part]
                    )
            except (KeyError, IndexError, TypeError):
                raise ValueError("default pointer references absent value") from None
        if len(set(self.defaulted_fields)) != len(self.defaulted_fields):
            raise ValueError("duplicate default pointer")
        object.__setattr__(self, "resources", resources)
        object.__setattr__(
            self, "defaulted_fields", tuple(sorted(set(self.defaulted_fields)))
        )
        # Count private input as well as the public redacted encoding.
        private = self.to_dict()
        for item, resource in zip(private["resources"], resources):
            item["manifest"] = resource.manifest
        if len(_canonical_json(private).encode()) > self.limits.max_artifact_bytes:
            raise ValueError("discovery artifact exceeds byte limit")

    @property
    def execution_authoritative(self) -> bool:
        return (
            self.provenance.source is not EvidenceSource.SYNTHETIC
            and self.coverage.complete
            and all(resource.content_complete for resource in self.resources)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provenance": self.provenance.__dict__,
            "target": self.target.__dict__,
            "captured_at": self.captured_at,
            "limits": self.limits.__dict__,
            "coverage": self.coverage.identity_dict(),
            "resources": [resource.public_dict() for resource in self.resources],
            "defaulted_fields": [
                {
                    "resource": item.resource.__dict__,
                    "json_pointer": item.json_pointer,
                }
                for item in self.defaulted_fields
            ],
        }

    def to_json(self) -> str:
        encoded = _canonical_json(self.to_dict())
        if len(encoded.encode()) > self.limits.max_artifact_bytes:
            raise ValueError("discovery artifact exceeds byte limit")
        return encoded

    def to_private_json(self) -> str:
        """Serialize exact discovery for owner-only execution recovery.

        Unlike :meth:`to_json`, this representation retains resource bodies,
        including Secret data, so that a durable deployment can reconstruct
        the identical snapshot hash. Callers must keep it in private storage;
        it is never suitable for reports, logs, receipts, or interchange.
        """
        value = self.to_dict()
        for encoded, resource in zip(value["resources"], self.resources):
            encoded["manifest"] = resource.manifest
            encoded["content_complete"] = resource.content_complete
        result = _canonical_json(value)
        if len(result.encode()) > self.limits.max_artifact_bytes:
            raise ValueError("private discovery artifact exceeds byte limit")
        return result

    @classmethod
    def from_private_json(cls, encoded: str) -> DiscoveryArtifact:
        """Restore an exact owner-only discovery artifact without redaction."""
        value = strict_json(encoded)
        if not isinstance(value, Mapping):
            raise ValueError("private discovery artifact must be an object")
        result = cls.from_dict(value)
        if result.to_private_json() != encoded:
            raise ValueError("private discovery artifact must use canonical JSON")
        if not result.execution_authoritative:
            raise ValueError("private discovery artifact is not authoritative")
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DiscoveryArtifact:
        object_keys(
            value,
            {
                "schema_version",
                "target",
                "captured_at",
                "limits",
                "coverage",
                "resources",
                "defaulted_fields",
                "provenance",
            },
        )
        if (
            not isinstance(value["schema_version"], int)
            or isinstance(value["schema_version"], bool)
            or value["schema_version"] != DISCOVERY_SCHEMA_VERSION
        ):
            raise ValueError("unsupported discovery schema version")
        target_value = value["target"]
        limits_value = value["limits"]
        if not isinstance(target_value, Mapping) or not isinstance(
            limits_value, Mapping
        ):
            raise ValueError("target and limits must be objects")
        object_keys(target_value, {"cluster_id", "namespace"})
        object_keys(limits_value, set(DiscoveryLimits.__dataclass_fields__))
        target = PlanTarget(**target_value)
        limits = DiscoveryLimits(**limits_value)
        coverage_value = value["coverage"]
        if not isinstance(coverage_value, Mapping):
            raise ValueError("coverage must be an object")
        for key in ("resources", "defaulted_fields"):
            if not isinstance(value[key], list):
                raise ValueError("artifact entries must be arrays")
        for item in value["defaulted_fields"]:
            object_keys(item, {"resource", "json_pointer"})
            object_keys(item["resource"], {"api_version", "kind", "namespace", "name"})
        object_keys(
            value["provenance"],
            {"source", "endpoint_id", "cluster_uid", "namespace_uid"},
        )
        resources = tuple(
            DiscoveredResource.from_public_dict(item) for item in value["resources"]
        )
        defaulted_fields = tuple(
            ApiDefaultedField(
                ResourceIdentity(**item["resource"]), item["json_pointer"]
            )
            for item in value.get("defaulted_fields", ())
        )
        artifact = cls(
            target,
            value["captured_at"],
            limits,
            DiscoveryCoverage.from_dict(coverage_value),
            resources,
            defaulted_fields,
            provenance=DiscoveryProvenance(
                **(
                    value["provenance"]
                    | {"source": EvidenceSource(value["provenance"]["source"])}
                )
            ),
        )
        artifact.to_json()
        return artifact

    @classmethod
    def from_json(cls, value: str, *, max_bytes: int = 10_000_000) -> DiscoveryArtifact:
        decoded = strict_json(value, max_bytes)
        if not isinstance(decoded, Mapping):
            raise ValueError("discovery artifact must be an object")
        artifact = cls.from_dict(decoded)
        if len(value.encode()) > artifact.limits.max_artifact_bytes:
            raise ValueError("discovery input exceeds byte limit")
        return artifact


def capture_discovery(
    provider: DiscoveryProvider,
    request: DiscoveryRequest,
    *,
    capture_id: str,
    captured_at: str,
    policy_revision: str,
    deadline: float | None = None,
) -> DiscoveryArtifact:
    """Capture bounded pages without constructing or owning a provider client."""
    timestamp(captured_at)
    for value in (capture_id, policy_revision, provider.provider_id):
        text(value, "capture provenance")
    deadline = min(
        deadline if deadline is not None else float("inf"),
        time.monotonic() + request.limits.max_seconds,
    )
    provenance = getattr(provider, "provenance", DiscoveryProvenance())
    if provenance.source is not EvidenceSource.SYNTHETIC:
        verifier = getattr(provider, "verify_target", None)
        if verifier is None:
            raise ValueError(
                "non-synthetic capture requires observed target verification"
            )
        bounded_call(
            lambda: verifier(deadline=deadline),
            min(deadline, time.monotonic() + request.limits.call_seconds),
        )
    api_resources: list[ApiResource] = []
    failures: list[DiscoveryFailure] = []
    completed: list[ResourceType] = []
    resources: list[DiscoveredResource] = []
    defaulted_fields: list[ApiDefaultedField] = []
    total_pages = 0

    def artifact() -> DiscoveryArtifact:
        return DiscoveryArtifact(
            request.target,
            captured_at,
            request.limits,
            DiscoveryCoverage(
                provider.provider_id,
                capture_id,
                policy_revision,
                request.resource_types,
                tuple(completed),
                tuple(api_resources),
                tuple(failures),
            ),
            tuple(resources),
            tuple(defaulted_fields),
            provenance=provenance,
        )

    # Reserve enough space for terminal coverage and bounded failure accounting.
    if (
        len(_canonical_json(artifact().to_dict()).encode())
        + 512 * len(request.resource_types)
        > request.limits.max_artifact_bytes
    ):
        raise ValueError("discovery byte budget cannot hold coverage")

    for resource_type in request.resource_types:
        try:
            api_result = bounded_call(
                partial(provider.discover_api_resource, request.target, resource_type),
                min(deadline, time.monotonic() + request.limits.call_seconds),
            )
        except TimeoutError:
            api_result = DiscoveryFailureKind.DEADLINE_EXCEEDED
        except PermissionError:
            api_result = DiscoveryFailureKind.RBAC_DENIED
        except Exception:
            api_result = DiscoveryFailureKind.PROVIDER_ERROR
        if isinstance(api_result, DiscoveryFailureKind):
            failures.append(DiscoveryFailure(resource_type, api_result))
            continue
        if (
            not isinstance(api_result, ApiResource)
            or api_result.resource_type != resource_type
        ):
            failures.append(
                DiscoveryFailure(resource_type, DiscoveryFailureKind.INVALID_PAGE)
            )
            continue
        api_resources.append(api_result)
        continuation: str | None = None
        seen_continuations: set[str | None] = set()
        list_version: str | None = None
        type_failed = False
        while True:
            if total_pages >= request.limits.max_pages:
                failures.append(
                    DiscoveryFailure(resource_type, DiscoveryFailureKind.LIMIT_EXCEEDED)
                )
                type_failed = True
                break
            if continuation in seen_continuations:
                failures.append(
                    DiscoveryFailure(resource_type, DiscoveryFailureKind.INVALID_PAGE)
                )
                type_failed = True
                break
            seen_continuations.add(continuation)
            page_request = ResourceListRequest(
                request.target,
                api_result,
                request.limits.page_size,
                continuation,
            )
            try:
                page = bounded_call(
                    partial(provider.list_resources, page_request),
                    min(deadline, time.monotonic() + request.limits.call_seconds),
                )
            except TimeoutError:
                failures.append(
                    DiscoveryFailure(
                        resource_type, DiscoveryFailureKind.DEADLINE_EXCEEDED
                    )
                )
                type_failed = True
                break
            except PermissionError:
                failures.append(
                    DiscoveryFailure(resource_type, DiscoveryFailureKind.RBAC_DENIED)
                )
                type_failed = True
                break
            except Exception:
                failures.append(
                    DiscoveryFailure(resource_type, DiscoveryFailureKind.PROVIDER_ERROR)
                )
                type_failed = True
                break
            total_pages += 1
            if (
                not isinstance(page, DiscoveryPage)
                or page.target != request.target
                or page.api_resource != api_result
                or len(page.resources) > request.limits.page_size
                or (
                    page.continuation is not None
                    and (
                        not isinstance(page.continuation, str)
                        or not page.continuation
                        or len(page.continuation) > 4096
                    )
                )
                or (
                    list_version is not None
                    and page.list_resource_version != list_version
                )
                or (
                    page.failure is None
                    and provenance.source is not EvidenceSource.SYNTHETIC
                    and not page.list_resource_version
                )
            ):
                failures.append(
                    DiscoveryFailure(resource_type, DiscoveryFailureKind.INVALID_PAGE)
                )
                type_failed = True
                break
            list_version = page.list_resource_version
            if page.failure is not None:
                failures.append(DiscoveryFailure(resource_type, page.failure))
                type_failed = True
                break
            if len(resources) + len(page.resources) > request.limits.max_resources:
                failures.append(
                    DiscoveryFailure(resource_type, DiscoveryFailureKind.LIMIT_EXCEEDED)
                )
                type_failed = True
                break
            previous_resources = len(resources)
            previous_defaults = len(defaulted_fields)
            resources.extend(page.resources)
            defaulted_fields.extend(page.defaulted_fields)
            try:
                candidate = artifact()
                private_bytes = sum(
                    len(item._manifest_json.encode()) for item in resources
                )
                if (
                    private_bytes
                    + len(candidate.to_json().encode())
                    + 512 * len(request.resource_types)
                    > request.limits.max_artifact_bytes
                ):
                    raise OverflowError
            except (ValueError, TypeError, AttributeError, OverflowError) as error:
                del resources[previous_resources:]
                del defaulted_fields[previous_defaults:]
                kind = (
                    DiscoveryFailureKind.LIMIT_EXCEEDED
                    if isinstance(error, OverflowError) or "byte limit" in str(error)
                    else DiscoveryFailureKind.INVALID_PAGE
                )
                failures.append(DiscoveryFailure(resource_type, kind))
                type_failed = True
                break
            continuation = page.continuation
            if continuation is None:
                break
        if not type_failed:
            completed.append(resource_type)

    # A namespace or cluster can be replaced while a paginated capture is running.
    if provenance.source is not EvidenceSource.SYNTHETIC:
        assert verifier is not None
        bounded_call(
            lambda: verifier(deadline=deadline),
            min(deadline, time.monotonic() + request.limits.call_seconds),
        )
    return artifact()
