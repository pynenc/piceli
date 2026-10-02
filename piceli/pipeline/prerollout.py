"""Pre-rollout checks, pure part: what a check Job is made of.

A check Job is derived from the rendered manifest of the workload it guards
(with the new image already resolved), so it carries exactly the settings the
real pods will: environment, Secret and ConfigMap references and mounts,
security context, service account, resources and node selector. Nothing here
contacts a cluster; :mod:`piceli.pipeline.prerollout_cluster` runs the Jobs.

What a check pod deliberately does **not** carry:

* the workload's labels and selector (a Service or NetworkPolicy must never
  select a check pod),
* probes, ports, lifecycle hooks, sidecars, init containers,
* claims: for the configuration check they become empty directories; for the
  upgrade check the retained claims are mounted read-only.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from piceli.pipeline.compose import (
    composition_function,
    offline_composition,
    preview_context,
)

if TYPE_CHECKING:
    from piceli.app.prerollout import PreRollout
    from piceli.k8s.ops.plan import DeploymentComposition
    from piceli.k8s.release_spec import ImageRef
    from piceli.pipeline.model import Pipeline

#: Label on every check Job: its value is ``config`` or ``upgrade``.
CHECK_LABEL = "piceli.io/pre-rollout"
APP_LABEL = "piceli.io/pre-rollout-app"
RUN_LABEL = "piceli.io/pre-rollout-run"
#: Seconds a finished check Job may linger if the deployer died before removing it.
TTL_SECONDS = 600
#: Extra seconds the deployer waits beyond a Job's own deadline.
DEADLINE_SLACK_SECONDS = 30
WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")
#: Volume sources a check pod keeps as they are; any other becomes an empty directory.
KEPT_VOLUMES = ("secret", "configMap", "projected", "downwardAPI", "emptyDir")
_POD_KEYS = (
    "serviceAccountName",
    "automountServiceAccountToken",
    "securityContext",
    "nodeSelector",
    "tolerations",
    "imagePullSecrets",
    "runtimeClassName",
    "priorityClassName",
    "shareProcessNamespace",
)
_CONTAINER_KEYS = (
    "image",
    "imagePullPolicy",
    "workingDir",
    "env",
    "envFrom",
    "resources",
    "securityContext",
)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ------------------------------------------------------------- rendering


@dataclass(frozen=True)
class Rendered:
    """The app rendered for the checks: workload manifests and declared config objects."""

    workloads: Mapping[str, dict[str, Any]]
    declared: frozenset[tuple[str, str]]


def _collect(composition: DeploymentComposition) -> Rendered:
    workloads: dict[str, dict[str, Any]] = {}
    declared: set[tuple[str, str]] = set()
    for component in composition.components:
        for resource in component.resources:
            kind, name = resource.ref.kind, resource.ref.name
            if kind in {"Secret", "ConfigMap"}:
                declared.add((kind, name))
            elif kind in WORKLOAD_KINDS:
                workloads[name] = resource.manifest
    return Rendered(workloads, frozenset(declared))


def render_offline(pipeline: Pipeline) -> Rendered:
    """The app as declared, build handles unresolved (plan time)."""
    return _collect(offline_composition(pipeline)[1])


def render_delivered(pipeline: Pipeline, images: Mapping[str, ImageRef]) -> Rendered:
    """The app with the delivered image references: the pods the release creates."""
    ctx = preview_context(pipeline)
    from dataclasses import replace

    ctx = replace(ctx, images=MappingProxyType(dict(images)))
    return _collect(composition_function(pipeline)(ctx))


# ------------------------------------------------------------ references


@dataclass(frozen=True)
class Reference:
    """A Secret or ConfigMap a workload's main container reads."""

    kind: str
    name: str
    key: str | None
    optional: bool
    via: str

    def label(self) -> str:
        return f"{self.kind}/{self.name}" + (f" key {self.key}" if self.key else "")

    def public(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            **({"key": self.key} if self.key else {}),
            "via": self.via,
        }


def _pod(manifest: Mapping[str, Any]) -> dict[str, Any]:
    template = manifest.get("spec", {}).get("template", {})
    pod = template.get("spec") if isinstance(template, dict) else None
    return pod if isinstance(pod, dict) else {}


def main_container(manifest: Mapping[str, Any]) -> dict[str, Any]:
    containers = _pod(manifest).get("containers") or []
    return containers[0] if containers else {}


def _mounted_volumes(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    mounts = {
        item.get("name") for item in main_container(manifest).get("volumeMounts") or []
    }
    return [
        item
        for item in _pod(manifest).get("volumes") or []
        if isinstance(item, dict) and item.get("name") in mounts
    ]


def references(manifest: Mapping[str, Any]) -> list[Reference]:
    """Every Secret and ConfigMap the main container's env and mounts read."""
    found: list[Reference] = []
    container = main_container(manifest)
    for item in container.get("env") or []:
        source = (item.get("valueFrom") or {}) if isinstance(item, dict) else {}
        for field, kind in (
            ("secretKeyRef", "Secret"),
            ("configMapKeyRef", "ConfigMap"),
        ):
            ref = source.get(field)
            if isinstance(ref, dict):
                found.append(
                    Reference(
                        kind,
                        str(ref.get("name")),
                        str(ref.get("key")),
                        bool(ref.get("optional")),
                        "env",
                    )
                )
    for item in container.get("envFrom") or []:
        for field, kind in (("secretRef", "Secret"), ("configMapRef", "ConfigMap")):
            ref = item.get(field) if isinstance(item, dict) else None
            if isinstance(ref, dict):
                found.append(
                    Reference(
                        kind,
                        str(ref.get("name")),
                        None,
                        bool(ref.get("optional")),
                        "envFrom",
                    )
                )
    for volume in _mounted_volumes(manifest):
        sources = [volume]
        projected = volume.get("projected")
        if isinstance(projected, dict):
            sources = [
                item
                for item in projected.get("sources") or []
                if isinstance(item, dict)
            ]
        for source in sources:
            secret = source.get("secret")
            config = source.get("configMap")
            for kind, ref, name in (
                (
                    "Secret",
                    secret,
                    secret.get("secretName") or secret.get("name")
                    if isinstance(secret, dict)
                    else None,
                ),
                (
                    "ConfigMap",
                    config,
                    config.get("name") if isinstance(config, dict) else None,
                ),
            ):
                if not isinstance(ref, dict) or name is None:
                    continue
                keys = [
                    str(entry.get("key"))
                    for entry in ref.get("items") or []
                    if isinstance(entry, dict)
                ]
                optional = bool(ref.get("optional"))
                if keys:
                    found.extend(
                        Reference(kind, str(name), key, optional, "volume")
                        for key in keys
                    )
                else:
                    found.append(Reference(kind, str(name), None, optional, "volume"))
    return found


# --------------------------------------------------------------- planning


def claim_names(
    manifest: Mapping[str, Any], ordinal: int | None = None
) -> dict[str, str]:
    """Mount path -> claim name of the main container's retained claims.

    A StatefulSet's claim template ``data`` is ``data-<set>-<ordinal>``; an
    existing claim keeps its name. ``ordinal`` is required for templates.
    """
    templates = {
        item.get("metadata", {}).get("name")
        for item in manifest.get("spec", {}).get("volumeClaimTemplates") or []
        if isinstance(item, dict)
    }
    claims = {
        item.get("name"): item["persistentVolumeClaim"].get("claimName")
        for item in _pod(manifest).get("volumes") or []
        if isinstance(item, dict)
        and isinstance(item.get("persistentVolumeClaim"), dict)
    }
    result: dict[str, str] = {}
    for mount in main_container(manifest).get("volumeMounts") or []:
        volume = mount.get("name")
        if volume in templates:
            if ordinal is None:
                continue
            result[mount["mountPath"]] = (
                f"{volume}-{manifest['metadata']['name']}-{ordinal}"
            )
        elif volume in claims:
            result[mount["mountPath"]] = str(claims[volume])
    return result


def replicas(manifest: Mapping[str, Any]) -> int:
    value = manifest.get("spec", {}).get("replicas")
    return int(value) if isinstance(value, int) else 1


def describe_checks(
    declared: Sequence[PreRollout], rendered: Rendered
) -> list[dict[str, Any]]:
    """The plan's view of each declaration: what runs, with which mounts (no values)."""
    described = []
    for item in declared:
        manifest = rendered.workloads.get(item.workload)
        entry = item.describe()
        if manifest is None:
            entry["state"] = "workload-not-rendered"
            described.append(entry)
            continue
        refs = references(manifest)
        entry["kind"] = manifest["kind"]
        entry["image"] = main_container(manifest).get("image")
        entry["uses"] = [
            {
                **ref.public(),
                "source": "release"
                if (ref.kind, ref.name) in rendered.declared
                else "cluster",
            }
            for ref in refs
        ]
        if item.upgrade is not None:
            entry["upgrade"]["claims"] = claim_paths(manifest, item)
        described.append(entry)
    return described


def claim_paths(manifest: Mapping[str, Any], item: PreRollout) -> list[str]:
    """The mount paths the upgrade check opens read-only."""
    assert item.upgrade is not None
    paths = sorted(set(claim_names(manifest, 0)) | set(claim_names(manifest)))
    if item.upgrade.volumes:
        paths = [path for path in paths if path in item.upgrade.volumes]
    return paths


# ---------------------------------------------------------------- the Job


def _short(value: str, length: int) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower())[:length].strip("-")


def job_name(workload: str, kind: str, ordinal: int | None, run: str) -> str:
    """A Job name of at most 52 characters (pod names add a suffix)."""
    tag = "cfg" if kind == "config" else f"up{ordinal or 0}"
    return f"{_short(workload, 30)}-{tag}-{_short(run[-8:], 8)}"


def _emptydir(name: str) -> dict[str, Any]:
    return {"name": name, "emptyDir": {}}


def check_job(
    manifest: Mapping[str, Any],
    *,
    app: str,
    namespace: str,
    kind: str,
    command: Sequence[str],
    timeout_seconds: int,
    run: str,
    ordinal: int | None = None,
    claims: Mapping[str, str] | None = None,
    node: str | None = None,
    optional: Iterable[tuple[str, str]] = (),
    renamed: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, Any]:
    """The check Job of ``manifest``'s workload (see the module docstring).

    :param kind: ``config`` (claims become empty directories) or ``upgrade``
        (the claims in ``claims``, mount path -> claim name, are mounted
        read-only).
    :param node: Schedule on this node (a running pod already holds a
        ReadWriteOnce claim there).
    :param optional: ``(kind, name)`` of Secrets and ConfigMaps that do not
        exist yet because this release creates them; they are marked optional
        so the check still runs.
    :param renamed: ``(kind, name)`` -> name of a staged copy: references to
        a Secret or ConfigMap this release creates read the copy instead.
    """
    pod = _pod(manifest)
    main = main_container(manifest)
    skip = set(optional)
    claims = dict(claims or {})
    retained = set(claims)
    run_label = _short(run[-8:], 8)
    spec: dict[str, Any] = {
        key: json.loads(canonical(pod[key])) for key in _POD_KEYS if key in pod
    }
    spec["restartPolicy"] = "Never"
    spec["terminationGracePeriodSeconds"] = 5
    container: dict[str, Any] = {
        "name": "check",
        **{
            key: json.loads(canonical(main[key]))
            for key in _CONTAINER_KEYS
            if key in main
        },
        "command": list(command),
    }
    _mark_optional(container, skip, renamed)
    mounts: list[dict[str, Any]] = []
    volumes: dict[str, dict[str, Any]] = {}
    pod_volumes = {
        item.get("name"): item
        for item in pod.get("volumes") or []
        if isinstance(item, dict)
    }
    templates = {
        item.get("metadata", {}).get("name")
        for item in manifest.get("spec", {}).get("volumeClaimTemplates") or []
        if isinstance(item, dict)
    }
    for mount in main.get("volumeMounts") or []:
        entry = dict(mount)
        name = entry["name"]
        path = entry["mountPath"]
        source = pod_volumes.get(name)
        if kind == "upgrade" and path in retained:
            claim = claims[path]
            entry["readOnly"] = True
            volumes[name] = {
                "name": name,
                "persistentVolumeClaim": {"claimName": claim, "readOnly": True},
            }
        elif source is not None and any(key in source for key in KEPT_VOLUMES):
            volumes[name] = _optional_volume(
                json.loads(canonical(source)), skip, renamed
            )
        elif source is not None or name in templates:
            volumes[name] = _emptydir(name)
        else:
            continue
        mounts.append(entry)
    if mounts:
        container["volumeMounts"] = mounts
    spec["containers"] = [container]
    if volumes:
        spec["volumes"] = list(volumes.values())
    if node is not None:
        spec["affinity"] = {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchFields": [
                                {
                                    "key": "metadata.name",
                                    "operator": "In",
                                    "values": [node],
                                }
                            ]
                        }
                    ]
                }
            }
        }
    labels = {CHECK_LABEL: kind, APP_LABEL: _short(app, 63), RUN_LABEL: run_label}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name(manifest["metadata"]["name"], kind, ordinal, run),
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "completions": 1,
            "parallelism": 1,
            "backoffLimit": 0,
            "activeDeadlineSeconds": timeout_seconds,
            "ttlSecondsAfterFinished": TTL_SECONDS,
            "template": {"metadata": {"labels": dict(labels)}, "spec": spec},
        },
    }


def _adjust(
    ref: Any,
    kind: str,
    field: str,
    skip: set[tuple[str, str]],
    renamed: Mapping[tuple[str, str], str],
) -> None:
    """Point ``ref`` at a staged copy, or mark it optional when it is skipped."""
    if not isinstance(ref, dict):
        return
    key = (kind, ref.get(field))
    if key in renamed:
        ref[field] = renamed[key]  # type: ignore[index]
    elif key in skip:
        ref["optional"] = True


def _mark_optional(
    container: dict[str, Any],
    skip: set[tuple[str, str]],
    renamed: Mapping[tuple[str, str], str] | None = None,
) -> None:
    renamed = renamed or {}
    if not skip and not renamed:
        return
    for item in container.get("env") or []:
        source = item.get("valueFrom") or {}
        for field, kind in (
            ("secretKeyRef", "Secret"),
            ("configMapKeyRef", "ConfigMap"),
        ):
            _adjust(source.get(field), kind, "name", skip, renamed)
    for item in container.get("envFrom") or []:
        for field, kind in (("secretRef", "Secret"), ("configMapRef", "ConfigMap")):
            _adjust(item.get(field), kind, "name", skip, renamed)


def _optional_volume(
    volume: dict[str, Any],
    skip: set[tuple[str, str]],
    renamed: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, Any]:
    renamed = renamed or {}
    if not skip and not renamed:
        return volume
    _adjust(volume.get("secret"), "Secret", "secretName", skip, renamed)
    _adjust(volume.get("configMap"), "ConfigMap", "name", skip, renamed)
    for source in (volume.get("projected") or {}).get("sources") or []:
        if isinstance(source, dict):
            _adjust(source.get("secret"), "Secret", "name", skip, renamed)
            _adjust(source.get("configMap"), "ConfigMap", "name", skip, renamed)
    return volume


#: Label value of a staged copy of a release's own Secret or ConfigMap.
STAGED = "staged"
#: A Secret type a copy cannot keep (the API server fills such a Secret).
SERVICE_ACCOUNT_TOKEN = "kubernetes.io/service-account-token"


def staged_name(workload: str, run: str, kind: str, index: int) -> str:
    """The name of a check's copy of a release-owned Secret or ConfigMap."""
    tag = "s" if kind == "Secret" else "c"
    return f"{_short(workload, 30)}-chk{tag}{index}-{_short(run[-8:], 8)}"


def staged_copy(
    manifest: Mapping[str, Any], *, name: str, namespace: str, app: str, run: str
) -> dict[str, Any]:
    """A labelled copy of a Secret or ConfigMap under ``name``, with its data.

    Only the data and (for a Secret) the type are kept: never the owner
    annotations or labels of the release, so the copy is no part of it.
    """
    kind = str(manifest["kind"])
    body: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                CHECK_LABEL: STAGED,
                APP_LABEL: _short(app, 63),
                RUN_LABEL: _short(run[-8:], 8),
            },
        },
    }
    for key in ("data", "binaryData", "stringData"):
        if isinstance(manifest.get(key), dict):
            body[key] = json.loads(canonical(manifest[key]))
    kept_type = manifest.get("type")
    if kind == "Secret" and kept_type and kept_type != SERVICE_ACCOUNT_TOKEN:
        body["type"] = kept_type
    return body



def staged_values(manifest: Mapping[str, Any]) -> list[str]:
    """The decoded values of a staged Secret, for redaction only."""
    if manifest.get("kind") != "Secret":
        return []
    import base64

    values: list[str] = []
    for value in (manifest.get("data") or {}).values():
        try:
            values.append(base64.b64decode(str(value)).decode())
        except Exception:
            continue
    values.extend(str(value) for value in (manifest.get("stringData") or {}).values())
    return values


def job_digest(job: Mapping[str, Any]) -> str:
    """A digest of a check Job without its run-specific names and labels."""
    body = json.loads(canonical(job))
    body["metadata"].pop("name", None)
    body["metadata"].pop("labels", None)
    body["spec"]["template"].pop("metadata", None)
    return "sha256:" + hashlib.sha256(canonical(body).encode()).hexdigest()


# --------------------------------------------------------------- scrubbing

LOG_LINES = 30
LOG_LINE_CHARS = 300
LOG_CHARS = 4000
_SENSITIVE = re.compile(
    r"(?i)((?:pass(?:word|wd)?|secret|token|api[_-]?key|authorization|bearer|credential)"
    r"[\w.-]*\s*[:=]\s*)(\S+)"
)
_SCHEME = re.compile(r"(?i)\b(bearer|basic)\s+\S+")
_TOKEN = re.compile(r"\b[A-Za-z0-9+_=-]{24,}\b")


def scrub(text: str, values: Iterable[str] = ()) -> str:
    """A bounded tail of ``text`` with secret values and token-like strings removed.

    Best effort, in three passes: every known secret ``values`` (4+ characters)
    is replaced first; then ``key=value`` pairs whose key names a password,
    token or credential; then any long run of token characters. Only the last
    :data:`LOG_LINES` lines are kept, each cut at :data:`LOG_LINE_CHARS`.
    """
    lines = text.replace("\r", "").splitlines()[-LOG_LINES:]
    known = sorted(
        {value for value in values if len(value) >= 4}, key=len, reverse=True
    )
    kept = []
    for line in lines:
        for value in known:
            line = line.replace(value, "<redacted>")
        line = _SCHEME.sub(lambda match: match[1] + " <redacted>", line)
        line = _SENSITIVE.sub(lambda match: match[1] + "<redacted>", line)
        line = _TOKEN.sub("<redacted>", line)
        kept.append(line[:LOG_LINE_CHARS])
    return "\n".join(kept)[-LOG_CHARS:]
