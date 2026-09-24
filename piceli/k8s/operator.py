"""Piceli Operator core: reactive inventory, classification, and bounded operations.

Enforces clear distinctions:
- managed: declared by the active/catalogued Piceli release or tagged as managed by Piceli.
- unmanaged: present in the cluster namespace but not part of Piceli's declared release catalog.
- unknown: observation/access failed; never inferred as absent or license for mutation.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from piceli.k8s.observe import (
    InventoryReader,
    ObservationRef,
    ObservedObject,
    _COMMON_TYPES,
    archive_resources,
)
from piceli.k8s.ops.discovery import ResourceIdentity
from piceli.k8s.ops.session import DeploymentSessionArchive
from piceli.k8s.release import ReleaseCatalog, ReleaseRecord


_SECRET_REDACT_PATTERNS = [
    re.compile(r"(?i)((?:password|token|secret|key|authorization|bearer)\s*[:=]\s*)([^\s,;]+)"),
]


@dataclass(frozen=True)
class ManagedResource:
    """An observed cluster resource with explicit Piceli ownership classification."""

    ref: ObservationRef
    classification: str  # 'managed', 'unmanaged', 'unknown'
    state: str           # 'present', 'missing', 'unknown'
    release_name: str | None = None
    session_id: str | None = None
    observed: ObservedObject | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.classification not in {"managed", "unmanaged", "unknown"}:
            raise ValueError(f"invalid classification: {self.classification}")
        if self.state not in {"present", "missing", "unknown"}:
            raise ValueError(f"invalid state: {self.state}")


@dataclass(frozen=True)
class InventoryEvent:
    """A change event in cluster inventory for reactive stream / polling."""

    timestamp: float
    ref: ObservationRef
    event_type: str  # 'added', 'modified', 'deleted', 'unknown'
    classification: str
    details: dict[str, Any] = field(default_factory=dict)


class BoundedInventoryBuffer:
    """Thread-safe bounded queue of recent inventory events to support reactive UIs."""

    def __init__(self, capacity: int = 1000) -> None:
        self.capacity = capacity
        self._events: list[InventoryEvent] = []

    def record(self, event: InventoryEvent) -> None:
        self._events.append(event)
        if len(self._events) > self.capacity:
            self._events = self._events[-self.capacity :]

    def events_since(self, timestamp: float) -> list[InventoryEvent]:
        return [e for e in self._events if e.timestamp > timestamp]

    def clear(self) -> None:
        self._events.clear()


@dataclass(frozen=True)
class OperatorReport:
    """Comprehensive status report for the Piceli Operator."""

    namespace: str
    session_id: str | None
    active_release: str | None
    managed: tuple[ManagedResource, ...]
    unmanaged: tuple[ManagedResource, ...]
    unknown: tuple[ManagedResource, ...]
    releases: tuple[dict[str, Any], ...]
    scan_errors: tuple[str, ...] = ()
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "session_id": self.session_id,
            "active_release": self.active_release,
            "managed": [_resource_dict(r) for r in self.managed],
            "unmanaged": [_resource_dict(r) for r in self.unmanaged],
            "unknown": [_resource_dict(r) for r in self.unknown],
            "releases": list(self.releases),
            "scan_errors": list(self.scan_errors),
            "timestamp": self.timestamp,
        }


def _resource_dict(resource: ManagedResource) -> dict[str, Any]:
    res: dict[str, Any] = {
        "ref": asdict(resource.ref),
        "classification": resource.classification,
        "state": resource.state,
    }
    if resource.release_name:
        res["release_name"] = resource.release_name
    if resource.session_id:
        res["session_id"] = resource.session_id
    if resource.error:
        res["error"] = resource.error
    if resource.observed is not None:
        obs_dict: dict[str, Any] = {
            "uid": resource.observed.uid,
            "resource_version": resource.observed.resource_version,
            "phase": resource.observed.phase,
            "images": list(resource.observed.images),
        }
        if getattr(resource.observed, "labels", None):
            obs_dict["labels"] = dict(resource.observed.labels)
        if getattr(resource.observed, "annotations", None):
            obs_dict["annotations"] = dict(resource.observed.annotations)
        res["observed"] = obs_dict
    return res


def build_operator_report(
    reader: InventoryReader,
    namespace: str,
    *,
    catalog: ReleaseCatalog | None = None,
    session_archive: DeploymentSessionArchive | None = None,
    include_common_types: bool = True,
    managed_labels: Mapping[str, str] | None = None,
    revision_label: str | None = None,
) -> OperatorReport:
    """Construct a classified operator report distinguishing managed, unmanaged, and unknown objects.

    Live objects outside the archive count as managed when labelled
    ``piceli.io/managed=true`` or when they match every ``managed_labels``
    selector supplied by the consumer.  ``revision_label`` names the label that
    carries a release name when no catalog release is active.
    """
    declared_refs: set[ObservationRef] = set()
    session_id: str | None = None
    if session_archive is not None:
        session_id = session_archive.session_id
        declared_refs = set(archive_resources(session_archive))

    active_release_name: str | None = None
    releases_summary: list[dict[str, Any]] = []
    if catalog is not None:
        try:
            active_rec = catalog.selected()
            active_release_name = active_rec.name
        except (ValueError, KeyError, FileNotFoundError):
            active_rec = None

        for rec in catalog.records():
            releases_summary.append({
                "name": rec.name,
                "namespace": rec.namespace,
                "session_id": rec.archive.session_id,
                "kind": rec.source.kind,
                "identity": rec.source.identity,
                "artifact_digest": rec.source.artifact_digest,
                "is_active": (rec.name == active_release_name),
                
            })
            if session_archive is None and rec.name == active_release_name:
                session_id = rec.archive.session_id
                declared_refs = set(archive_resources(rec.archive))

    managed: list[ManagedResource] = []
    unknown: list[ManagedResource] = []
    scan_errors: list[str] = []

    # 1. Check declared objects
    for ref in sorted(declared_refs):
        try:
            obs = reader.get(ref)
        except Exception as error:
            unknown.append(
                ManagedResource(
                    ref=ref,
                    classification="unknown",
                    state="unknown",
                    session_id=session_id,
                    release_name=active_release_name,
                    error=f"{type(error).__name__}: {error}",
                )
            )
            continue

        if obs is None:
            managed.append(
                ManagedResource(
                    ref=ref,
                    classification="managed",
                    state="missing",
                    session_id=session_id,
                    release_name=active_release_name,
                )
            )
        else:
            managed.append(
                ManagedResource(
                    ref=ref,
                    classification="managed",
                    state="present",
                    session_id=session_id,
                    release_name=active_release_name,
                    observed=obs,
                )
            )

    # 2. Discover objects in namespace to find unmanaged or other managed objects
    unmanaged: list[ManagedResource] = []
    types_to_scan = set(_COMMON_TYPES) if include_common_types else set()
    types_to_scan.update((ref.api_version, ref.kind) for ref in declared_refs)

    for api_version, kind in sorted(types_to_scan):
        try:
            live_items = reader.list(api_version, kind, namespace)
        except Exception as error:
            scan_errors.append(f"{api_version}/{kind}: {type(error).__name__}")
            continue

        for item in live_items:
            if item.ref in declared_refs:
                continue
            item_labels = dict(getattr(item, "labels", ()))
            is_managed = item_labels.get("piceli.io/managed") == "true" or (
                managed_labels is not None
                and bool(managed_labels)
                and all(item_labels.get(k) == v for k, v in managed_labels.items())
            )
            if is_managed:
                managed.append(
                    ManagedResource(
                        ref=item.ref,
                        classification="managed",
                        state="present",
                        session_id=session_id,
                        release_name=active_release_name
                        or (item_labels.get(revision_label) if revision_label else None),
                        observed=item,
                    )
                )
            else:
                unmanaged.append(
                    ManagedResource(
                        ref=item.ref,
                        classification="unmanaged",
                        state="present",
                        observed=item,
                    )
                )

    return OperatorReport(
        namespace=namespace,
        session_id=session_id,
        active_release=active_release_name,
        managed=tuple(sorted(managed, key=lambda r: r.ref)),
        unmanaged=tuple(sorted(unmanaged, key=lambda r: r.ref)),
        unknown=tuple(sorted(unknown, key=lambda r: r.ref)),
        releases=tuple(releases_summary),
        scan_errors=tuple(sorted(scan_errors)),
    )


def redact_log_content(line: str, known_secrets: Sequence[str] = ()) -> str:
    """Redact sensitive tokens, passwords, and user-supplied secrets from a log line."""
    cleaned = line
    for secret in known_secrets:
        if secret and len(secret) >= 4:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    for pattern in _SECRET_REDACT_PATTERNS:
        cleaned = pattern.sub(r"\g<1>[REDACTED]", cleaned)
    return cleaned
