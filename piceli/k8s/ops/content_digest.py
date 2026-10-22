"""Roll workloads when the ConfigMaps or Secrets they read change.

A pod reads a Secret or ConfigMap when it starts: environment variables
(``env.valueFrom``, ``envFrom``) never change in a running container, and many
programs read a mounted file only once. Kubernetes rolls a Deployment,
StatefulSet or DaemonSet only when its pod template changes, so a changed
value would otherwise reach the pods only at their next restart.

:func:`content_digests` writes, on the pod template of every such workload,
a digest of the content of the ConfigMaps and Secrets *of the same
composition* that the pods read (volumes, projected volumes, ``envFrom`` and
``env.valueFrom`` of every container), when the composition declares their
content (an object declared with its metadata only, such as a Secret adopted
without its value, is left out):

``piceli.io/configmaps-digest``
    ``sha256:<hex>`` of the consumed ConfigMaps' ``data`` and ``binaryData``.
    ConfigMaps are public, so a plain digest reveals nothing new.

``piceli.io/secrets-digest``
    ``sha256:<hex>`` of the consumed Secrets' ``type``, key names and one
    token per secret-bound value. The token comes from the caller: the
    release engine passes ``SecretVersionStore.fingerprint``, an
    HMAC-SHA256 of the stored value under a random key that never leaves the
    private store, so the annotation changes exactly when a value changes and
    says nothing about the value without that key. Offline renders use the
    opaque version id of the reference (:func:`version_token`), which holds
    no value at all; a plan observes the cluster with those, then captures
    the server dry runs of the workloads again once their final digests are
    known, so the evidence is for exactly the manifest that is applied.

Unchanged content gives the same annotations, so the pod template does not
change and nothing rolls. A workload whose own metadata carries
``piceli.io/roll-on-change: "false"`` (``roll_on_change=False`` on a typed
App workload) is left as declared, for programs that reload their
configuration themselves; :func:`strip_roll_markers` removes that marker
before anything is applied. Jobs and CronJobs are not annotated: each run
starts new pods that read the current content, and a Job's pod template is
immutable.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    ResourceIntent,
    ResourceRef,
)
from piceli.k8s.ops.secret_versions import SecretVersionRef

#: Pod template annotation: digest of the consumed ConfigMaps' content.
CONFIG_DIGEST_ANNOTATION = "piceli.io/configmaps-digest"
#: Pod template annotation: keyed digest of the consumed Secrets' content.
SECRET_DIGEST_ANNOTATION = "piceli.io/secrets-digest"
#: Workload annotation; ``"false"`` turns the digests off for that workload.
ROLL_ON_CHANGE_ANNOTATION = "piceli.io/roll-on-change"
#: Kinds whose pods roll when the pod template changes.
ROLLED_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet"})

#: Turns a secret version into a value-free token that changes with the value.
Token = Callable[[SecretVersionRef], str]

_DIGESTS = frozenset({CONFIG_DIGEST_ANNOTATION, SECRET_DIGEST_ANNOTATION})


def version_token(reference: SecretVersionRef) -> str:
    """The opaque version id: no value, but it changes with every new version."""
    return "version:" + reference.version


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _name(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _pod_containers(pod: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    for field in ("initContainers", "containers", "ephemeralContainers"):
        for item in pod.get(field) or ():
            if isinstance(item, Mapping):
                yield item


def pod_references(pod: Mapping[str, Any]) -> set[tuple[str, str]]:
    """``("ConfigMap"|"Secret", name)`` of every object a pod spec reads.

    Volumes (``configMap``, ``secret``, ``projected`` sources) and, in every
    container, ``env[].valueFrom`` (``configMapKeyRef``, ``secretKeyRef``) and
    ``envFrom`` (``configMapRef``, ``secretRef``). Image pull secrets are not
    read by the pods and are left out.
    """
    refs: set[tuple[str, str]] = set()

    def add(kind: str, value: Any) -> None:
        name = _name(value)
        if name is not None:
            refs.add((kind, name))

    for volume in pod.get("volumes") or ():
        if not isinstance(volume, Mapping):
            continue
        add("ConfigMap", (volume.get("configMap") or {}).get("name"))
        add("Secret", (volume.get("secret") or {}).get("secretName"))
        for source in (volume.get("projected") or {}).get("sources") or ():
            if isinstance(source, Mapping):
                add("ConfigMap", (source.get("configMap") or {}).get("name"))
                add("Secret", (source.get("secret") or {}).get("name"))
    for container in _pod_containers(pod):
        for item in container.get("env") or ():
            source = item.get("valueFrom") if isinstance(item, Mapping) else None
            if isinstance(source, Mapping):
                add("ConfigMap", (source.get("configMapKeyRef") or {}).get("name"))
                add("Secret", (source.get("secretKeyRef") or {}).get("name"))
        for item in container.get("envFrom") or ():
            if isinstance(item, Mapping):
                add("ConfigMap", (item.get("configMapRef") or {}).get("name"))
                add("Secret", (item.get("secretRef") or {}).get("name"))
    return refs


def _template(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    if manifest.get("apiVersion") != "apps/v1" or manifest.get("kind") not in (
        ROLLED_KINDS
    ):
        return None
    template = (manifest.get("spec") or {}).get("template")
    return template if isinstance(template, dict) else None


def rolls_on_change(manifest: Mapping[str, Any]) -> bool:
    """Whether the workload has not opted out (``piceli.io/roll-on-change``)."""
    annotations = (manifest.get("metadata") or {}).get("annotations") or {}
    return str(annotations.get(ROLL_ON_CHANGE_ANNOTATION, "true")).lower() != "false"


def _config_content(resource: ResourceIntent) -> dict[str, Any]:
    manifest = resource.manifest
    return {
        "data": manifest.get("data") or {},
        "binaryData": manifest.get("binaryData") or {},
    }


def _secret_content(resource: ResourceIntent, token: Token) -> dict[str, Any]:
    """Type, key names and one token per bound value; never a literal value."""
    manifest = resource.manifest
    keys = sorted({*(manifest.get("data") or {}), *(manifest.get("stringData") or {})})
    values = {
        binding.json_pointer: token(binding.reference)
        for binding in resource.secret_bindings
    }
    return {
        "type": manifest.get("type") or "Opaque",
        "keys": keys,
        "values": dict(sorted(values.items())),
    }


def _declares_content(resource: ResourceIntent) -> bool:
    """Whether the composition declares any key of the object's content.

    An object declared with its metadata only (a Secret adopted without its
    value, whose content another tool manages) has no content Piceli knows:
    a digest of it would never change, so it only adds an annotation.
    """
    manifest = resource.manifest
    fields = (
        ("data", "binaryData")
        if resource.ref.kind == "ConfigMap"
        else (
            "data",
            "stringData",
        )
    )
    return any(manifest.get(field) for field in fields)


def _digests(
    pod: Mapping[str, Any],
    namespace: str,
    objects: Mapping[tuple[str, str, str], ResourceIntent],
    token: Token,
) -> dict[str, str]:
    configs: dict[str, Any] = {}
    secrets: dict[str, Any] = {}
    for kind, name in sorted(pod_references(pod)):
        resource = objects.get((kind, namespace, name))
        if resource is None:
            continue  # not part of this composition: never ours to roll for
        if not _declares_content(resource):
            continue  # metadata only: its content is managed elsewhere
        if kind == "ConfigMap":
            configs[name] = _config_content(resource)
        else:
            secrets[name] = _secret_content(resource, token)
    result = {}
    if configs:
        result[CONFIG_DIGEST_ANNOTATION] = _sha256(configs)
    if secrets:
        result[SECRET_DIGEST_ANNOTATION] = _sha256(secrets)
    return result


def _rebuilt(resource: ResourceIntent, manifest: dict[str, Any]) -> ResourceIntent:
    rebuilt = ResourceIntent.from_manifest(manifest, resource.dependencies)
    if rebuilt.ref != resource.ref:  # pragma: no cover - annotations keep the ref
        raise ValueError(f"content digest changed the identity of {resource.ref}")
    for binding in resource.secret_bindings:
        rebuilt = rebuilt.with_secret(binding.json_pointer, binding.reference)
    return rebuilt


def _annotated(resource: ResourceIntent, digests: Mapping[str, str]) -> ResourceIntent:
    """``resource`` with exactly ``digests`` as its digest annotations."""
    manifest = resource.manifest
    template = _template(manifest)
    if template is None:
        return resource
    metadata = template.setdefault("metadata", {})
    annotations = dict(metadata.get("annotations") or {})
    before = dict(annotations)
    for key in _DIGESTS:
        annotations.pop(key, None)
    annotations.update(digests)
    if annotations == before:
        return resource
    if annotations:
        metadata["annotations"] = annotations
    else:
        metadata.pop("annotations", None)
    return _rebuilt(resource, manifest)


def _map(
    composition: DeploymentComposition,
    change: Callable[[ResourceIntent], ResourceIntent],
) -> DeploymentComposition:
    components = []
    changed = False
    for component in composition.components:
        resources = tuple(change(resource) for resource in component.resources)
        changed = changed or resources != component.resources
        components.append(
            DeploymentComponent(component.name, resources, component.dependencies)
        )
    return DeploymentComposition(tuple(components)) if changed else composition


def content_digests(
    composition: DeploymentComposition,
    token: Token = version_token,
    *,
    refresh: bool = False,
) -> DeploymentComposition:
    """Annotate each workload's pod template with the content it reads.

    Idempotent: the digest annotations are recomputed from scratch. With
    ``refresh`` only workloads that already carry a digest are recomputed:
    the release engine uses it to replace the offline tokens with keyed
    fingerprints of the stored values after the opt-out markers are gone
    (see :func:`strip_roll_markers`).
    """
    objects = {
        (resource.ref.kind, resource.ref.namespace, resource.ref.name): resource
        for component in composition.components
        for resource in component.resources
        if resource.ref.api_version == "v1"
        and resource.ref.kind in ("ConfigMap", "Secret")
    }

    def change(resource: ResourceIntent) -> ResourceIntent:
        manifest = resource.manifest
        template = _template(manifest)
        if template is None or not rolls_on_change(manifest):
            return resource  # opted out: its annotations are left as declared
        if refresh and not _DIGESTS & set(
            (template.get("metadata") or {}).get("annotations") or {}
        ):
            return resource
        digests = _digests(
            template.get("spec") or {}, resource.ref.namespace, objects, token
        )
        return _annotated(resource, digests)

    return _map(composition, change)


def _secret_digest(manifest: Mapping[str, Any]) -> str | None:
    template = _template(manifest)
    if template is None:
        return None
    value = ((template.get("metadata") or {}).get("annotations") or {}).get(
        SECRET_DIGEST_ANNOTATION
    )
    return value if isinstance(value, str) else None


def carry_live_secret_digests(
    composition: DeploymentComposition, live: Mapping[ResourceRef, Mapping[str, Any]]
) -> DeploymentComposition:
    """Keep the live ``piceli.io/secrets-digest`` where values are not known.

    A preview of a new release (``diff``, a placeholder preview) has no
    secret values, only stand-in references, so it cannot tell whether a
    Secret's content changes. Like the Secrets themselves, a consumer is then
    shown with its live secret digest; the plan of the release computes the
    real one.
    """

    def change(resource: ResourceIntent) -> ResourceIntent:
        current = live.get(resource.ref)
        desired = _secret_digest(resource.manifest)
        if current is None or desired is None:
            return resource
        kept = _secret_digest(current)
        if kept is None or kept == desired:
            return resource
        manifest = resource.manifest
        template = _template(manifest)
        assert template is not None
        template["metadata"]["annotations"][SECRET_DIGEST_ANNOTATION] = kept
        return _rebuilt(resource, manifest)

    return _map(composition, change)


def has_secret_digests(composition: DeploymentComposition) -> bool:
    """Whether any workload carries a ``piceli.io/secrets-digest``."""
    return any(
        _secret_digest(resource.manifest) is not None
        for component in composition.components
        for resource in component.resources
    )


def strip_roll_markers(composition: DeploymentComposition) -> DeploymentComposition:
    """Remove ``piceli.io/roll-on-change`` from every workload.

    The marker is an instruction to :func:`content_digests`, not cluster
    state: the release engine removes it once the digests are computed, so
    opting out (or importing a live namespace) never changes the object.
    """

    def change(resource: ResourceIntent) -> ResourceIntent:
        manifest = resource.manifest
        if _template(manifest) is None:
            return resource
        metadata = manifest.get("metadata") or {}
        annotations = dict(metadata.get("annotations") or {})
        if annotations.pop(ROLL_ON_CHANGE_ANNOTATION, None) is None:
            return resource
        if annotations:
            metadata["annotations"] = annotations
        else:
            metadata.pop("annotations", None)
        return _rebuilt(resource, manifest)

    return _map(composition, change)
