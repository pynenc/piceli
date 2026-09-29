"""Which retained claims a release touches, and which writers must stop.

Pure functions over manifests: the desired workloads (as the release will
apply them), the live workloads and the live PersistentVolumeClaims of the
namespace. Nothing here reads a cluster.

A workload is *changed* when a container image differs from the live one (a
build image that is not delivered yet counts as changed), when its claim
templates or claim mounts differ, or when it is new and mounts a claim that
already exists. A changed workload *touches* every claim it mounts writably:
for a StatefulSet each existing claim of its templates
(``<template>-<name>-<ordinal>``, one per replica, including replicas scaled
away) and every existing claim its pod volumes name. The *writers* of a claim
are every workload of the release that mounts it writably; all of them are
stopped for the copy.

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from piceli.restore.model import RestorePointError

#: Workload kinds whose pods can write a claim.
WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"})
#: Writers Piceli can stop (scale to zero) and start again.
STOPPABLE = frozenset({"Deployment", "StatefulSet"})


def _pod(manifest: Mapping[str, Any]) -> dict[str, Any]:
    spec = manifest.get("spec") or {}
    if manifest.get("kind") == "CronJob":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
    template = spec.get("template") or {}
    pod = template.get("spec") if isinstance(template, dict) else None
    return pod if isinstance(pod, dict) else {}


def _containers(pod: Mapping[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for key in ("initContainers", "containers", "ephemeralContainers"):
        found.extend(item for item in pod.get(key) or () if isinstance(item, dict))
    return found


def _ref(manifest: Mapping[str, Any]) -> str:
    return f"{manifest.get('kind')}/{(manifest.get('metadata') or {}).get('name')}"


def images(manifest: Mapping[str, Any]) -> dict[str, str]:
    """``container name → image`` of a workload (init containers included)."""
    return {
        str(item.get("name")): str(item.get("image") or "")
        for item in _containers(_pod(manifest))
    }


def _templates(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    if manifest.get("kind") != "StatefulSet":
        return []
    spec = manifest.get("spec") or {}
    return [
        item
        for item in spec.get("volumeClaimTemplates") or ()
        if isinstance(item, dict)
    ]


def _template_settings(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Claim templates reduced to what a release can change (no defaults)."""
    found = []
    for item in _templates(manifest):
        spec = item.get("spec") or {}
        found.append(
            {
                "name": (item.get("metadata") or {}).get("name"),
                "accessModes": sorted(spec.get("accessModes") or ()),
                "storageClassName": spec.get("storageClassName"),
                "storage": ((spec.get("resources") or {}).get("requests") or {}).get(
                    "storage"
                ),
            }
        )
    return sorted(found, key=lambda value: str(value["name"]))


def _writable_volumes(manifest: Mapping[str, Any]) -> dict[str, str | None]:
    """Volume name → claim name (``None`` for a claim template) mounted writably."""
    pod = _pod(manifest)
    claims: dict[str, str | None] = {}
    for volume in pod.get("volumes") or ():
        if not isinstance(volume, dict):
            continue
        source = volume.get("persistentVolumeClaim")
        if isinstance(source, dict) and not source.get("readOnly"):
            claims[str(volume.get("name"))] = str(source.get("claimName"))
    for template in _templates(manifest):
        name = (template.get("metadata") or {}).get("name")
        if name:
            claims[str(name)] = None
    mounted = {
        str(mount.get("name"))
        for container in _containers(pod)
        for mount in container.get("volumeMounts") or ()
        if isinstance(mount, dict) and not mount.get("readOnly")
    }
    return {name: claim for name, claim in claims.items() if name in mounted}


def _mounts(manifest: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """Claim mounts, for comparing storage settings."""
    pod = _pod(manifest)
    sources: dict[str, str] = {}
    for volume in pod.get("volumes") or ():
        if isinstance(volume, dict) and isinstance(
            volume.get("persistentVolumeClaim"), dict
        ):
            claim = volume["persistentVolumeClaim"]
            sources[str(volume.get("name"))] = (
                f"claim:{claim.get('claimName')}:{bool(claim.get('readOnly'))}"
            )
    for template in _templates(manifest):
        name = (template.get("metadata") or {}).get("name")
        if name:
            sources[str(name)] = f"template:{name}"
    found = []
    for container in _containers(pod):
        for mount in container.get("volumeMounts") or ():
            if isinstance(mount, dict) and str(mount.get("name")) in sources:
                found.append(
                    (
                        str(container.get("name")),
                        sources[str(mount.get("name"))],
                        str(mount.get("mountPath")),
                        str(mount.get("subPath") or ""),
                        str(bool(mount.get("readOnly"))),
                    )
                )
    return sorted(found)


def changes(
    desired: Mapping[str, Any],
    live: Mapping[str, Any] | None,
    *,
    pending: Callable[[str], bool] = lambda _image: False,
) -> list[str]:
    """Why applying ``desired`` over ``live`` changes the workload's data path.

    Empty when neither an image nor a storage setting changes.
    """
    why: list[str] = []
    wanted = images(desired)
    current = images(live) if live is not None else {}
    for name, image in sorted(wanted.items()):
        if pending(image):
            why.append(f"image of container {name} is not delivered yet")
        elif live is not None and current.get(name) != image:
            why.append(f"image of container {name} changes")
    if live is None:
        if _writable_volumes(desired):
            why.append("new writer of existing claims")
        return why
    if _template_settings(desired) != _template_settings(live):
        why.append("claim templates change")
    if _mounts(desired) != _mounts(live):
        why.append("claim mounts change")
    return why


@dataclass
class RestorePlan:
    """The claims a restore point covers and the writers stopped for it."""

    #: One entry per claim: ``claim``, ``workload``, ``ordinal``, ``why``.
    claims: list[dict[str, Any]] = field(default_factory=list)
    #: One entry per writer to stop: ``workload``, ``kind``, ``name``,
    #: ``replicas`` (live), ``claims`` it writes, ``quiesce`` hooks.
    writers: list[dict[str, Any]] = field(default_factory=list)
    #: Workloads that change but write no existing claim (evidence).
    unchanged_claims: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.claims

    def identity(self) -> dict[str, Any]:
        """What an approval covers (no live replica counts)."""
        return {
            "claims": [
                {key: item[key] for key in ("claim", "workload", "ordinal", "why")}
                for item in self.claims
            ],
            "writers": [
                {
                    "workload": item["workload"],
                    "claims": item["claims"],
                    "quiesce": item["quiesce"],
                }
                for item in self.writers
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        return {"claims": list(self.claims), "writers": list(self.writers)}


def _claim_names(
    manifest: Mapping[str, Any], existing: Mapping[str, Mapping[str, Any]]
) -> list[tuple[str, int | None]]:
    """Existing claims a workload mounts writably, with the replica ordinal."""
    name = str((manifest.get("metadata") or {}).get("name"))
    found: list[tuple[str, int | None]] = []
    for _volume, claim in sorted(_writable_volumes(manifest).items()):
        if claim is not None:
            if claim in existing:
                found.append((claim, None))
            continue
        pattern = re.compile(re.escape(f"{_volume}-{name}-") + r"(0|[1-9][0-9]*)")
        for candidate in sorted(existing):
            match = pattern.fullmatch(candidate)
            if match:
                found.append((candidate, int(match.group(1))))
    return found


def plan(
    desired: Iterable[Mapping[str, Any]],
    live: Iterable[Mapping[str, Any]],
    claims: Iterable[Mapping[str, Any]],
    *,
    pending: Callable[[str], bool] = lambda _image: False,
    hooks: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> RestorePlan:
    """The restore point a release needs (see the module doc).

    :param desired: The release's workload manifests (other kinds ignored).
    :param live: The namespace's live workloads.
    :param claims: The namespace's live PersistentVolumeClaims; only bound
        claims hold data.
    :param pending: Whether an image is a placeholder for an image that is
        not delivered yet (counted as a change).
    :param hooks: Quiesce hook descriptions per ``Kind/name``.
    :raises RestorePointError: ``restore-point-writer-unsupported`` when a
        writer of a touched claim cannot be stopped (a DaemonSet, Job or
        CronJob, or a workload the release does not declare).
    """
    hooks = hooks or {}
    wanted = {
        _ref(item): item for item in desired if item.get("kind") in WORKLOAD_KINDS
    }
    current = {_ref(item): item for item in live if item.get("kind") in WORKLOAD_KINDS}
    existing = {
        str((item.get("metadata") or {}).get("name")): item
        for item in claims
        if (item.get("status") or {}).get("phase", "Bound") == "Bound"
        and not (item.get("metadata") or {}).get("deletionTimestamp")
    }
    result = RestorePlan()
    touched: dict[str, dict[str, Any]] = {}
    for ref, manifest in sorted(wanted.items()):
        live_manifest = current.get(ref)
        why = changes(manifest, live_manifest, pending=pending)
        if not why:
            continue
        names = {
            claim: ordinal
            for source in (manifest, live_manifest)
            if source is not None
            for claim, ordinal in _claim_names(source, existing)
        }
        if not names:
            result.unchanged_claims.append(ref)
            continue
        for claim, ordinal in sorted(names.items()):
            entry = touched.setdefault(
                claim,
                {"claim": claim, "workload": ref, "ordinal": ordinal, "why": []},
            )
            entry["why"] = sorted({*entry["why"], *why})
    # Every workload (declared or live) that writes a touched claim stops.
    writers: dict[str, dict[str, Any]] = {}
    for ref in sorted({*wanted, *current}):
        sources = [item for item in (wanted.get(ref), current.get(ref)) if item]
        written = sorted(
            {
                claim
                for source in sources
                for claim, _ordinal in _claim_names(source, existing)
                if claim in touched
            }
        )
        if not written:
            continue
        kind = ref.split("/", 1)[0]
        if kind not in STOPPABLE or ref not in wanted:
            raise RestorePointError(
                "restore-point-writer-unsupported",
                f"{ref} writes claim {written[0]}, which the release touches, "
                "and Piceli cannot stop it for a consistent copy (only "
                "Deployments and StatefulSets the release declares); stop it "
                "first or mount the claim read-only",
            )
        live_manifest = current.get(ref)
        replicas = (
            int(((live_manifest.get("spec") or {}).get("replicas", 1)) or 0)
            if live_manifest is not None
            else 0
        )
        writers[ref] = {
            "workload": ref,
            "kind": kind,
            "name": ref.split("/", 1)[1],
            "replicas": replicas,
            "live": live_manifest is not None,
            "claims": written,
            "quiesce": list(hooks.get(ref, ())),
        }
    result.claims = [touched[name] for name in sorted(touched)]
    result.writers = [writers[name] for name in sorted(writers)]
    return result


def writer_pods(pods: Iterable[Mapping[str, Any]], claims: Iterable[str]) -> list[str]:
    """Pods that may still write one of ``claims``: every pod that mounts it
    writably and has not finished, terminating pods included."""
    wanted = set(claims)
    found = []
    for pod in pods:
        if (pod.get("status") or {}).get("phase") in {"Succeeded", "Failed"}:
            continue
        spec = pod.get("spec") or {}
        volumes = {
            str(volume.get("name")): volume["persistentVolumeClaim"]
            for volume in spec.get("volumes") or ()
            if isinstance(volume, dict)
            and isinstance(volume.get("persistentVolumeClaim"), dict)
        }
        for container in _containers(spec):
            hit = False
            for mount in container.get("volumeMounts") or ():
                claim = volumes.get(str(mount.get("name")))
                if (
                    claim is not None
                    and claim.get("claimName") in wanted
                    and not claim.get("readOnly")
                    and not mount.get("readOnly")
                ):
                    hit = True
                    break
            if hit:
                found.append(str((pod.get("metadata") or {}).get("name")))
                break
    return sorted(found)
