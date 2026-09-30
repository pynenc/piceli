"""Registry retention: which manifests a registry keeps, and deleting the rest.

``piceli artifacts retention`` reads a registry (the node-local registry Piceli
delivers to, or any OCI registry reachable with credentials) and decides, per
manifest, whether it is **kept** or **collectable**:

* the last ``keep`` **releases** (newest first; from the publish and delivery
  receipts you give it) keep every digest they name;
* **pinned** digests (``--pin``, ``--pin-file``) are always kept;
* **live** digests, read from the pods and the pod templates (Deployments,
  StatefulSets, DaemonSets, ReplicaSets, Jobs, CronJobs) of a cluster, or a
  file, are always kept: a digest a running workload uses is never deleted;
* the children of a kept index, and the referrers (SBOM, provenance,
  signatures) of a kept manifest, are kept with it;
* a tagged manifest that no receipt mentions is **unledgered**: kept unless
  ``collect_unledgered`` is set (conservative default).

With a ``budget`` more releases than ``keep`` are kept, newest first, while the
deduplicated bytes of everything kept stay within it (never fewer than
``keep`` releases, never less than the pinned and live digests).

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
        if self.keep < 1:
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
)


def _pod_specs(item: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The pod spec of a Pod, or the pod template's spec of a workload."""
    spec = item.get("spec") or {}
    if item.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    if item.get("kind") not in {None, "Pod"}:
        spec = (spec.get("template") or {}).get("spec") or {}
    return [spec]


def live_images(
    items: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, set[str]], dict[tuple[str, str], set[str]]]:
    """``({digest: kinds}, {(repository path, tag): kinds})`` the objects use.

    ``items`` are pods (every container, init and ephemeral container, in any
    phase: the spec's image and the status's ``imageID``) and the pod templates
    of Deployments, StatefulSets, DaemonSets, ReplicaSets (old ones kept for
    rollout history included), Jobs and CronJobs, so an image a scaled-to-zero
    workload or a CronJob between runs will pull is live too. An object
    without a ``kind`` is a pod. A tag-only image is returned as a pair so
    the tag's digest can be kept.
    """
    digests: dict[str, set[str]] = {}
    tags: dict[tuple[str, str], set[str]] = {}
    for item in items:
        kind = str(item.get("kind") or "Pod")
        found: set[str] = set()
        found_tags: set[tuple[str, str]] = set()
        for spec in _pod_specs(item):
            for key in ("containers", "initContainers", "ephemeralContainers"):
                for container in spec.get(key) or ():
                    _image(str(container.get("image", "")), found, found_tags)
        status = item.get("status") or {} if kind == "Pod" else {}
        for key in (
            "containerStatuses",
            "initContainerStatuses",
            "ephemeralContainerStatuses",
        ):
            for entry in status.get(key) or ():
                _image(str(entry.get("image", "")), found, found_tags)
                match = _IMAGE_ID.search(str(entry.get("imageID", "")))
                if match:
                    found.add(match.group("digest"))
        for digest in found:
            digests.setdefault(digest, set()).add(kind.lower())
        for pair in found_tags:
            tags.setdefault(pair, set()).add(kind.lower())
    return digests, tags


def live_digests_from_pods(
    pods: Iterable[Mapping[str, Any]],
) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    """``(digests, (repository path, tag) pairs)`` of :func:`live_images`."""
    digests, tags = live_images(pods)
    return frozenset(digests), frozenset(tags)


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


@dataclass(frozen=True)
class LiveWorkloads:
    """What running workloads use. ``known`` is ``False`` when nothing was read."""

    known: bool
    digests: frozenset[str] = frozenset()
    tags: frozenset[tuple[str, str]] = frozenset()
    source: str | None = None
    by: Mapping[str, frozenset[str]] = field(default_factory=dict)  # digest -> kinds
    tag_by: Mapping[tuple[str, str], frozenset[str]] = field(default_factory=dict)

    @classmethod
    def unknown(cls) -> LiveWorkloads:
        return cls(False)

    @classmethod
    def from_objects(cls, items: Iterable[Mapping[str, Any]]) -> LiveWorkloads:
        """Pods and workload pod templates (see :func:`live_images`)."""
        digests, tags = live_images(items)
        return cls(
            True,
            frozenset(digests),
            frozenset(tags),
            "cluster",
            {d: frozenset(k) for d, k in digests.items()},
            {t: frozenset(k) for t, k in tags.items()},
        )

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
)


def read_live_pods(
    kubeconfig: Path,
    context: str,
    namespaces: Sequence[str] | None = None,
    *,
    transport: str = "https",
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Pods and the pod templates of Deployments, StatefulSets, DaemonSets,
    ReplicaSets, Jobs and CronJobs in ``namespaces`` (default: all), read with
    an explicit context. Each item carries its ``kind``. Any failed list (for
    example a missing permission) raises: the inventory would be incomplete."""
    from kubernetes import client as k8s

    from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

    api_client = api_client_from_kubeconfig(
        kubeconfig,
        context,
        transport=transport,  # type: ignore[arg-type]
    )
    try:
        items: list[dict[str, Any]] = []
        # Raw JSON: only image fields are read, so a field the typed client
        # does not know (or requires) cannot fail the read.
        for api_name, noun, kind in _LISTS:
            api = getattr(k8s, api_name)(api_client)
            options = {"_preload_content": False, "_request_timeout": timeout}
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
    return repository == prefix or repository.startswith(prefix + "/")


def collect_inventory(
    target: RegistryTarget,
    endpoint: RegistryEndpoint,
    releases: Sequence[Release],
    *,
    repositories: Sequence[str] = (),
    client_factory: ClientFactory = default_client,
) -> Inventory:
    """Read the registry: repositories under the target's prefix, their tags,
    and every manifest those tags and ``releases`` reach (with children and
    referrers). Any read failure raises: an unknown inventory never licenses a
    deletion."""
    prefix = target.repository
    names = {repo for repo in repositories if in_scope(repo, prefix)}
    names.update(repo for release in releases for repo, _ in release.digests)
    names = {repo for repo in names if in_scope(repo, prefix)}
    catalog = client_factory(endpoint, "pull").list_repositories()
    if catalog is not None:
        names.update(repo for repo in catalog if in_scope(repo, prefix))
    inventory = Inventory(
        repositories=tuple(sorted(names)), catalog=catalog is not None
    )
    wanted: dict[str, set[str]] = {}
    for release in releases:
        for repo, digest in release.digests:
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

    target: RegistryTarget
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
                    {"repository": repo, "digest": digest}
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
}


def plan_retention(
    target: RegistryTarget,
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
    live_by: dict[str, set[str]] = {}
    for digest in sorted(live.digests):
        if digest in inventory.manifests:
            live_by.setdefault(digest, set()).update(live.by.get(digest, ()))
    for (repo, tag), digest in sorted(inventory.tag_digests.items()):
        if (repo, tag) in live.tags:
            keep(digest, "live")
            live_by.setdefault(digest, set()).update(live.tag_by.get((repo, tag), ()))
    if not policy.collect_unledgered:
        referrers = {d for found in inventory.referrers.values() for d in found}
        for digest, info in inventory.manifests.items():
            if info.tags and digest not in ledgered and digest not in referrers:
                keep(digest, "unledgered")
    kept_roots = set(reasons)
    decided: list[dict[str, Any]] = []
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
    hash, and (``retention-live-unknown``) when no live inventory was read.
    Never deletes a digest that the plan keeps or that a live workload uses.
    Order: indexes, then their children, so no kept index ever loses a child.
    """
    if grant.digest != plan.digest:
        raise RetentionError(code="retention-not-approved")
    if not plan.live.known:
        raise RetentionError(code="retention-live-unknown")
    protected = set(plan.kept) | set(plan.live.digests)
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
