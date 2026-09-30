"""Render-time isolation of a branch environment.

:func:`isolate` takes the app as rendered into the branch namespace and

* substitutes prebuilt images for build handles (pinning workloads to the
  delivery node like a deploy does), and mirrored references;
* refuses what would reach across namespaces or onto the node: a Service
  named absolutely in another namespace (``api.shop.svc.cluster.local``),
  ``NodePort``/``LoadBalancer`` Services, ``hostPort``/``hostNetwork``,
  ``hostPath`` volumes, claims bound to a volume the app does not declare,
  role bindings and network policy peers in other namespaces, and cluster
  objects that cannot be renamed (``CustomResourceDefinition``,
  ``Namespace``);
* gives every other cluster-scoped object a namespace-qualified name
  (``reader`` → ``reader-shop-wp-login``) and rewrites the references to it;
* sets the declared claim sizes;
* adds a default-deny ``NetworkPolicy`` across namespaces (same namespace
  and cluster DNS allowed) and a ``ResourceQuota``.

The main branch gets only the image substitution (``isolated=False``).
Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping
from typing import Any

from piceli.envs.model import ISOLATION_NAME, MANAGED_BY, BranchEnv, EnvError
from piceli.k8s.ops.discovery import ResourceScope
from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    ResourceIntent,
    ResourceRef,
)
from piceli.pipeline.compose import HOSTNAME_LABEL, containers, mirrored, pod_specs
from piceli.pipeline.model import handle_image

#: The component holding the isolation objects of a branch environment.
COMPONENT = "piceli-env"
#: Namespaces a branch may name absolutely (the API server, cluster DNS).
SHARED_NAMESPACES = frozenset({"default", "kube-system"})
_UNRENAMEABLE = frozenset({"CustomResourceDefinition", "Namespace"})
_SERVICE_NAME = re.compile(
    r"(?<![-a-z0-9.])[a-z0-9](?:[-a-z0-9]*[a-z0-9])?\."
    r"([a-z0-9](?:[-a-z0-9]*[a-z0-9])?)\.svc(?:\.cluster\.local)?\b"
)
_METADATA_NAME = "kubernetes.io/metadata.name"


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _where(ref: ResourceRef) -> str:
    return f"{ref.kind}/{ref.name}"


def qualified_name(name: str, namespace: str) -> str:
    """``<name>-<namespace>`` (at most 253 characters, hash suffix when cut)."""
    value = f"{name}-{namespace}"
    if len(value) <= 253:
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()[:8]
    return value[:244].rstrip("-.") + "-" + digest


# ------------------------------------------------------------------ images


def _images(manifest: dict[str, Any], env: BranchEnv, ref: ResourceRef) -> bool:
    """Prebuilt and mirrored images in place of handles; whether any changed."""
    changed = False
    for pod in pod_specs(manifest):
        pin = False
        for container in containers(pod):
            image = container.get("image")
            name = handle_image(image)
            if name is None:
                copy = mirrored(str(image or ""), env.mirrors)
                if copy is not None:
                    container["image"] = copy
                    changed = pin = True
                continue
            if name not in env.images:
                raise EnvError(
                    "env-image-missing",
                    f"{_where(ref)} uses build image {name!r}, and no digest was "
                    "given for it: pass --digest NAME=REF or a build receipt",
                )
            container["image"] = env.images[name]
            changed = pin = True
        if (
            pin
            and env.pin_node is not None
            and not pod.get("nodeSelector")
            and not pod.get("nodeName")
            and not pod.get("affinity")
        ):
            pod["nodeSelector"] = {HOSTNAME_LABEL: env.pin_node}
    return changed


# --------------------------------------------------------------- refusals


def _refuse(code: str, message: str) -> EnvError:
    return EnvError(code, message + " (not allowed in a branch environment)")


def _check_names(manifest: Mapping[str, Any], env: BranchEnv, ref: ResourceRef) -> None:
    if ref.kind == "Secret":
        return
    for text in _strings(manifest):
        for match in _SERVICE_NAME.finditer(text):
            namespace = match.group(1)
            if namespace != env.namespace and namespace not in SHARED_NAMESPACES:
                raise _refuse(
                    "env-isolation-absolute-service",
                    f"{_where(ref)} names a Service in namespace {namespace} "
                    "absolutely; name Services by their relative name",
                )


def _check_node_access(manifest: Mapping[str, Any], ref: ResourceRef) -> None:
    spec = manifest.get("spec") or {}
    if ref.kind == "Service":
        kind = spec.get("type") or "ClusterIP"
        ports = spec.get("ports") or ()
        if kind in {"NodePort", "LoadBalancer"} or any(
            isinstance(port, Mapping) and port.get("nodePort") for port in ports
        ):
            raise _refuse(
                "env-isolation-node-port",
                f"{_where(ref)} is a {kind} Service that opens a node port",
            )
    for pod in pod_specs(manifest):
        if pod.get("hostNetwork") or pod.get("hostPID") or pod.get("hostIPC"):
            raise _refuse(
                "env-isolation-host-port",
                f"{_where(ref)} uses the node's network or process namespaces",
            )
        for container in containers(pod):
            for port in container.get("ports") or ():
                if isinstance(port, Mapping) and port.get("hostPort"):
                    raise _refuse(
                        "env-isolation-host-port",
                        f"{_where(ref)} container {container.get('name')!r} "
                        "binds a host port",
                    )
        for volume in pod.get("volumes") or ():
            if isinstance(volume, Mapping) and "hostPath" in volume:
                raise _refuse(
                    "env-isolation-host-path",
                    f"{_where(ref)} mounts node directory volume "
                    f"{volume.get('name')!r}",
                )


def _selects_other(selector: Any, env: BranchEnv) -> bool:
    """Whether a namespaceSelector may select a namespace other than the env's."""
    if not isinstance(selector, Mapping):
        return False
    labels = selector.get("matchLabels") or {}
    expressions = selector.get("matchExpressions") or ()
    if not labels and not expressions:
        return True  # every namespace
    name = labels.get(_METADATA_NAME) if isinstance(labels, Mapping) else None
    if name is None:
        return False  # selects by another label (an ingress controller's)
    return bool(name == env.main_namespace or name.startswith(env.config.prefix))


def _check_cross_namespace(
    manifest: Mapping[str, Any], env: BranchEnv, ref: ResourceRef
) -> None:
    if ref.kind in {"RoleBinding", "ClusterRoleBinding"}:
        for subject in manifest.get("subjects") or ():
            namespace = (
                subject.get("namespace") if isinstance(subject, Mapping) else None
            )
            if namespace and namespace != env.namespace:
                raise _refuse(
                    "env-isolation-cross-namespace",
                    f"{_where(ref)} binds a subject of namespace {namespace}",
                )
    if ref.kind == "NetworkPolicy":
        spec = manifest.get("spec") or {}
        for direction, key in (("ingress", "from"), ("egress", "to")):
            for rule in spec.get(direction) or ():
                for peer in (rule or {}).get(key) or ():
                    if isinstance(peer, Mapping) and _selects_other(
                        peer.get("namespaceSelector"), env
                    ):
                        raise _refuse(
                            "env-isolation-cross-namespace",
                            f"{_where(ref)} allows {direction} from or to other "
                            "branch namespaces",
                        )


# ------------------------------------------------------------ claim sizes


def _set_size(claim_spec: dict[str, Any], size: str) -> None:
    resources = claim_spec.setdefault("resources", {})
    resources.setdefault("requests", {})["storage"] = size


def _claim_sizes(manifests: dict[ResourceRef, dict[str, Any]], env: BranchEnv) -> None:
    sizes = dict(env.config.claim_sizes)
    if not sizes:
        return
    used: set[str] = set()
    workload_claims: dict[str, str] = {}  # claim name -> the workload using it
    for ref, manifest in manifests.items():
        if ref.kind not in {"Deployment", "StatefulSet", "Job", "CronJob"}:
            continue
        for pod in pod_specs(manifest):
            for volume in pod.get("volumes") or ():
                claim = (volume or {}).get("persistentVolumeClaim") or {}
                if claim.get("claimName"):
                    workload_claims.setdefault(str(claim["claimName"]), ref.name)
        if ref.kind == "StatefulSet":
            for template in (manifest.get("spec") or {}).get(
                "volumeClaimTemplates"
            ) or ():
                name = str((template.get("metadata") or {}).get("name"))
                size = sizes.get(f"{ref.name}/{name}", sizes.get(ref.name))
                if size is not None:
                    _set_size(template.setdefault("spec", {}), size)
                    used.update({f"{ref.name}/{name}", ref.name} & sizes.keys())
    for ref, manifest in manifests.items():
        if ref.kind != "PersistentVolumeClaim":
            continue
        owner = workload_claims.get(ref.name)
        keys = [ref.name, *([f"{owner}/{ref.name}", owner] if owner else [])]
        key = next((item for item in keys if item in sizes), None)
        if key is not None:
            _set_size(manifest.setdefault("spec", {}), sizes[key])
            used.add(key)
    unknown = sorted(set(sizes) - used)
    if unknown:
        raise EnvError(
            "env-config-invalid",
            f"claim_sizes names {unknown[0]!r}, which is no claim, workload or "
            "workload/claim template of the app",
        )


# ---------------------------------------------------------- cluster scope


def _renames(
    manifests: Mapping[ResourceRef, dict[str, Any]], env: BranchEnv
) -> dict[tuple[str, str], str]:
    renames: dict[tuple[str, str], str] = {}
    for ref in manifests:
        if ref.namespace:
            continue
        if ref.kind in _UNRENAMEABLE:
            raise _refuse(
                "env-isolation-cluster-scoped",
                f"{_where(ref)} is shared by every namespace and cannot be renamed",
            )
        renames[(ref.kind, ref.name)] = qualified_name(ref.name, env.namespace)
    return renames


def _claim_volume(spec: dict[str, Any], renames: Mapping[tuple[str, str], str]) -> None:
    storage_class = spec.get("storageClassName")
    if storage_class and ("StorageClass", storage_class) in renames:
        spec["storageClassName"] = renames[("StorageClass", storage_class)]


def _rewrite(
    ref: ResourceRef,
    manifest: dict[str, Any],
    renames: Mapping[tuple[str, str], str],
    env: BranchEnv,
) -> None:
    if (ref.kind, ref.name) in renames:
        manifest["metadata"]["name"] = renames[(ref.kind, ref.name)]
    role = manifest.get("roleRef")
    if isinstance(role, dict) and (role.get("kind"), role.get("name")) in renames:
        role["name"] = renames[(role["kind"], role["name"])]
    spec = manifest.get("spec")
    if not isinstance(spec, dict):
        return
    if ref.kind == "PersistentVolumeClaim":
        _claim_volume(spec, renames)
        volume = spec.get("volumeName")
        if volume:
            if ("PersistentVolume", volume) not in renames:
                raise _refuse(
                    "env-isolation-shared-volume",
                    f"{_where(ref)} binds volume {volume}, which the app does not "
                    "declare (another environment's data)",
                )
            spec["volumeName"] = renames[("PersistentVolume", volume)]
    if ref.kind == "PersistentVolume":
        _claim_volume(spec, renames)
        claim = spec.get("claimRef")
        if isinstance(claim, dict) and claim.get("namespace"):
            claim["namespace"] = env.namespace
    if ref.kind == "StatefulSet":
        for template in spec.get("volumeClaimTemplates") or ():
            if isinstance(template, dict) and isinstance(template.get("spec"), dict):
                _claim_volume(template["spec"], renames)
                if template["spec"].get("volumeName"):
                    raise _refuse(
                        "env-isolation-shared-volume",
                        f"{_where(ref)} binds its claims to a named volume",
                    )


# ------------------------------------------------------------ added objects


def isolation_objects(env: BranchEnv) -> tuple[dict[str, Any], dict[str, Any]]:
    """The default-deny NetworkPolicy and the ResourceQuota of a branch env."""
    metadata = {
        "name": ISOLATION_NAME,
        "namespace": env.namespace,
        "labels": dict(MANAGED_BY),
    }
    egress: list[dict[str, Any]] = [
        {"to": [{"podSelector": {}}]},
        {
            "to": [
                {"namespaceSelector": {"matchLabels": {_METADATA_NAME: "kube-system"}}}
            ],
            "ports": [
                {"protocol": "UDP", "port": 53},
                {"protocol": "TCP", "port": 53},
            ],
        },
    ]
    for cidr in env.config.allow_egress:
        egress.append({"to": [{"ipBlock": {"cidr": cidr}}]})
    policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": dict(metadata),
        "spec": {
            "podSelector": {},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [{"from": [{"podSelector": {}}]}],
            "egress": egress,
        },
    }
    quota = {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": dict(metadata),
        "spec": {"hard": dict(env.config.quota or {})},
    }
    return policy, quota


# ------------------------------------------------------------------- entry


def isolate(
    composition: DeploymentComposition, env: BranchEnv
) -> DeploymentComposition:
    """The composition of one environment (see the module docstring).

    :raises EnvError: an ``env-isolation-*`` code, ``env-image-missing`` or
        ``env-config-invalid`` (a claim size naming nothing).
    """
    manifests: dict[ResourceRef, dict[str, Any]] = {}
    for component in composition.components:
        for resource in component.resources:
            manifests[resource.ref] = resource.manifest
    changed: set[ResourceRef] = set()
    for ref, manifest in manifests.items():
        if _images(manifest, env, ref):
            changed.add(ref)
    renames: dict[tuple[str, str], str] = {}
    if env.isolated:
        for ref, manifest in manifests.items():
            _check_names(manifest, env, ref)
            _check_node_access(manifest, ref)
            _check_cross_namespace(manifest, env, ref)
        before = {ref: repr(manifest) for ref, manifest in manifests.items()}
        _claim_sizes(manifests, env)
        renames = _renames(manifests, env)
        for ref, manifest in manifests.items():
            _rewrite(ref, manifest, renames, env)
        changed.update(
            ref for ref, manifest in manifests.items() if repr(manifest) != before[ref]
        )

    def moved(ref: ResourceRef) -> ResourceRef:
        name = renames.get((ref.kind, ref.name))
        if name is None or ref.namespace:
            return ref
        return ResourceRef(ref.api_version, ref.kind, ref.namespace, name)

    components = []
    for component in composition.components:
        resources = []
        for resource in component.resources:
            dependencies = tuple(moved(item) for item in resource.dependencies)
            if resource.ref not in changed and dependencies == resource.dependencies:
                resources.append(resource)
                continue
            scope = None if resource.ref.namespace else ResourceScope.CLUSTER
            rebuilt = ResourceIntent.from_manifest(
                manifests[resource.ref], dependencies, scope=scope
            )
            for binding in resource.secret_bindings:
                rebuilt = rebuilt.with_secret(binding.json_pointer, binding.reference)
            resources.append(rebuilt)
        components.append(
            DeploymentComponent(
                component.name, tuple(resources), component.dependencies
            )
        )
    if env.isolated:
        if any(component.name == COMPONENT for component in components):
            raise EnvError(
                "env-config-invalid",
                f"the app declares a component named {COMPONENT!r}, which branch "
                "environments use for their isolation objects",
            )
        components.append(
            DeploymentComponent(
                COMPONENT,
                tuple(
                    ResourceIntent.from_manifest(item)
                    for item in isolation_objects(env)
                ),
            )
        )
    return DeploymentComposition(tuple(components))
