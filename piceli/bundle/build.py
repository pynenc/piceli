"""Assemble a client bundle from a render (see :mod:`piceli.bundle`).

:func:`build_bundle` is pure (it never writes): it turns the components of a
render into the bundle's objects, overlay, ``prepare.sh``, guides and
manifest, after the safety gate. :func:`write_bundle` writes them into an
empty directory, exports the image archives and writes ``SHA256SUMS`` last.

Importing this module is side-effect free.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from piceli.bundle import guides
from piceli.bundle.prepare import (
    PrepareError,
    SecretPlan,
    client_inputs,
    script,
    secret_plans,
)
from piceli.bundle.safety import SafetyException, Violation, check, exceptions

BUNDLE_SCHEMA = "piceli.client-bundle.v1"
PART_OF = "app.kubernetes.io/part-of"
BUNDLE_ANNOTATION = "piceli.io/bundle"
#: The host of image references in the base: never resolvable (RFC 2606), so
#: the base alone can never pull an image; the overlay names the registry.
IMAGE_HOST = "images.piceli.invalid"
REGISTRY_PLACEHOLDER = "@REGISTRY@"
OVERLAY = "dev"
NAME = re.compile(r"^[a-z]([-a-z0-9]{0,51}[a-z0-9])?$")
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")
_PENDING = re.compile(
    r"^(?:pending-build|pending-delivery|pipeline)\.piceli\.invalid/([a-z0-9][a-z0-9_-]{0,62})"
    r"(?:@sha256:0{64}|:unresolved)$"
)
_DIGEST = re.compile(r"@sha256:[a-f0-9]{64}$")
_SENTINELS = ("<redacted>", "<private>")
_WORKLOADS = {
    ("apps/v1", "Deployment"),
    ("apps/v1", "StatefulSet"),
    ("apps/v1", "DaemonSet"),
    ("batch/v1", "Job"),
    ("batch/v1", "CronJob"),
}


class BundleError(ValueError):
    """The render cannot become a bundle. ``code`` is a registered error code."""

    def __init__(
        self, code: str, message: str, violations: Sequence[Violation] = ()
    ) -> None:
        super().__init__(message)
        self.code = code
        self.violations = tuple(violations)


@dataclass(frozen=True)
class BundleObject:
    """One object of the base; ``cluster`` when it is cluster-scoped."""

    file: str
    manifest: Mapping[str, Any]
    cluster: bool = False

    @property
    def kind(self) -> str:
        return str(self.manifest.get("kind"))

    @property
    def name(self) -> str:
        return str((self.manifest.get("metadata") or {}).get("name"))


@dataclass(frozen=True)
class ImageUse:
    """One image the bundle's workloads use."""

    name: str
    source: str  # "receipt" or "external"
    reference: str  # what the base names

    def public(self) -> dict[str, str]:
        return {"name": self.name, "source": self.source, "reference": self.reference}


@dataclass(frozen=True)
class Forward:
    """The port-forward INSTALL.md shows (a Service and its port)."""

    service: str
    local: int
    remote: int


@dataclass
class Bundle:
    """Everything of a bundle but the image archives (see :func:`write_bundle`)."""

    name: str
    version: str
    namespace: str
    environment: str | None
    objects: list[BundleObject]
    secrets: list[SecretPlan]
    inputs: list[dict[str, str]]
    images: list[ImageUse]
    digests: dict[str, str]
    exceptions: list[SafetyException]
    egress: list[dict[str, Any]]
    forward: Forward | None
    storage_class: str | None
    generators: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def workloads(self) -> list[BundleObject]:
        return [
            item
            for item in self.objects
            if (item.manifest.get("apiVersion"), item.kind) in _WORKLOADS
        ]

    @property
    def cluster_kinds(self) -> list[str]:
        """Kinds of the cluster-scoped objects (UNINSTALL.md deletes them by label)."""
        return sorted({item.kind.lower() for item in self.objects if item.cluster})


# ------------------------------------------------------------------ helpers


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _pods(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    from piceli.pipeline.compose import pod_specs

    return pod_specs(manifest)


def _templates(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The pod templates (``metadata`` + ``spec``) of a workload."""
    spec = manifest.get("spec")
    if not isinstance(spec, dict):
        return []
    if manifest.get("kind") == "CronJob":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
    template = spec.get("template")
    return [template] if isinstance(template, dict) else []


def _yaml(document: Any) -> bytes:
    import yaml

    return yaml.safe_dump(document, sort_keys=True, default_flow_style=False).encode()


def _yaml_documents(documents: Sequence[Any]) -> bytes:
    return b"---\n".join(_yaml(item) for item in documents)


def part_of(components: Sequence[Mapping[str, Any]]) -> str | None:
    """The single ``app.kubernetes.io/part-of`` label of a render, if there is one."""
    from piceli.chart import chart_name

    return chart_name(components)


# --------------------------------------------------------------- the build


def _objects(
    components: Sequence[Mapping[str, Any]], name: str
) -> list[tuple[str, dict[str, Any]]]:
    from piceli.gitops import file_name

    result = []
    for component in components:
        for resource in component["resources"]:
            manifest = copy.deepcopy(dict(resource["manifest"]))
            kind = manifest.get("kind")
            if (manifest.get("apiVersion"), kind) == ("v1", "Secret"):
                continue
            label = f"{kind}/{(manifest.get('metadata') or {}).get('name')}"
            if kind == "Namespace":
                raise BundleError(
                    "bundle-invalid",
                    f"{label}: a bundle installs into a namespace the client "
                    "creates; it cannot declare one",
                )
            if resource.get("secret_bindings") or any(
                value in _SENTINELS for value in _strings(manifest)
            ):
                raise BundleError(
                    "bundle-secret-value",
                    f"{label} holds a value Piceli redacts or injects at apply "
                    "time; a bundle never carries secret values. Move it to a "
                    "Secret bound to a generator",
                )
            metadata = manifest.setdefault("metadata", {})
            labels = metadata.setdefault("labels", {})
            found = labels.get(PART_OF)
            if found is not None and found != name:
                raise BundleError(
                    "bundle-invalid",
                    f"{label} is labelled {PART_OF}={found}, not {name}; "
                    "one bundle is one app (pass --name or align the labels)",
                )
            labels[PART_OF] = name
            for template in _templates(manifest):
                template_labels = template.setdefault("metadata", {}).setdefault(
                    "labels", {}
                )
                if template_labels.get(PART_OF, name) != name:
                    raise BundleError(
                        "bundle-invalid", f"{label}: its pods carry another {PART_OF}"
                    )
                template_labels[PART_OF] = name
            result.append((file_name(str(component["name"]), manifest), manifest))
    return result


def _images(
    objects: Sequence[tuple[str, dict[str, Any]]],
    receipt_names: Sequence[str],
) -> list[ImageUse]:
    """Rewrite build images to ``images.piceli.invalid/<name>``; list every image."""
    from piceli.pipeline.compose import containers

    found: dict[str, ImageUse] = {}
    for _, manifest in objects:
        for pod in _pods(manifest):
            for container in containers(pod):
                image = container.get("image")
                if not isinstance(image, str):
                    continue
                pending = _PENDING.match(image)
                label = f"{manifest.get('kind')}/{manifest['metadata'].get('name')}"
                if pending:
                    name = pending.group(1)
                    if name not in receipt_names:
                        raise BundleError(
                            "bundle-image-unresolved",
                            f"{label} uses the build image {name!r}, which the "
                            "build receipt does not hold; pass --receipt with it",
                        )
                    container["image"] = f"{IMAGE_HOST}/{name}"
                    found[name] = ImageUse(name, "receipt", container["image"])
                elif not _DIGEST.search(image):
                    raise BundleError(
                        "bundle-image-unpinned",
                        f"{label} uses an image without a digest; a bundle pins "
                        "every image (name it repository@sha256:...)",
                    )
                else:
                    found.setdefault(image, ImageUse(image, "external", image))
    return sorted(found.values(), key=lambda item: (item.source, item.name))


def default_deny(name: str) -> dict[str, Any]:
    """Ingress only from the namespace; egress only to it and to cluster DNS."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": f"{name}-default-deny", "labels": {PART_OF: name}},
        "spec": {
            "podSelector": {},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [{"from": [{"podSelector": {}}]}],
            "egress": [
                {"to": [{"podSelector": {}}]},
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "kube-system"
                                }
                            },
                            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                        }
                    ],
                    "ports": [
                        {"protocol": "UDP", "port": 53},
                        {"protocol": "TCP", "port": 53},
                    ],
                },
            ],
        },
    }


def _declared_egress(
    objects: Sequence[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    result = []
    for _, manifest in objects:
        if manifest.get("kind") != "NetworkPolicy":
            continue
        spec = manifest.get("spec") or {}
        if "Egress" not in (spec.get("policyTypes") or ()):
            continue
        for rule in spec.get("egress") or ():
            result.append(
                {
                    "policy": manifest["metadata"].get("name"),
                    "to": rule.get("to") or "any destination",
                    "ports": rule.get("ports") or "any port",
                }
            )
    return result


def build_bundle(
    components: Sequence[Mapping[str, Any]],
    *,
    name: str,
    version: str,
    namespace: str,
    generators: Mapping[str, Any],
    receipt_names: Sequence[str] = (),
    environment: str | None = None,
    storage_class: str | None = None,
    forward: Forward | None = None,
) -> Bundle:
    """The bundle of a render (no image archives yet; see :func:`write_bundle`).

    :raises BundleError: ``bundle-invalid``, ``bundle-secret-value``,
        ``bundle-secret-unsupported``, ``bundle-image-unresolved``,
        ``bundle-image-unpinned``, ``bundle-empty`` or one
        ``bundle-unsafe-<rule>`` (with every violation).
    """
    if not NAME.fullmatch(name or ""):
        raise BundleError(
            "bundle-invalid",
            "the bundle name must be lowercase letters, digits and '-', start "
            "with a letter, at most 53 characters",
        )
    if not _SEMVER.fullmatch(version or ""):
        raise BundleError("bundle-invalid", "the bundle version must be X.Y.Z[-pre]")
    try:
        plans = secret_plans(components)
        inputs = client_inputs(plans, generators)
    except PrepareError as error:
        raise BundleError(error.code, str(error)) from None
    objects = _objects(components, name)
    if not objects:
        raise BundleError("bundle-empty", "the render has no object for a bundle")
    images = _images(objects, receipt_names)
    egress = _declared_egress(objects)
    objects.append((f"{name}-default-deny.yaml", default_deny(name)))
    manifests = [manifest for _, manifest in objects]
    violations = check(manifests)
    if violations:
        from piceli.bundle.safety import first_code

        raise BundleError(
            first_code(violations),
            f"{len(violations)} safety violation(s); first: {violations[0].object} "
            f"breaks {violations[0].rule} ({violations[0].detail})",
            violations,
        )
    waived = exceptions(manifests)
    final: list[BundleObject] = []
    for file, manifest in objects:
        metadata = manifest["metadata"]
        cluster = metadata.pop("namespace", None) is None and not (
            manifest.get("kind") == "NetworkPolicy"
            and metadata.get("name") == f"{name}-default-deny"
        )
        metadata.setdefault("annotations", {})[BUNDLE_ANNOTATION] = f"{name}@{version}"
        final.append(BundleObject(file, manifest, cluster))
    return Bundle(
        name=name,
        version=version,
        namespace=namespace,
        environment=environment,
        objects=final,
        secrets=plans,
        inputs=inputs,
        images=images,
        digests={},
        exceptions=waived,
        egress=egress,
        forward=forward,
        storage_class=storage_class,
        generators=dict(generators),
    )


# ------------------------------------------------------------------ files


def _overlay(bundle: Bundle) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    transformers = ["namespace.yaml"]
    files["namespace.yaml"] = _yaml(
        {
            "apiVersion": "builtin",
            "kind": "NamespaceTransformer",
            "metadata": {"name": "namespace", "namespace": bundle.namespace},
            "setRoleBindingSubjects": "allServiceAccounts",
            "unsetOnly": False,
        }
    )
    built = [item for item in bundle.images if item.source == "receipt"]
    if built:
        transformers.append("images.yaml")
        files["images.yaml"] = (
            b"# The images of this bundle. After copying images/<name>.oci.tar into\n"
            b"# your registry, put its path in place of @REGISTRY@ (INSTALL.md does\n"
            b"# it with sed). Each digest is the one in the archive; a copy with\n"
            b"# --preserve-digests keeps it. If your tool changed it, set the new one.\n"
            + _yaml_documents(
                [
                    {
                        "apiVersion": "builtin",
                        "kind": "ImageTagTransformer",
                        "metadata": {"name": item.name},
                        "imageTag": {
                            "name": item.reference,
                            "newName": f"{REGISTRY_PLACEHOLDER}/{item.name}",
                            "digest": bundle.digests.get(
                                item.name, "sha256:" + "0" * 64
                            ),
                        },
                    }
                    for item in built
                ]
            )
        )
    patches: list[str] = []
    resources = []
    for item in bundle.workloads:
        for template in _templates(dict(item.manifest)):
            spec = template.get("spec") or {}
            containers = [
                {
                    "name": container["name"],
                    "resources": container.get("resources") or {},
                }
                for key in ("initContainers", "containers")
                for container in spec.get(key) or ()
            ]
            patch_spec: dict[str, Any] = {"template": {"spec": {}}}
            for key in ("initContainers", "containers"):
                if spec.get(key):
                    patch_spec["template"]["spec"][key] = [
                        entry
                        for entry in containers
                        if entry["name"] in {c["name"] for c in spec[key]}
                    ]
            if item.kind == "CronJob":
                patch_spec = {"jobTemplate": {"spec": patch_spec}}
            resources.append(
                {
                    "apiVersion": item.manifest["apiVersion"],
                    "kind": item.kind,
                    "metadata": {"name": item.name},
                    "spec": patch_spec,
                }
            )
    if resources:
        patches.append("resources.yaml")
        files["resources.yaml"] = (
            b"# Requests and limits of every container; change them here.\n"
            + _yaml_documents(resources)
        )
    storage = []
    for item in bundle.objects:
        manifest = item.manifest
        if (manifest.get("apiVersion"), item.kind) == ("v1", "PersistentVolumeClaim"):
            spec = copy.deepcopy(manifest.get("spec") or {})
            if bundle.storage_class:
                spec["storageClassName"] = bundle.storage_class
            storage.append(
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {"name": item.name},
                    "spec": {
                        key: spec[key]
                        for key in ("storageClassName", "resources")
                        if key in spec
                    },
                }
            )
        elif item.kind == "StatefulSet" and (manifest.get("spec") or {}).get(
            "volumeClaimTemplates"
        ):
            templates = copy.deepcopy(manifest["spec"]["volumeClaimTemplates"])
            if bundle.storage_class:
                for template in templates:
                    template.setdefault("spec", {})["storageClassName"] = (
                        bundle.storage_class
                    )
            storage.append(
                {
                    "apiVersion": "apps/v1",
                    "kind": "StatefulSet",
                    "metadata": {"name": item.name},
                    "spec": {"volumeClaimTemplates": templates},
                }
            )
    if storage:
        patches.append("storage.yaml")
        files["storage.yaml"] = (
            b"# Storage of every claim: set storageClassName (and the size) here.\n"
            b"# Without storageClassName the cluster's default class is used.\n"
            + _yaml_documents(storage)
        )
    kustomization: dict[str, Any] = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": ["../../base"],
        "transformers": transformers,
    }
    if patches:
        kustomization["patches"] = [{"path": item} for item in patches]
    files["kustomization.yaml"] = _yaml(kustomization)
    return {f"overlays/{OVERLAY}/{key}": value for key, value in files.items()}


def render_files(bundle: Bundle) -> dict[str, bytes]:
    """Every text file of the bundle (all but images and ``SHA256SUMS``)."""
    files: dict[str, bytes] = {}
    for item in bundle.objects:
        files[f"base/{item.file}"] = _yaml(item.manifest)
    files["base/kustomization.yaml"] = _yaml(
        {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "resources": sorted(item.file for item in bundle.objects),
        }
    )
    files.update(_overlay(bundle))
    files["prepare.sh"] = script(
        bundle.name, bundle.secrets, bundle.generators
    ).encode()
    files["INSTALL.md"] = guides.install(bundle).encode()
    files["UNINSTALL.md"] = guides.uninstall(bundle).encode()
    return files


def manifest(bundle: Bundle, images: Sequence[Any] = ()) -> dict[str, Any]:
    """``bundle.json``: what the bundle holds (never a value)."""
    return {
        "schema": BUNDLE_SCHEMA,
        "name": bundle.name,
        "version": bundle.version,
        "environment": bundle.environment,
        "namespace": bundle.namespace,
        "overlay": OVERLAY,
        "objects": [
            {
                "kind": item.kind,
                "name": item.name,
                "file": f"base/{item.file}",
                "scope": "cluster" if item.cluster else "namespace",
            }
            for item in bundle.objects
        ],
        "secrets": [item.public() for item in bundle.secrets],
        "client_inputs": bundle.inputs,
        "images": [
            {**item.public(), "digest": bundle.digests.get(item.name)}
            for item in bundle.images
        ],
        "archives": [item.public() for item in images],
        "egress": bundle.egress,
        "safety_exceptions": [item.public() for item in bundle.exceptions],
    }


def sha256sums(files: Mapping[str, Path]) -> bytes:
    """``SHA256SUMS`` in ``sha256sum -c`` format, sorted by path."""
    lines = []
    for name in sorted(files):
        digest = hashlib.sha256()
        with open(files[name], "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        lines.append(f"{digest.hexdigest()}  {name}\n")
    return "".join(lines).encode()


def write_bundle(
    directory: Path,
    bundle: Bundle,
    images: Sequence[tuple[str, Sequence[Any]]] = (),
) -> dict[str, Any]:
    """Write ``bundle`` into ``directory`` (absent or empty); its summary.

    ``images`` are ``(name, platform images)`` to export as OCI archives
    (:func:`piceli.bundle.images.export_image`). ``SHA256SUMS`` is written
    last, over every other file. On any error, everything written is removed.

    :raises BundleError: ``bundle-out-refused``, or an image export code.
    """
    import shutil

    from piceli.bundle.images import ImageExportError, export_image

    if directory.is_symlink() or (
        directory.exists() and (not directory.is_dir() or any(directory.iterdir()))
    ):
        raise BundleError(
            "bundle-out-refused", f"{directory} must be absent or an empty directory"
        )
    created = not directory.exists()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        exported = [
            export_image(name, platforms, directory, tag=bundle.version)
            for name, platforms in images
        ]
        bundle.digests = {item.name: item.digest for item in exported}
        files = render_files(bundle)
        files["bundle.json"] = (
            json.dumps(manifest(bundle, exported), indent=2, sort_keys=True) + "\n"
        ).encode()
        for name, body in files.items():
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        (directory / "prepare.sh").chmod(0o755)
        written = {
            path.relative_to(directory).as_posix(): path
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }
        (directory / "SHA256SUMS").write_bytes(sha256sums(written))
    except ImageExportError as error:
        _remove(directory, created, shutil)
        raise BundleError(error.code, str(error)) from None
    except OSError as error:
        _remove(directory, created, shutil)
        raise BundleError(
            "bundle-out-refused", f"cannot write the bundle: {type(error).__name__}"
        ) from None
    except BaseException:
        _remove(directory, created, shutil)
        raise
    return {
        "directory": str(directory),
        "files": sorted([*written, "SHA256SUMS"]),
        "images": [item.public() for item in exported],
    }


def _remove(directory: Path, created: bool, shutil: Any) -> None:
    if created:
        shutil.rmtree(directory, ignore_errors=True)
        return
    for child in directory.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
