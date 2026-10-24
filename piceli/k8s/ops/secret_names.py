"""Find and rewrite the Secret names objects refer to (0.27.0).

A versioned Secret (``app.secret(..., versioned=True)``) is rendered under a
content-addressed name, ``<name>-<hash>``. Every object that refers to it by
its declared name is rewritten to that name in the same render, and a plan
deletes an old version once nothing mounts it. These helpers read and
rewrite those references on plain manifests: pod specs (volumes, projected
volumes, ``env[].valueFrom.secretKeyRef``, ``envFrom[].secretRef`` and
``imagePullSecrets``) and an Ingress's ``spec.tls[].secretName``.

They import nothing from the planner, so both the planner and the renderer
can use them.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

#: Secret annotation: the declared (logical) name of a versioned Secret.
VERSIONED_SECRET_ANNOTATION = "piceli.io/versioned-from"


def versioned_logical(manifest: Mapping[str, Any]) -> str | None:
    """The declared name of a versioned Secret, or ``None`` for any other object."""
    if manifest.get("apiVersion") != "v1" or manifest.get("kind") != "Secret":
        return None
    annotations = (manifest.get("metadata") or {}).get("annotations") or {}
    value = annotations.get(VERSIONED_SECRET_ANNOTATION)
    return value if isinstance(value, str) and value else None


def pod_specs(manifest: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """The pod spec of a workload manifest (Pod, CronJob, any ``spec.template``)."""
    spec = manifest.get("spec")
    if not isinstance(spec, dict):
        return
    if manifest.get("kind") == "Pod":
        yield spec
        return
    if manifest.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    template = spec.get("template") if isinstance(spec, dict) else None
    pod = template.get("spec") if isinstance(template, dict) else None
    if isinstance(pod, dict):
        yield pod


def _containers(pod: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    for field in ("initContainers", "containers", "ephemeralContainers"):
        for item in pod.get(field) or ():
            if isinstance(item, dict):
                yield item


def _slots(manifest: Mapping[str, Any]) -> Iterator[tuple[dict[str, Any], str]]:
    """``(mapping, key)`` of every place that holds a Secret name."""
    for pod in pod_specs(manifest):
        for volume in pod.get("volumes") or ():
            if not isinstance(volume, dict):
                continue
            if isinstance(volume.get("secret"), dict):
                yield volume["secret"], "secretName"
            for source in (volume.get("projected") or {}).get("sources") or ():
                if isinstance(source, dict) and isinstance(source.get("secret"), dict):
                    yield source["secret"], "name"
        for container in _containers(pod):
            for item in container.get("env") or ():
                source = item.get("valueFrom") if isinstance(item, dict) else None
                if isinstance(source, dict) and isinstance(
                    source.get("secretKeyRef"), dict
                ):
                    yield source["secretKeyRef"], "name"
            for item in container.get("envFrom") or ():
                if isinstance(item, dict) and isinstance(item.get("secretRef"), dict):
                    yield item["secretRef"], "name"
        for item in pod.get("imagePullSecrets") or ():
            if isinstance(item, dict):
                yield item, "name"
    if manifest.get("kind") == "Ingress":
        for item in (manifest.get("spec") or {}).get("tls") or ():
            if isinstance(item, dict):
                yield item, "secretName"


def secret_names(manifest: Mapping[str, Any]) -> set[str]:
    """Names of every Secret the object refers to (see the module docstring)."""
    return {
        value
        for mapping, key in _slots(manifest)
        if isinstance(value := mapping.get(key), str) and value
    }


def rename_secret_refs(manifest: dict[str, Any], names: Mapping[str, str]) -> bool:
    """Rewrite references to the keys of ``names`` in place; whether any changed."""
    changed = False
    for mapping, key in _slots(manifest):
        value = mapping.get(key)
        if isinstance(value, str) and value in names:
            mapping[key] = names[value]
            changed = True
    return changed
