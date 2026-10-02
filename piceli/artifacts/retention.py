"""Registry retention: which manifests a registry keeps, and deleting the rest.

``piceli artifacts retention`` reads a registry (the node-local registry Piceli
delivers to, or any OCI registry reachable with credentials) and decides, per
manifest, whether it is **kept** or **collectable**:

* the last ``keep`` **releases** (newest first; from the publish and delivery
  receipts you give it) keep every digest they name (``keep=0``: none, only
  with a live inventory read from the cluster);
* **pinned** digests (``--pin``, ``--pin-file``) are always kept;
* **live** digests, read from what runs or is referenced now: pods that have
  not finished, the pod templates of Deployments, StatefulSets, DaemonSets,
  CronJobs, unfinished Jobs and ReplicaSets that are not a Deployment's
  scaled-down rollout history, the environment records and pushed images of
  branch environments, and the GitOps controller's status (or a file): a
  digest a running workload uses is never deleted;
* **rollback targets**: the previous release of every Deployment and
  StatefulSet (its newest scaled-down ReplicaSet, or its newest
  ControllerRevision that is not the current one), except Piceli's own
  controller, UI and registry, whose old copies are collectable;
* the children of a kept index, and the referrers (SBOM, provenance,
  signatures) of a kept manifest, are kept with it;
* a tagged manifest that no receipt mentions is **unledgered**: kept unless
  ``collect_unledgered`` is set (conservative default).

With a ``budget`` more releases than ``keep`` are kept, newest first, while the
deduplicated bytes of everything kept stay within it (never fewer than
``keep`` releases, never less than the pinned and live digests).

Besides tags and receipts, the inventory probes every digest the cluster
still names (finished pods and Jobs, rollout history included) in the
repository its reference names: a manifest pushed by digest that no receipt
mentions is found that way and, when nothing live uses it, collected.

For the in-cluster registry (``Registry.in_cluster``) the inventory can also
list every manifest the registry stores (``stored``, read from its storage:
:mod:`piceli.artifacts.registry_storage`), so a digest-only manifest that
nothing names any more (an old builder) is found and collected too.

Deleting is approval-gated: the plan hash covers the exact ``(repository,
digest)`` list, and a delete whose freshly computed plan differs is refused.
The registry frees blob space only when *its* garbage collector runs (for a
Piceli-managed node-local registry: ``garbage_collection(delete_untagged=False)``).

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from piceli.artifacts.registry import (
    RegistryEndpoint,
    RegistryError,
    RegistryTarget,
    StreamedOciRegistryClient,
)

PLAN_SCHEMA = "piceli.registry-retention.v1"
INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_FALLBACK_TAG = re.compile(r"sha256-([0-9a-f]{64})(?:\.[A-Za-z0-9._-]+)?")
_IMAGE_ID = re.compile(r"(?:^|[/@=])(?P<digest>sha256:[0-9a-f]{64})")
_MAX_MANIFESTS = 20_000

ClientFactory = Callable[[RegistryEndpoint, str], StreamedOciRegistryClient]


def default_client(
    endpoint: RegistryEndpoint, actions: str
) -> StreamedOciRegistryClient:
    """A client scoped to ``actions`` (``pull`` to read, ``pull,delete`` to delete)."""
    return StreamedOciRegistryClient(endpoint, actions=actions)


class RetentionError(RuntimeError):
    """A retention refusal with a registered error ``code`` (never a detail)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class StableTarget:
    """A registry by its stable name (``name.namespace.svc:port``), read through
    another endpoint (a port-forward): a plan binds to the name, never to the
    forwarded port. ``repository`` ``""`` is the whole registry."""

    registry: str
    repository: str = ""
    tls: bool = False

    @property
    def identity(self) -> str:
        tls = "true" if self.tls else "false"
        return f"oci://{self.registry}/{self.repository}?tls={tls}"

    def public(self) -> dict[str, object]:
        return {
            "registry": self.registry,
            "repository": self.repository or None,
            "tag": None,
            "tls": self.tls,
        }


Target = RegistryTarget | StableTarget


# --------------------------------------------------------------------- policy


@dataclass(frozen=True)
class RetentionPolicy:
    """What to keep. ``keep`` releases at least; ``budget`` bytes at most for the
    releases beyond the minimum (``None``: exactly ``keep``)."""

    keep: int = 3
    budget: int | None = None
    pins: frozenset[str] = frozenset()
    collect_unledgered: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.keep, bool) or not isinstance(self.keep, int):
            raise RetentionError(code="retention-invalid")
        if self.keep < 0:
            raise RetentionError(code="retention-invalid")
        if self.budget is not None and (
            isinstance(self.budget, bool)
            or not isinstance(self.budget, int)
            or self.budget < 0
        ):
            raise RetentionError(code="retention-invalid")
        for pin in self.pins:
            if not isinstance(pin, str) or not _DIGEST.fullmatch(pin):
                raise RetentionError(code="retention-invalid")

    def public(self) -> dict[str, Any]:
        return {
            "keep": self.keep,
            "budget_bytes": self.budget,
            "pins": sorted(self.pins),
            "collect_unledgered": self.collect_unledgered,
        }


_UNITS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}


def parse_size(text: str) -> int:
    """``10GiB``, ``500MB``, ``1024`` (bytes) to a byte count."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*", text or "")
    if not match:
        raise RetentionError(code="retention-invalid")
    unit = (match.group(2) or "b").lower()
    if unit not in _UNITS:
        raise RetentionError(code="retention-invalid")
    return int(float(match.group(1)) * _UNITS[unit])


def read_pins(path: Path) -> frozenset[str]:
    """Digests from a file: one per line, ``#`` comments and blank lines ignored."""
    pins = set()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            pins.add(line)
    return frozenset(pins)


# ------------------------------------------------------------------- releases


@dataclass(frozen=True)
class Release:
    """One release of the target: when it finished and which digests it names."""

    label: str
    finished_at: str
    digests: tuple[tuple[str, str], ...]  # (repository, digest)

    def public(self) -> dict[str, Any]:
        return {
            "release": self.label,
            "finished_at": self.finished_at,
            "digests": [digest for _, digest in self.digests],
        }


def _release_from(document: Any, label: str) -> Release | None:
    """A release from a publish or a registry-delivery receipt, else ``None``."""
    if not isinstance(document, dict):
        return None
    found: list[tuple[str, str]] = []
    if document.get("state") == "published" and isinstance(
        document.get("images"), dict
    ):
        registry = str(document.get("registry", ""))
        for entry in document["images"].values():
            repository = str(entry.get("repository", ""))
            if registry and repository.startswith(registry + "/"):
                repository = repository[len(registry) + 1 :]
            digest = entry.get("digest")
            if isinstance(digest, str) and _DIGEST.fullmatch(digest):
                found.append((repository, digest))
    elif document.get("state") == "succeeded" and isinstance(
        document.get("image"), dict
    ):
        target = document.get("target")
        digest = document["image"].get("manifest_digest")
        if (
            isinstance(target, dict)
            and isinstance(target.get("repository"), str)
            and isinstance(digest, str)
            and _DIGEST.fullmatch(digest)
        ):
            found.append((target["repository"], digest))
    if not found:
        return None
    finished = document.get("finished_at") or document.get("started_at") or ""
    return Release(label, str(finished), tuple(sorted(set(found))))


def load_releases(paths: Sequence[Path]) -> list[Release]:
    """Releases from receipt files, JSON Lines journals or directories of them.

    Publish receipts and registry-delivery receipts count; failed or unknown
    documents are skipped. Receipts name a repository, never a host: a
    receipt for ``app/api`` protects ``app/api`` wherever the registry is.
    Newest first (by ``finished_at``, then argument order).
    """
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(
                sorted(
                    item
                    for item in path.iterdir()
                    if item.is_file() and item.suffix in {".json", ".jsonl"}
                )
            )
        else:
            files.append(path)
    releases: list[tuple[str, int, Release]] = []
    for index, file in enumerate(files):
        try:
            text = file.read_text()
        except OSError as error:
            raise RetentionError(code="retention-invalid") from error
        lines = text.splitlines() if file.suffix == ".jsonl" else [text]
        for number, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                document = json.loads(line)
            except ValueError as error:
                raise RetentionError(code="retention-invalid") from error
            release = _release_from(document, f"{file.name}#{number}")
            if release is not None:
                releases.append((release.finished_at, index * 100000 + number, release))
    releases.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in releases]


def generations(releases: Sequence[Release]) -> list[Release]:
    """Regroup receipts (newest first) into releases, per repository.

    A delivery receipt names one image, a publish receipt several, and an image
    that did not change has no receipt in a release. So "release k" is, for
    every repository, its k-th newest distinct digest: ``--keep 3`` keeps each
    repository's last three digests, and the releases of several images that
    change together line up.
    """
    per_repo: dict[str, list[tuple[str, str, str]]] = {}
    for release in releases:
        for repository, digest in release.digests:
            rows = per_repo.setdefault(repository, [])
            if all(row[2] != digest for row in rows):
                rows.append((release.finished_at, release.label, digest))
    grouped: list[Release] = []
    for index in range(max((len(rows) for rows in per_repo.values()), default=0)):
        members = [
            (repository, rows[index])
            for repository, rows in sorted(per_repo.items())
            if len(rows) > index
        ]
        newest = max(members, key=lambda item: item[1][0])[1]
        grouped.append(
            Release(
                newest[1],
                newest[0],
                tuple(sorted((repo, row[2]) for repo, row in members)),
            )
        )
    return grouped


# ------------------------------------------------------------ live workloads

WORKLOAD_KINDS = (
    "Pod",
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "ReplicaSet",
    "Job",
    "CronJob",
    "ControllerRevision",
)


def _pod_specs(item: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The pod spec of a Pod, or the pod template's spec of a workload."""
    if item.get("kind") == "ControllerRevision":
        data = item.get("data") or {}
        template = (data.get("spec") or {}).get("template") or {}
        return [template.get("spec") or {}]
    spec = item.get("spec") or {}
    if item.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    if item.get("kind") not in {None, "Pod"}:
        spec = (spec.get("template") or {}).get("spec") or {}
    return [spec]


#: Piceli ConfigMaps whose images are live: an environment's record, the
#: images ``env push`` recorded for a branch, and the GitOps controller status.
ENV_RECORD = "piceli-env"
ENV_PUSHED_PREFIX = "piceli-env-"
CONTROLLER_STATUS = "piceli-gitops-status"
#: The GitOps controller's configuration: the builder image it runs now.
CONTROLLER_CONFIG = "piceli-gitops-config"
#: The label every one of them carries (the cluster read selects on it).
MANAGED_SELECTOR = "app.kubernetes.io/managed-by=piceli"


def not_live(item: Mapping[str, Any]) -> str | None:
    """Why an object will never pull its images again, else ``None`` (live).

    ``finished``: a pod that Succeeded or Failed, or a Job with a true
    ``Complete`` or ``Failed`` condition (a CronJob's template stays live).
    ``history``: a ReplicaSet a Deployment owns that is scaled to zero with no
    pod left, kept only for ``rollout undo``, and every ControllerRevision
    (the StatefulSet's template is what runs). Everything else is live,
    including a workload scaled to zero by hand.
    """
    kind = str(item.get("kind") or "Pod")
    status = item.get("status") or {}
    if kind == "ControllerRevision":
        return "history"
    if kind == "Pod" and status.get("phase") in {"Succeeded", "Failed"}:
        return "finished"
    if kind == "Job":
        for condition in status.get("conditions") or ():
            if (
                isinstance(condition, Mapping)
                and condition.get("type") in {"Complete", "Failed"}
                and str(condition.get("status")) == "True"
            ):
                return "finished"
    if kind == "ReplicaSet":
        owners = (item.get("metadata") or {}).get("ownerReferences") or ()
        replicas = (item.get("spec") or {}).get("replicas")
        if (
            any(
                isinstance(owner, Mapping) and owner.get("kind") == "Deployment"
                for owner in owners
            )
            and replicas == 0
            and not status.get("replicas")
        ):
            return "history"
    return None


def _record_kind(item: Mapping[str, Any]) -> str | None:
    """``environment``, ``controller`` or ``piceli`` (the controller's
    configuration: its builder) for a Piceli ConfigMap that names images in
    use; ``None`` for any other ConfigMap."""
    name = str((item.get("metadata") or {}).get("name") or "")
    if name == ENV_RECORD or name.startswith(ENV_PUSHED_PREFIX):
        return "environment"
    if name == CONTROLLER_STATUS:
        return "controller"
    if name == CONTROLLER_CONFIG:
        return "piceli"
    return None


def _record_references(item: Mapping[str, Any]) -> list[str]:
    """Image references in an environment record, pushed images or controller
    status: each ``images`` entry (a reference, or ``{digest, pull_ref}``), and
    every ``sha256:`` digest anywhere in the data (bare)."""
    found: list[str] = []
    data = item.get("data") or {}
    for key, text in data.items():
        if not isinstance(text, str):
            continue
        found.extend(_DIGEST.findall(text))
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if key == "record" and isinstance(value, Mapping):
            value = value.get("images")
        elif key != "images":
            continue
        for entry in (value or {}).values() if isinstance(value, Mapping) else ():
            if isinstance(entry, str):
                found.append(entry)
            elif isinstance(entry, Mapping):
                for field_name in ("pull_ref", "digest"):
                    if isinstance(entry.get(field_name), str):
                        found.append(entry[field_name])
    return found


def _object_references(item: Mapping[str, Any]) -> list[str]:
    kind = str(item.get("kind") or "Pod")
    if kind == "ConfigMap":
        return _record_references(item) if _record_kind(item) else []
    found: list[str] = []
    for spec in _pod_specs(item):
        for key in ("containers", "initContainers", "ephemeralContainers"):
            for container in spec.get(key) or ():
                found.append(str(container.get("image", "")))
    status = item.get("status") or {} if kind == "Pod" else {}
    for key in (
        "containerStatuses",
        "initContainerStatuses",
        "ephemeralContainerStatuses",
    ):
        for entry in status.get(key) or ():
            found.append(str(entry.get("image", "")))
            match = _IMAGE_ID.search(str(entry.get("imageID", "")))
            if match:
                path = _path(str(entry.get("imageID", "")))
                digest = match.group("digest")
                found.append(f"{path}@{digest}" if path else digest)
    return found


def _scan(
    items: Iterable[Mapping[str, Any]],
) -> tuple[
    dict[str, set[str]],
    dict[tuple[str, str], set[str]],
    dict[tuple[str | None, str], set[str]],
]:
    """``(live digests, live tags, every digest reference)`` with their kinds.

    The references cover live and not-live objects alike, keyed by
    ``(repository path or None, digest)``; a not-live object's kind carries
    why (``replicaset:history``, ``pod:finished``, ``job:finished``).
    """
    digests: dict[str, set[str]] = {}
    tags: dict[tuple[str, str], set[str]] = {}
    refs: dict[tuple[str | None, str], set[str]] = {}
    for item in items:
        kind = str(item.get("kind") or "Pod")
        label = (
            (_record_kind(item) or "configmap") if kind == "ConfigMap" else kind.lower()
        )
        state = not_live(item)
        found: set[str] = set()
        found_tags: set[tuple[str, str]] = set()
        for reference in _object_references(item):
            _image(reference, found, found_tags)
            match = _IMAGE_ID.search(reference)
            if match:
                refs.setdefault((_path(reference), match.group("digest")), set()).add(
                    label if state is None else f"{label}:{state}"
                )
        if state is not None:
            continue
        for digest in found:
            digests.setdefault(digest, set()).add(label)
        for pair in found_tags:
            tags.setdefault(pair, set()).add(label)
    return digests, tags, refs


def live_images(
    items: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, set[str]], dict[tuple[str, str], set[str]]]:
    """``({digest: kinds}, {(repository path, tag): kinds})`` the objects use.

    ``items`` are pods (every container, init and ephemeral container: the
    spec's image and the status's ``imageID``; a pod that Succeeded or Failed
    runs nothing), the pod templates of Deployments, StatefulSets,
    DaemonSets, ReplicaSets, Jobs and CronJobs, so an image a scaled-to-zero
    workload or a CronJob between runs will pull is live too, and Piceli's
    environment records, pushed images and controller status ConfigMaps. A
    finished Job and a Deployment's scaled-down rollout history are not live
    (:func:`not_live`). An object without a ``kind`` is a pod. A tag-only
    image is returned as a pair so the tag's digest can be kept.
    """
    digests, tags, _ = _scan(items)
    return digests, tags


def live_digests_from_pods(
    pods: Iterable[Mapping[str, Any]],
) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    """``(digests, (repository path, tag) pairs)`` of :func:`live_images`."""
    digests, tags = live_images(pods)
    return frozenset(digests), frozenset(tags)


def _path(reference: str) -> str | None:
    """The repository path of an image reference (no host, tag or digest)."""
    name = reference.split("://", 1)[-1].split("@", 1)[0]
    if not name or _DIGEST.fullmatch(name) or name.startswith("sha256:"):
        return None
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name = name[: len(name) - len(last)] + last.split(":", 1)[0]
    first, _, rest = name.partition("/")
    path = (
        rest
        if rest and ("." in first or ":" in first or first == "localhost")
        else name
    )
    return path or None


def _image(reference: str, digests: set[str], tags: set[tuple[str, str]]) -> None:
    match = _IMAGE_ID.search(reference)
    if match:
        digests.add(match.group("digest"))
        return
    name, _, tag = reference.rpartition(":")
    if not name or "/" in tag:  # no tag (a port only) -> implicit latest
        name, tag = reference, "latest"
    first, _, rest = name.partition("/")
    path = (
        rest
        if rest and ("." in first or ":" in first or first == "localhost")
        else name
    )
    if path:
        tags.add((path, tag))


#: Labels of Piceli's own workloads (controller, UI, registry): their old
#: copies are not an environment's rollback target.
_PICELI_COMPONENTS = {"gitops", "ui", "cluster"}
_REVISION = "deployment.kubernetes.io/revision"


def piceli_component(item: Mapping[str, Any]) -> bool:
    """Whether a workload is Piceli's own (controller, UI, in-cluster registry)."""
    labels = (item.get("metadata") or {}).get("labels") or {}
    return (
        labels.get("piceli.io/component") in _PICELI_COMPONENTS
        or labels.get("app.kubernetes.io/name") == "piceli-cluster-registry"
    )


def _number(value: Any) -> int:
    try:
        return int(str(value))
    except ValueError:
        return 0


def _owner(item: Mapping[str, Any], kind: str) -> str | None:
    for owner in (item.get("metadata") or {}).get("ownerReferences") or ():
        if isinstance(owner, Mapping) and owner.get("kind") == kind:
            return str(owner.get("name"))
    return None


def rollback_targets(
    items: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, set[str]], dict[tuple[str, str], set[str]]]:
    """``({digest: kinds}, {(path, tag): kinds})`` of every workload's rollback
    target: the previous release ``rollout undo`` returns to.

    - a Deployment's newest scaled-down ReplicaSet (by revision) that is not
      the Deployment's current revision;
    - a StatefulSet's newest ControllerRevision that is not its update
      revision (and its current revision while a rollout runs).

    An owner that is not among ``items`` keeps its newest old revision too
    (conservative). Piceli's own controller, UI and registry
    (:func:`piceli_component`) have none: their old copies are collectable.
    """
    owners: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for item in items:
        kind = str(item.get("kind") or "Pod")
        meta = item.get("metadata") or {}
        ns = str(meta.get("namespace") or "")
        if kind in {"Deployment", "StatefulSet"}:
            owners[(ns, kind, str(meta.get("name")))] = item
        owned = {"ReplicaSet": "Deployment", "ControllerRevision": "StatefulSet"}
        if kind in owned:
            name = _owner(item, owned[kind])
            if name is not None:
                groups.setdefault((ns, owned[kind], name), []).append(item)
    chosen: list[tuple[str, Mapping[str, Any]]] = []
    for key, members in sorted(groups.items()):
        owner = owners.get(key)
        if owner is not None and piceli_component(owner):
            continue
        if key[1] == "Deployment":
            current = ((owner or {}).get("metadata") or {}).get("annotations") or {}
            now = current.get(_REVISION)
            candidates = [
                rs
                for rs in members
                if not_live(rs) == "history"
                and (
                    now is None
                    or ((rs.get("metadata") or {}).get("annotations") or {}).get(
                        _REVISION
                    )
                    != now
                )
            ]

            def order(rs: Mapping[str, Any]) -> tuple[int, str]:
                meta = rs.get("metadata") or {}
                return (
                    _number((meta.get("annotations") or {}).get(_REVISION)),
                    str(meta.get("creationTimestamp") or ""),
                )

            if candidates:
                chosen.append(("replicaset", max(candidates, key=order)))
            continue
        status = (owner or {}).get("status") or {}
        update, running = status.get("updateRevision"), status.get("currentRevision")
        by_revision = sorted(members, key=lambda cr: _number(cr.get("revision")))
        if owner is None or not update:
            older = by_revision[:-1]  # the newest is (or was) what runs
        else:
            older = [
                cr
                for cr in by_revision
                if (cr.get("metadata") or {}).get("name") != update
            ]
        if older:
            chosen.append(("controllerrevision", older[-1]))
        for cr in by_revision:
            name = (cr.get("metadata") or {}).get("name")
            if running and running != update and name == running:
                chosen.append(("controllerrevision", cr))
    digests: dict[str, set[str]] = {}
    tags: dict[tuple[str, str], set[str]] = {}
    for label, item in chosen:
        found: set[str] = set()
        found_tags: set[tuple[str, str]] = set()
        for reference in _object_references(item):
            _image(reference, found, found_tags)
        for digest in found:
            digests.setdefault(digest, set()).add(f"{label}:rollback")
        for pair in found_tags:
            tags.setdefault(pair, set()).add(f"{label}:rollback")
    return digests, tags


@dataclass(frozen=True)
class LiveWorkloads:
    """What running workloads use. ``known`` is ``False`` when nothing was read."""

    known: bool
    digests: frozenset[str] = frozenset()
    tags: frozenset[tuple[str, str]] = frozenset()
    source: str | None = None
    by: Mapping[str, frozenset[str]] = field(default_factory=dict)  # digest -> kinds
    tag_by: Mapping[tuple[str, str], frozenset[str]] = field(default_factory=dict)
    #: Every digest the cluster names, live or not: (path or None, digest) -> kinds.
    refs: Mapping[tuple[str | None, str], frozenset[str]] = field(default_factory=dict)
    #: Rollback targets (:func:`rollback_targets`): digest -> kinds.
    rollback: Mapping[str, frozenset[str]] = field(default_factory=dict)
    rollback_tags: Mapping[tuple[str, str], frozenset[str]] = field(
        default_factory=dict
    )

    @classmethod
    def unknown(cls) -> LiveWorkloads:
        return cls(False)

    @classmethod
    def from_objects(cls, items: Iterable[Mapping[str, Any]]) -> LiveWorkloads:
        """Pods, workload pod templates and records (see :func:`live_images`),
        and the rollback targets of workloads (:func:`rollback_targets`)."""
        items = list(items)
        digests, tags, refs = _scan(items)
        back, back_tags = rollback_targets(items)
        return cls(
            True,
            frozenset(digests),
            frozenset(tags),
            "cluster",
            {d: frozenset(k) for d, k in digests.items()},
            {t: frozenset(k) for t, k in tags.items()},
            {r: frozenset(k) for r, k in refs.items()},
            {d: frozenset(k) for d, k in back.items()},
            {t: frozenset(k) for t, k in back_tags.items()},
        )

    @classmethod
    def merge(cls, parts: Sequence[LiveWorkloads]) -> LiveWorkloads:
        """One inventory from several sources (unknown when there is none)."""
        if not parts:
            return cls.unknown()

        def union(name: str) -> dict[Any, frozenset[str]]:
            merged: dict[Any, set[str]] = {}
            for part in parts:
                for key, kinds in getattr(part, name).items():
                    merged.setdefault(key, set()).update(kinds)
            return {key: frozenset(kinds) for key, kinds in merged.items()}

        sources = [part.source or "" for part in parts]
        return cls(
            all(part.known for part in parts),
            frozenset().union(*(part.digests for part in parts)),
            frozenset().union(*(part.tags for part in parts)),
            "+".join(sources) if len(set(sources)) > 1 else sources[0],
            union("by"),
            union("tag_by"),
            union("refs"),
            union("rollback"),
            union("rollback_tags"),
        )

    def probes(self) -> set[tuple[str | None, str]]:
        """Digests the inventory looks up: every reference (in the repository it
        names), and each live digest without one (in every repository)."""
        found = set(self.refs)
        named = {digest for _, digest in found}
        found.update((None, digest) for digest in self.digests - named)
        return found

    from_pods = from_objects

    @classmethod
    def from_file(cls, path: Path) -> LiveWorkloads:
        """Digests from a file: a JSON list or one digest per line."""
        text = path.read_text()
        try:
            value = json.loads(text)
            items = value if isinstance(value, list) else value.get("digests")
        except (ValueError, AttributeError):
            items = [
                line.split("#", 1)[0].strip()
                for line in text.splitlines()
                if line.split("#", 1)[0].strip()
            ]
        if not isinstance(items, list) or not all(
            isinstance(item, str) and _DIGEST.fullmatch(item) for item in items
        ):
            raise RetentionError(code="retention-invalid")
        return cls(
            True,
            frozenset(items),
            frozenset(),
            "file",
            {item: frozenset({"file"}) for item in items},
        )


_LISTS = (
    ("CoreV1Api", "pod", "Pod"),
    ("AppsV1Api", "deployment", "Deployment"),
    ("AppsV1Api", "stateful_set", "StatefulSet"),
    ("AppsV1Api", "daemon_set", "DaemonSet"),
    ("AppsV1Api", "replica_set", "ReplicaSet"),
    ("BatchV1Api", "job", "Job"),
    ("BatchV1Api", "cron_job", "CronJob"),
    ("AppsV1Api", "controller_revision", "ControllerRevision"),
    ("CoreV1Api", "config_map", "ConfigMap"),
)


def read_live_pods(
    kubeconfig: Path,
    context: str,
    namespaces: Sequence[str] | None = None,
    *,
    transport: str = "https",
    timeout: float = 30.0,
    exec_policy: Any = None,
) -> list[dict[str, Any]]:
    """Pods and the pod templates of Deployments, StatefulSets, DaemonSets,
    ReplicaSets, ControllerRevisions, Jobs and CronJobs, and Piceli's
    ConfigMaps (environment records, pushed images, controller status and
    configuration; selected by label), in
    ``namespaces`` (default: all), read with an explicit context. Each item
    carries its ``kind``. Any failed list (for example a missing permission)
    raises: the inventory would be incomplete."""
    from kubernetes import client as k8s

    from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

    api_client = api_client_from_kubeconfig(
        kubeconfig,
        context,
        transport=transport,  # type: ignore[arg-type]
        exec_policy=exec_policy,
    )
    try:
        items: list[dict[str, Any]] = []
        # Raw JSON: only image fields are read, so a field the typed client
        # does not know (or requires) cannot fail the read.
        for api_name, noun, kind in _LISTS:
            api = getattr(k8s, api_name)(api_client)
            options: dict[str, Any] = {
                "_preload_content": False,
                "_request_timeout": timeout,
            }
            if kind == "ConfigMap":
                options["label_selector"] = MANAGED_SELECTOR
            responses = (
                [getattr(api, f"list_{noun}_for_all_namespaces")(**options)]
                if not namespaces
                else [
                    getattr(api, f"list_namespaced_{noun}")(name, **options)
                    for name in namespaces
                ]
            )
            for response in responses:
                for item in json.loads(response.data).get("items") or ():
                    if kind == "ConfigMap" and _record_kind(item) is None:
                        continue
                    items.append({**item, "kind": kind})
        return items
    finally:
        api_client.close()


# ------------------------------------------------------------------ inventory


@dataclass
class ManifestInfo:
    """One manifest, keyed by digest (the same content in two repositories is
    one entry present in both)."""

    digest: str
    media_type: str
    size: int
    repositories: set[str] = field(default_factory=set)
    children: tuple[str, ...] = ()
    blobs: dict[str, int] = field(default_factory=dict)  # config + layers
    subject: str | None = None
    tags: dict[str, set[str]] = field(default_factory=dict)  # repo -> tags


@dataclass
class Inventory:
    """Everything the registry holds that retention can see."""

    manifests: dict[str, ManifestInfo] = field(default_factory=dict)
    referrers: dict[str, set[str]] = field(default_factory=dict)  # subject -> digests
    repositories: tuple[str, ...] = ()
    tag_digests: dict[tuple[str, str], str] = field(default_factory=dict)
    catalog: bool = True

    def closure(self, roots: Iterable[str]) -> set[str]:
        """``roots`` plus every child manifest and referrer, transitively."""
        seen: set[str] = set()
        stack = [root for root in roots if root in self.manifests]
        while stack:
            digest = stack.pop()
            if digest in seen:
                continue
            seen.add(digest)
            info = self.manifests[digest]
            stack.extend(c for c in info.children if c in self.manifests)
            stack.extend(
                r for r in self.referrers.get(digest, ()) if r in self.manifests
            )
        return seen

    def blob_sizes(self, digests: Iterable[str]) -> dict[str, int]:
        """Deduplicated ``{blob digest: size}`` of the manifests (their own bytes
        included): a blob shared by several manifests counts once."""
        blobs: dict[str, int] = {}
        for digest in digests:
            info = self.manifests[digest]
            blobs[digest] = info.size
            blobs.update(info.blobs)
        return blobs

    def bytes_of(self, digests: Iterable[str]) -> int:
        return sum(self.blob_sizes(digests).values())


def _parse_manifest(digest: str, body: bytes, media: str) -> ManifestInfo:
    try:
        document = json.loads(body)
    except ValueError as error:
        raise RegistryError("registry-error") from error
    if not isinstance(document, dict):
        raise RegistryError("registry-error")
    kind = str(document.get("mediaType") or media.split(";")[0].strip())
    info = ManifestInfo(digest, kind, len(body))
    subject = document.get("subject")
    if isinstance(subject, dict) and _DIGEST.fullmatch(str(subject.get("digest"))):
        info.subject = str(subject["digest"])
    if isinstance(document.get("manifests"), list):
        info.children = tuple(
            str(item["digest"])
            for item in document["manifests"]
            if isinstance(item, dict) and _DIGEST.fullmatch(str(item.get("digest")))
        )
        return info
    for descriptor in [document.get("config"), *(document.get("layers") or ())]:
        if isinstance(descriptor, dict) and _DIGEST.fullmatch(
            str(descriptor.get("digest"))
        ):
            size = descriptor.get("size")
            info.blobs[str(descriptor["digest"])] = (
                size if isinstance(size, int) and size >= 0 else 0
            )
    return info


def in_scope(repository: str, prefix: str) -> bool:
    """Whether ``repository`` is ``prefix`` or under it (``""``: everything)."""
    return not prefix or repository == prefix or repository.startswith(prefix + "/")


def collect_inventory(
    target: Target,
    endpoint: RegistryEndpoint,
    releases: Sequence[Release],
    *,
    repositories: Sequence[str] = (),
    client_factory: ClientFactory = default_client,
    probes: Iterable[tuple[str | None, str]] = (),
    stored: Iterable[tuple[str, str]] = (),
) -> Inventory:
    """Read the registry: repositories under the target's prefix, their tags,
    and every manifest those tags, ``releases`` and ``probes`` reach (with
    children and referrers). A probe ``(path, digest)`` is looked up in that
    repository (``None``: in every one): the registry API lists no untagged
    manifest, so a digest pushed without a tag or receipt is found through
    whatever still names it, or through ``stored``: ``(repository, digest)``
    of every manifest read from the registry's own storage. Any read
    failure raises: an unknown inventory never licenses a deletion."""
    probes = set(probes)
    stored = {item for item in stored if in_scope(item[0], target.repository)}
    prefix = target.repository
    names = {repo for repo in repositories if in_scope(repo, prefix)}
    names.update(repo for repo, _ in stored)
    names.update(repo for release in releases for repo, _ in release.digests)
    names = {repo for repo in names if in_scope(repo, prefix)}
    catalog = client_factory(endpoint, "pull").list_repositories()
    if catalog is not None:
        names.update(repo for repo in catalog if in_scope(repo, prefix))
    else:  # no catalog: the repositories the cluster's references name
        names.update(
            path for path, _ in probes if path is not None and in_scope(path, prefix)
        )
    inventory = Inventory(
        repositories=tuple(sorted(names)), catalog=catalog is not None
    )
    wanted: dict[str, set[str]] = {}
    for release in releases:
        for repo, digest in release.digests:
            wanted.setdefault(repo, set()).add(digest)
    for path, digest in probes:
        for repo in names if path is None else ({path} & names):
            wanted.setdefault(repo, set()).add(digest)
    for repo, digest in stored:
        wanted.setdefault(repo, set()).add(digest)
    for repo in sorted(names):
        client = client_factory(endpoint, "pull")
        client.authenticate(repo)
        pending = set(wanted.get(repo, ()))
        for tag in client.list_tags(repo):
            digest = client.manifest_digest(repo, tag)
            if digest is None:
                continue
            inventory.tag_digests[(repo, tag)] = digest
            pending.add(digest)
        fallback: dict[str, set[str]] = {}
        loaded: set[str] = set()
        while pending:
            digest = pending.pop()
            if digest in loaded:
                continue
            loaded.add(digest)
            try:
                body, media = client.get_manifest(repo, digest)
            except RegistryError as error:
                if error.reason == "manifest-not-found":
                    continue  # a receipt's digest already deleted
                raise
            if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
                raise RegistryError("registry-digest-mismatch")
            if len(inventory.manifests) >= _MAX_MANIFESTS:
                raise RegistryError("registry-response-too-large")
            info = inventory.manifests.get(digest) or _parse_manifest(
                digest, body, media
            )
            inventory.manifests[digest] = info
            info.repositories.add(repo)
            pending.update(info.children)
            if info.subject is not None:
                inventory.referrers.setdefault(info.subject, set()).add(digest)
                pending.add(info.subject)
        for (name, tag), digest in inventory.tag_digests.items():
            if name == repo:
                inventory.manifests[digest].tags.setdefault(repo, set()).add(tag)
                match = _FALLBACK_TAG.fullmatch(tag)
                if match:  # ``sha256-<hex>`` fallback tag: a referrer of <hex>
                    fallback.setdefault("sha256:" + match.group(1), set()).add(digest)
        for subject, found in fallback.items():
            inventory.referrers.setdefault(subject, set()).update(found)
        for digest in sorted(loaded & set(inventory.manifests)):
            if inventory.manifests[digest].subject is not None:
                continue
            listed = client.referrers(repo, digest)
            for item in listed or ():
                child = item.get("digest")
                if isinstance(child, str) and _DIGEST.fullmatch(child):
                    inventory.referrers.setdefault(digest, set()).add(child)
                    if child not in inventory.manifests:
                        body, media = client.get_manifest(repo, child)
                        inventory.manifests[child] = _parse_manifest(child, body, media)
                    inventory.manifests[child].repositories.add(repo)
    return inventory


# ------------------------------------------------------------------- planning


@dataclass(frozen=True)
class RetentionPlan:
    """The decision: kept and collectable manifests, and the exact delete list."""

    target: Target
    policy: RetentionPolicy
    releases: tuple[dict[str, Any], ...]
    kept: dict[str, tuple[str, ...]]  # digest -> reasons
    collectable: tuple[tuple[str, str], ...]  # (repository, digest), sorted
    kept_bytes: int
    reclaimable_bytes: int
    live: LiveWorkloads
    repositories: tuple[str, ...]
    live_by: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    shared_bytes: int = 0  # collectable manifests' blobs still used by kept ones
    #: Why each collectable digest is not kept, and the not-live objects that
    #: still name it (``replicaset:history``, ``pod:finished`` ...).
    why: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    seen_in: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        """The hash an approval binds to: target, and the exact delete list."""
        body = json.dumps(
            {
                "schema": PLAN_SCHEMA,
                "target": self.target.identity,
                "delete": [list(item) for item in self.collectable],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(body.encode()).hexdigest()

    def public(self, inventory: Inventory) -> dict[str, Any]:
        rows = []
        for digest, info in sorted(inventory.manifests.items()):
            for repository in sorted(info.repositories):
                reasons = self.kept.get(digest)
                rows.append(
                    {
                        "repository": repository,
                        "digest": digest,
                        "tags": sorted(info.tags.get(repository, ())),
                        "bytes": info.size + sum(info.blobs.values()),
                        "kept": reasons is not None,
                        "reasons": list(reasons) if reasons else ["unreferenced"],
                        "live_by": list(self.live_by.get(digest, ())),
                    }
                )
        return {
            "schema": PLAN_SCHEMA,
            "digest": self.digest,
            "target": self.target.public(),
            "policy": self.policy.public(),
            "live": {
                "read": self.live.known,
                "source": self.live.source,
                "digests": len(self.live.digests),
                "rollback": len(self.live.rollback),
                "by": {
                    kind: sum(1 for kinds in self.live.by.values() if kind in kinds)
                    for kind in sorted({k for v in self.live.by.values() for k in v})
                },
            },
            "repositories": list(self.repositories),
            "releases": list(self.releases),
            "manifests": rows,
            "kept": {"manifests": len(self.kept), "bytes": self.kept_bytes},
            "collectable": {
                "manifests": len(self.collectable),
                "deletions": [
                    {
                        "repository": repo,
                        "digest": digest,
                        "bytes": inventory.manifests[digest].size
                        + sum(inventory.manifests[digest].blobs.values()),
                        "why": list(self.why.get(digest, ("unreferenced",))),
                        "seen_in": list(self.seen_in.get(digest, ())),
                    }
                    for repo, digest in self.collectable
                ],
                "reclaimable_bytes": self.reclaimable_bytes,
            },
            "garbage_collect": GARBAGE_COLLECT,
        }


GARBAGE_COLLECT = {
    "note": (
        "Deleting manifests frees no disk by itself: run the registry's own "
        "garbage collector afterwards, WITHOUT --delete-untagged (images "
        "pushed by digest are untagged and would be deleted too)."
    ),
    "distribution": "registry garbage-collect /etc/docker/registry/config.yml",
    "piceli_node_local_registry": "registry.garbage_collection(delete_untagged=False)",
    "piceli_cluster_registry": (
        "kubectl --kubeconfig FILE --context NAME -n piceli-system exec "
        "deploy/piceli-registry -- registry garbage-collect "
        "/etc/distribution/config.yml"
    ),
}


def plan_retention(
    target: Target,
    inventory: Inventory,
    releases: Sequence[Release],
    policy: RetentionPolicy,
    live: LiveWorkloads,
) -> RetentionPlan:
    """Decide what to keep. Pure: no network."""
    ledgered = {digest for release in releases for _, digest in release.digests}
    releases = generations(releases)
    reasons: dict[str, set[str]] = {}

    def keep(digest: str, reason: str) -> None:
        if digest in inventory.manifests:
            reasons.setdefault(digest, set()).add(reason)

    for pin in sorted(policy.pins):
        keep(pin, "pinned")
    for digest in sorted(live.digests):
        keep(digest, "live")
    # The previous release of every workload: what ``rollout undo`` pulls.
    for digest in sorted(live.rollback):
        keep(digest, "rollback")
    live_by: dict[str, set[str]] = {}
    for digest in sorted(live.digests):
        if digest in inventory.manifests:
            live_by.setdefault(digest, set()).update(live.by.get(digest, ()))
    for (repo, tag), digest in sorted(inventory.tag_digests.items()):
        if (repo, tag) in live.tags:
            keep(digest, "live")
            live_by.setdefault(digest, set()).update(live.tag_by.get((repo, tag), ()))
        if (repo, tag) in live.rollback_tags:
            keep(digest, "rollback")
    if not policy.collect_unledgered:
        referrers = {d for found in inventory.referrers.values() for d in found}
        for digest, info in inventory.manifests.items():
            if info.tags and digest not in ledgered and digest not in referrers:
                keep(digest, "unledgered")
    kept_roots = set(reasons)
    decided: list[dict[str, Any]] = []
    released: dict[str, str] = {}  # digest -> why its release was not kept
    stopped = False
    for index, release in enumerate(releases):
        names = {digest for _, digest in release.digests}
        if index < policy.keep:
            keeping, reason = True, "release"
        elif policy.budget is None or stopped:
            keeping, reason = False, "old-release"
        else:
            trial = inventory.closure(kept_roots | names)
            keeping = inventory.bytes_of(trial) <= policy.budget
            reason = "budget" if keeping else "over-budget"
        if keeping:
            kept_roots |= names
            for digest in names:
                keep(digest, reason)
        else:
            stopped = True
        decided.append({**release.public(), "kept": keeping, "reason": reason})
        if not keeping:
            for digest in names:
                released.setdefault(digest, reason)
    closure = inventory.closure(set(reasons))
    for digest in closure - set(reasons):
        reasons.setdefault(digest, set()).add("referenced")
    kept_digests = set(reasons)
    doomed = set(inventory.manifests) - kept_digests
    kept_blobs = inventory.blob_sizes(kept_digests)
    doomed_blobs = inventory.blob_sizes(doomed)
    reclaim = {d: s for d, s in doomed_blobs.items() if d not in kept_blobs}
    collectable = tuple(
        sorted(
            (repo, digest)
            for digest in doomed
            for repo in inventory.manifests[digest].repositories
        )
    )
    seen: dict[str, set[str]] = {}
    for (_, digest), kinds in live.refs.items():
        seen.setdefault(digest, set()).update(kinds)
    why: dict[str, tuple[str, ...]] = {}
    for digest in doomed:
        found = [released[digest]] if digest in released else []
        if digest in seen:
            found.append("not-live")
        if policy.collect_unledgered and inventory.manifests[digest].tags:
            found.append("unledgered")
        why[digest] = tuple(found or ["unreferenced"])
    return RetentionPlan(
        target=target,
        policy=policy,
        releases=tuple(decided),
        kept={digest: tuple(sorted(found)) for digest, found in reasons.items()},
        collectable=collectable,
        kept_bytes=sum(kept_blobs.values()),
        reclaimable_bytes=sum(reclaim.values()),
        live=live,
        live_by={d: tuple(sorted(k)) for d, k in live_by.items()},
        repositories=inventory.repositories,
        shared_bytes=sum(s for d, s in doomed_blobs.items() if d in kept_blobs),
        why=why,
        seen_in={d: tuple(sorted(seen[d])) for d in doomed if d in seen},
    )


# ------------------------------------------------------------------- deleting


@dataclass(frozen=True)
class RetentionGrant:
    """The approved plan hash."""

    digest: str


def delete_collectable(
    plan: RetentionPlan,
    inventory: Inventory,
    grant: RetentionGrant,
    endpoint: RegistryEndpoint,
    *,
    client_factory: ClientFactory = default_client,
) -> dict[str, Any]:
    """Delete exactly the approved manifests; return a receipt.

    Refuses (``retention-not-approved``) when ``grant`` is not this plan's
    hash, and (``retention-live-unknown``) when no live inventory was read, or
    when ``keep=0`` (no release kept) rests on anything less than a read of
    the cluster.
    Never deletes a digest that the plan keeps, that a live workload uses or
    that is a workload's rollback target.
    Order: indexes, then their children, so no kept index ever loses a child.
    """
    if grant.digest != plan.digest:
        raise RetentionError(code="retention-not-approved")
    if not plan.live.known:
        raise RetentionError(code="retention-live-unknown")
    if plan.policy.keep == 0 and "cluster" not in (plan.live.source or ""):
        raise RetentionError(code="retention-live-unknown")
    protected = set(plan.kept) | set(plan.live.digests) | set(plan.live.rollback)
    if any(digest in protected for _, digest in plan.collectable):
        raise RetentionError(code="retention-not-approved")
    order = sorted(
        plan.collectable,
        key=lambda item: (
            inventory.manifests[item[1]].children == (),
            item[0],
            item[1],
        ),
    )
    deleted: list[dict[str, str]] = []
    absent: list[dict[str, str]] = []
    receipt: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "state": "deleted",
        "reason": None,
        "plan": plan.digest,
        "target": plan.target.public(),
        "deleted": deleted,
        "already_absent": absent,
        "reclaimed_bytes": 0,
        "garbage_collect": GARBAGE_COLLECT,
    }
    clients: dict[str, StreamedOciRegistryClient] = {}
    try:
        for repository, digest in order:
            client = clients.get(repository)
            if client is None:
                client = clients[repository] = client_factory(endpoint, "pull,delete")
                client.authenticate(repository)
            row = {"repository": repository, "digest": digest}
            (deleted if client.delete_manifest(repository, digest) else absent).append(
                row
            )
    except RegistryError as error:
        receipt.update(state="failed", reason=error.reason)
    receipt["reclaimed_bytes"] = _reclaimed(plan, inventory, deleted, absent)
    receipt["reclaimable_after_garbage_collect"] = plan.reclaimable_bytes
    return receipt


def _reclaimed(
    plan: RetentionPlan,
    inventory: Inventory,
    deleted: Sequence[Mapping[str, str]],
    absent: Sequence[Mapping[str, str]],
) -> int:
    """Bytes the registry's collector will free: blobs of the deleted manifests
    that no kept manifest (and no manifest left undeleted) still references."""
    gone = {row["digest"] for row in (*deleted, *absent)}
    left = set(inventory.manifests) - gone
    kept_blobs = inventory.blob_sizes(left)
    return sum(
        size
        for digest, size in inventory.blob_sizes(gone).items()
        if digest not in kept_blobs
    )
