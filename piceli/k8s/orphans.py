"""Leftover objects: report the objects no current release owns, and prune them.

A release only ever touches the objects it declares (and, with ``[release]
prune``, the ones a previous release of the same owner declared). Objects that
outlive their release stay in the namespace: a component removed in a later
release, or the objects of another environment of the same app. This module
finds them and, with an approval of the exact set, deletes them.

An object is a **candidate** only when Piceli wrote it (a non-empty
``piceli.io/owner`` annotation) and it is not declared by the current
(selected) release. It is reported when

* its owner is this release's owner (or an inherited owner): reason
  ``not-in-current-release``, matched on ``piceli.io/owner``; or
* it carries every label the current release's objects share (the app's
  ownership labels, ``app.kubernetes.io/part-of`` by default) and another
  owner: reason ``other-owner`` (another environment of the app).

Never a candidate: an object without Piceli's owner annotation, an object with
``ownerReferences`` (its owner's garbage collection decides), an object that is
already terminating, and anything outside the target namespace. Some
candidates are reported but **not prunable** unless the matching flag is given:
claims (``--include-claims``, also a StatefulSet whose retention policy
deletes its claims), Secrets (``--include-secrets``), objects of another owner
(``--include-other-owners``); cluster-scoped objects are only scanned with
``--include-cluster-scoped``. A ``piceli.io/retained`` object is never
prunable.

Pure functions plus one provider call (:func:`delete_orphans`); no cluster
client is built here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from piceli.k8s.ops.discovery import (
    RELEASE_NAMESPACE_ANNOTATION,
    DiscoveredResource,
    PlanTarget,
    ResourceIdentity,
    ResourceType,
)
from piceli.k8s.ops.kubernetes_provider import KubernetesProvider, ProviderError
from piceli.k8s.ops.plan import DeploymentComposition

OWNER_ANNOTATION = "piceli.io/owner"
RETAIN_ANNOTATION = "piceli.io/retained"

REASON_REMOVED = "not-in-current-release"
REASON_OTHER_OWNER = "other-owner"

#: Namespaced kinds scanned besides the kinds of the catalogued releases.
SCANNED_KINDS: tuple[tuple[str, str], ...] = (
    ("v1", "ConfigMap"),
    ("v1", "Secret"),
    ("v1", "PersistentVolumeClaim"),
    ("v1", "ServiceAccount"),
    ("v1", "Service"),
    ("apps/v1", "Deployment"),
    ("apps/v1", "StatefulSet"),
    ("apps/v1", "DaemonSet"),
    ("batch/v1", "Job"),
    ("batch/v1", "CronJob"),
    ("networking.k8s.io/v1", "Ingress"),
    ("networking.k8s.io/v1", "NetworkPolicy"),
    ("autoscaling/v2", "HorizontalPodAutoscaler"),
    ("policy/v1", "PodDisruptionBudget"),
    ("rbac.authorization.k8s.io/v1", "Role"),
    ("rbac.authorization.k8s.io/v1", "RoleBinding"),
)
#: Cluster-scoped kinds scanned with ``--include-cluster-scoped``.
CLUSTER_KINDS: tuple[tuple[str, str], ...] = (
    ("rbac.authorization.k8s.io/v1", "ClusterRole"),
    ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding"),
)

#: Deletion order: the things that use other objects go first.
_DELETE_ORDER = (
    "Ingress",
    "HTTPRoute",
    "HorizontalPodAutoscaler",
    "PodDisruptionBudget",
    "NetworkPolicy",
    "Service",
    "CronJob",
    "Job",
    "DaemonSet",
    "StatefulSet",
    "Deployment",
    "RoleBinding",
    "ClusterRoleBinding",
    "Role",
    "ClusterRole",
    "ServiceAccount",
    "ConfigMap",
    "PersistentVolumeClaim",
    "Secret",
)


@dataclass(frozen=True)
class OrphanOptions:
    """What a prune may include beyond the safe default."""

    include_claims: bool = False
    include_secrets: bool = False
    include_cluster_scoped: bool = False
    include_other_owners: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "include_claims": self.include_claims,
            "include_secrets": self.include_secrets,
            "include_cluster_scoped": self.include_cluster_scoped,
            "include_other_owners": self.include_other_owners,
        }

    def kinds(self) -> tuple[tuple[str, str], ...]:
        return SCANNED_KINDS + (CLUSTER_KINDS if self.include_cluster_scoped else ())


@dataclass(frozen=True)
class Orphan:
    """One leftover object: what it is, why it is unowned, and if it may go."""

    api_version: str
    kind: str
    namespace: str
    name: str
    uid: str
    resource_version: str
    matched: tuple[str, ...]
    owner: str
    reason: str
    created_at: str | None
    age_seconds: int | None
    blocked_by: tuple[str, ...] = ()

    @property
    def prunable(self) -> bool:
        return not self.blocked_by

    @property
    def identity(self) -> ResourceIdentity:
        return ResourceIdentity(self.api_version, self.kind, self.namespace, self.name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_version": self.api_version,
            "kind": self.kind,
            "name": self.name,
            "namespace": self.namespace,
            "scope": "namespaced" if self.namespace else "cluster",
            "uid": self.uid,
            "resource_version": self.resource_version,
            "matched": list(self.matched),
            "owner": self.owner,
            "reason": self.reason,
            "created_at": self.created_at,
            "age_seconds": self.age_seconds,
            "prunable": self.prunable,
            "blocked_by": list(self.blocked_by),
        }


def app_labels(composition: DeploymentComposition) -> dict[str, str]:
    """The labels every object of ``composition`` carries: the app's ownership labels."""
    common: dict[str, str] | None = None
    for component in composition.components:
        for resource in component.resources:
            labels = (resource.manifest.get("metadata") or {}).get("labels") or {}
            pairs = {
                str(key): str(value)
                for key, value in labels.items()
                if isinstance(key, str) and isinstance(value, str)
            }
            common = (
                pairs
                if common is None
                else {k: v for k, v in common.items() if pairs.get(k) == v}
            )
    return dict(sorted((common or {}).items()))


def declared_keys(composition: DeploymentComposition) -> set[tuple[str, str, str]]:
    """``(kind, namespace, name)`` of every object the composition declares."""
    return {
        (resource.ref.kind, resource.ref.namespace or "", resource.ref.name)
        for component in composition.components
        for resource in component.resources
    }


def scan_types(
    compositions: Iterable[DeploymentComposition], options: OrphanOptions
) -> set[ResourceType]:
    """Kinds to discover: the scanned set plus what catalogued releases declare."""
    kinds = {ResourceType(*item) for item in options.kinds()}
    for composition in compositions:
        for component in composition.components:
            for resource in component.resources:
                ref = resource.ref
                if not ref.namespace and not options.include_cluster_scoped:
                    continue
                kinds.add(ResourceType(ref.api_version, ref.kind))
    return kinds


def _age(created: Any, now: datetime) -> tuple[str | None, int | None]:
    if not isinstance(created, str) or not created:
        return None, None
    try:
        moment = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return created, None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return created, max(0, int((now - moment).total_seconds()))


def _blocked(
    resource: DiscoveredResource,
    manifest: Mapping[str, Any],
    reason: str,
    options: OrphanOptions,
) -> tuple[str, ...]:
    kind = resource.identity.kind
    metadata = manifest["metadata"]
    blocked: list[str] = []
    if (metadata.get("annotations") or {}).get(RETAIN_ANNOTATION) == "true":
        blocked.append("retained")
    if kind == "PersistentVolumeClaim" and not options.include_claims:
        blocked.append("claims: needs --include-claims")
    if kind == "Secret" and not options.include_secrets:
        blocked.append("secrets: needs --include-secrets")
    if reason == REASON_OTHER_OWNER and not options.include_other_owners:
        blocked.append("other-owner: needs --include-other-owners")
    if kind == "StatefulSet" and not options.include_claims:
        policy = (manifest.get("spec") or {}).get(
            "persistentVolumeClaimRetentionPolicy"
        ) or {}
        if policy.get("whenDeleted") == "Delete":
            blocked.append(
                "claims: its retention policy deletes the claims; "
                "needs --include-claims"
            )
    return tuple(blocked)


def find_orphans(
    resources: Iterable[DiscoveredResource],
    *,
    declared: set[tuple[str, str, str]],
    owner_ids: Sequence[str],
    labels: Mapping[str, str],
    namespace: str,
    options: OrphanOptions,
    now: datetime,
) -> tuple[Orphan, ...]:
    """The leftover objects among ``resources``, sorted by kind and name."""
    owners = set(owner_ids)
    found: list[Orphan] = []
    for resource in resources:
        identity = resource.identity
        manifest = resource.manifest
        metadata = manifest["metadata"]
        key = (identity.kind, identity.namespace, identity.name)
        if key in declared or identity.namespace not in {"", namespace}:
            continue
        if metadata.get("ownerReferences") or metadata.get("deletionTimestamp"):
            continue
        annotations = metadata.get("annotations") or {}
        owner = annotations.get(OWNER_ANNOTATION)
        if not isinstance(owner, str) or not owner:
            continue
        if not identity.namespace and (
            not options.include_cluster_scoped
            or annotations.get(RELEASE_NAMESPACE_ANNOTATION) != namespace
        ):
            continue
        matched: list[str] = []
        if owner in owners:
            matched.append(OWNER_ANNOTATION)
        object_labels = metadata.get("labels") or {}
        if labels and all(object_labels.get(k) == v for k, v in labels.items()):
            matched.extend(f"{k}={v}" for k, v in labels.items())
        if not matched:
            continue
        reason = REASON_REMOVED if owner in owners else REASON_OTHER_OWNER
        created, age = _age(metadata.get("creationTimestamp"), now)
        found.append(
            Orphan(
                identity.api_version,
                identity.kind,
                identity.namespace,
                identity.name,
                str(metadata["uid"]),
                str(metadata["resourceVersion"]),
                tuple(matched),
                owner,
                reason,
                created,
                age,
                _blocked(resource, manifest, reason, options),
            )
        )
    return tuple(sorted(found, key=lambda o: (o.kind, o.namespace, o.name)))


def prune_hash(
    target: PlanTarget,
    release: str,
    options: OrphanOptions,
    orphans: Iterable[Orphan],
) -> str:
    """Digest of the exact set a prune would delete (UIDs and resourceVersions)."""
    payload = {
        "schema": "piceli.orphans/v1",
        "target": {"cluster_id": target.cluster_id, "namespace": target.namespace},
        "release": release,
        "options": options.to_dict(),
        "objects": [
            [o.api_version, o.kind, o.namespace, o.name, o.uid, o.resource_version]
            for o in orphans
            if o.prunable
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def deletion_order(orphans: Iterable[Orphan]) -> list[Orphan]:
    rank = {kind: index for index, kind in enumerate(_DELETE_ORDER)}
    return sorted(
        (o for o in orphans if o.prunable),
        key=lambda o: (rank.get(o.kind, len(rank)), o.namespace, o.name),
    )


def delete_orphans(
    provider: KubernetesProvider, orphans: Iterable[Orphan]
) -> list[dict[str, str]]:
    """Delete the prunable ``orphans`` with UID and resourceVersion preconditions.

    Returns one ``{"kind", "name", "outcome"}`` per object: ``deleted``,
    ``gone`` (already absent) or ``failed`` (with ``category``: an allowlisted
    provider category, never a server message).
    """
    results: list[dict[str, str]] = []
    for item in deletion_order(orphans):
        entry = {"kind": item.kind, "name": item.name}
        try:
            provider.delete(
                item.identity,
                uid=item.uid,
                resource_version=item.resource_version,
                propagation="Background",
                allow_retained=True,
            )
            results.append({**entry, "outcome": "deleted"})
        except ProviderError as error:
            if error.category == "not-found":
                results.append({**entry, "outcome": "gone"})
            else:
                results.append(
                    {**entry, "outcome": "failed", "category": error.category}
                )
    return results
