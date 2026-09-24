"""Where imported manifests come from: a live namespace or a directory of files.

Both readers return scrubbed manifests (see :func:`~piceli.importing.clean.scrub`):
Secret values are replaced with ``<private>`` as soon as they are read and
never leave this module.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from piceli.importing.clean import scrub
from piceli.importing.generate import ImportFailure, Skipped

#: Kinds ``piceli import live`` reads, with their API versions.
LIVE_KINDS: tuple[tuple[str, str], ...] = (
    ("v1", "ConfigMap"),
    ("v1", "Secret"),
    ("v1", "Service"),
    ("v1", "PersistentVolumeClaim"),
    ("apps/v1", "Deployment"),
    ("networking.k8s.io/v1", "NetworkPolicy"),
)
_CLUSTER_SCOPED = frozenset(
    {
        "APIService",
        "ClusterRole",
        "ClusterRoleBinding",
        "CustomResourceDefinition",
        "IngressClass",
        "MutatingWebhookConfiguration",
        "Namespace",
        "Node",
        "PersistentVolume",
        "PriorityClass",
        "RuntimeClass",
        "StorageClass",
        "ValidatingWebhookConfiguration",
    }
)
_SELECT = re.compile(
    r"(?P<kind>[A-Z][A-Za-z0-9]*)/(?P<name>[a-z0-9]([-a-z0-9.]*[a-z0-9])?)"
    r"|(?P<key>([a-z0-9]([-a-z0-9.]*[a-z0-9])?/)?[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?)"
    r"=(?P<value>([A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?)?)"
)


@dataclass
class Source:
    """Scrubbed manifests plus what was skipped and the running images."""

    manifests: list[dict[str, Any]]
    namespace: str
    skipped: list[Skipped] = field(default_factory=list)
    running_images: dict[tuple[str, str], str] = field(default_factory=dict)


# ------------------------------------------------------------------ select


@dataclass(frozen=True)
class Selector:
    """``Kind/name`` or ``label=value``."""

    kind: str | None = None
    name: str | None = None
    label: str | None = None
    value: str | None = None

    @classmethod
    def parse(cls, text: str) -> Selector:
        match = _SELECT.fullmatch(text or "")
        if match is None:
            raise ImportFailure(
                "import-select-invalid",
                f"--select must be Kind/name or label=value, got {text!r}",
            )
        if match["kind"]:
            return cls(kind=match["kind"], name=match["name"])
        return cls(label=match["key"], value=match["value"])

    def matches(self, manifest: Mapping[str, Any]) -> bool:
        metadata = manifest.get("metadata") or {}
        if self.kind is not None:
            return (
                manifest.get("kind") == self.kind and metadata.get("name") == self.name
            )
        return (metadata.get("labels") or {}).get(self.label) == self.value


def select(source: Source, selectors: Sequence[str]) -> Source:
    """Keep only objects matching any selector (all objects without selectors)."""
    parsed = [Selector.parse(text) for text in selectors]
    if parsed:
        source.manifests = [
            item for item in source.manifests if any(s.matches(item) for s in parsed)
        ]
        kept = {
            item["metadata"]["name"]
            for item in source.manifests
            if item.get("kind") == "Deployment"
        }
        source.running_images = {
            key: value for key, value in source.running_images.items() if key[0] in kept
        }
    if not source.manifests:
        raise ImportFailure(
            "import-nothing-selected",
            "no object to import"
            + (" matches --select" if parsed else " in the source"),
        )
    return source


def _skip_reason(manifest: Mapping[str, Any], namespace: str) -> str | None:
    metadata = manifest.get("metadata") or {}
    if metadata.get("ownerReferences"):
        return "owned by another object"
    kind, name = manifest.get("kind"), metadata.get("name")
    if kind == "ConfigMap" and name == "kube-root-ca.crt":
        return "created by Kubernetes in every namespace"
    if kind == "Service" and name == "kubernetes" and namespace == "default":
        return "the API server's own Service"
    return None


# -------------------------------------------------------------------- live


def read_live(
    kubeconfig: Path,
    context: str,
    namespace: str,
    *,
    transport: str = "https",
    request_seconds: float = 10.0,
    api_client: Any | None = None,
) -> Source:
    """Read the importable objects of one namespace through an explicit kubeconfig.

    Read-only: lists ConfigMaps, Secrets, Services, PersistentVolumeClaims,
    Deployments and NetworkPolicies, plus Pods for their running image
    digests. Never writes to the cluster.

    :raises ImportFailure: ``import-target-invalid`` or ``import-discovery-failed``.
    """
    from piceli.k8s.ops.discovery import (
        ApiResource,
        ResourceListRequest,
        ResourceType,
    )
    from piceli.k8s.ops.kubernetes_provider import ProviderError
    from piceli.k8s.ops.provider_factory import (
        KubeconfigTarget,
        ProviderFactoryError,
        build_provider,
    )

    try:
        binding = build_provider(
            KubeconfigTarget(
                Path(kubeconfig),
                context,
                namespace,
                transport=transport,  # type: ignore[arg-type]
                request_seconds=request_seconds,
            ),
            field_manager="piceli-import",
            owner_id="piceli-import",
            api_client=api_client,
        )
    except (ProviderFactoryError, ValueError) as error:
        raise ImportFailure("import-target-invalid", str(error)) from None
    provider = binding.provider

    def listed(api_version: str, kind: str, clean: bool = True) -> list[dict[str, Any]]:
        api = provider.discover_api_resource(
            binding.target, ResourceType(api_version, kind)
        )
        if not isinstance(api, ApiResource):
            raise ImportFailure(
                "import-discovery-failed", f"cannot list {kind}: {api.value}"
            )
        items: list[dict[str, Any]] = []
        continuation = None
        while True:
            page = provider.list_resources(
                ResourceListRequest(binding.target, api, 250, continuation)
            )
            if page.failure is not None:
                raise ImportFailure(
                    "import-discovery-failed",
                    f"cannot list {kind}: {page.failure.value}",
                )
            items += [
                scrub(resource.manifest, namespace) if clean else resource.manifest
                for resource in page.resources
            ]
            continuation = page.continuation
            if continuation is None:
                return items

    try:
        manifests: list[dict[str, Any]] = []
        for api_version, kind in LIVE_KINDS:
            manifests += listed(api_version, kind)
        try:
            # Pods only for their status (running digests); never imported.
            pods = listed("v1", "Pod", clean=False)
        except ImportFailure:
            pods = []  # running digests are a convenience, not required
    except ProviderError as error:
        raise ImportFailure(
            "import-discovery-failed", f"the cluster read failed: {error.category}"
        ) from None
    finally:
        binding.close()
    source = _filtered(manifests, namespace)
    source.running_images = running_images(source.manifests, pods)
    return source


def _filtered(manifests: Iterable[dict[str, Any]], namespace: str) -> Source:
    source = Source([], namespace)
    for manifest in manifests:
        reason = _skip_reason(manifest, namespace)
        if reason is None:
            source.manifests.append(manifest)
        else:
            source.skipped.append(
                Skipped(
                    str(manifest["kind"]), str(manifest["metadata"]["name"]), reason
                )
            )
    return source


def running_images(
    manifests: Iterable[Mapping[str, Any]], pods: Iterable[Mapping[str, Any]]
) -> dict[tuple[str, str], str]:
    """``{(deployment, container): "repo@sha256:…"}`` from the pods' status.

    A Deployment's pods are those whose labels contain its selector. When its
    pods run different digests for one container (a rollout in progress) the
    container is left out.
    """
    found: dict[tuple[str, str], set[str]] = {}
    for manifest in manifests:
        if manifest.get("kind") != "Deployment":
            continue
        selector = (manifest.get("spec", {}).get("selector") or {}).get("matchLabels")
        if not isinstance(selector, dict) or not selector:
            continue
        for pod in pods:
            labels = (pod.get("metadata") or {}).get("labels") or {}
            if any(labels.get(key) != value for key, value in selector.items()):
                continue
            for status in (pod.get("status") or {}).get("containerStatuses") or []:
                image_id = str(status.get("imageID") or "")
                reference = image_id.split("://", 1)[-1]
                if "@sha256:" in reference:
                    key = (str(manifest["metadata"]["name"]), str(status.get("name")))
                    found.setdefault(key, set()).add(reference)
    return {
        key: next(iter(values)) for key, values in found.items() if len(values) == 1
    }


# -------------------------------------------------------------------- yaml


def read_yaml(directory: Path, namespace: str | None = None) -> Source:
    """Read every ``*.yaml``, ``*.yml`` and ``*.json`` file below ``directory``.

    Documents are read in path order; ``kind: List`` documents are expanded.
    Objects must be in ``namespace`` (default: the one namespace the files
    name, else ``default``); cluster-scoped objects are skipped.

    :raises ImportFailure: ``import-manifests-invalid``.
    """
    import yaml

    root = Path(directory)
    if not root.is_dir():
        raise ImportFailure("import-manifests-invalid", "not a directory")
    documents: list[tuple[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in {".yaml", ".yml", ".json"}:
            continue
        where = str(path.relative_to(root))
        try:
            loaded = list(yaml.safe_load_all(path.read_text()))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            raise ImportFailure(
                "import-manifests-invalid", f"{where} is not readable YAML or JSON"
            ) from None
        for document in loaded:
            if document is None:
                continue
            if isinstance(document, dict) and document.get("kind") == "List":
                documents += [(where, item) for item in document.get("items") or []]
            else:
                documents.append((where, document))
    manifests: list[dict[str, Any]] = []
    skipped: list[Skipped] = []
    for where, document in documents:
        metadata = document.get("metadata") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or not isinstance(document.get("apiVersion"), str)
            or not isinstance(document.get("kind"), str)
            or not isinstance(metadata, dict)
            or not isinstance(metadata.get("name"), str)
        ):
            raise ImportFailure(
                "import-manifests-invalid",
                f"{where}: every document needs apiVersion, kind and metadata.name",
            )
        if document["kind"] in _CLUSTER_SCOPED:
            skipped.append(
                Skipped(document["kind"], metadata["name"], "cluster-scoped")
            )
            continue
        manifests.append(document)
    declared = sorted(
        {
            str(item["metadata"]["namespace"])
            for item in manifests
            if item["metadata"].get("namespace")
        }
    )
    if namespace is None:
        if len(declared) > 1:
            raise ImportFailure(
                "import-manifests-invalid",
                f"the files name several namespaces {declared}; pass --namespace",
            )
        namespace = declared[0] if declared else "default"
    outside = sorted(
        f"{item['kind']}/{item['metadata']['name']}"
        for item in manifests
        if item["metadata"].get("namespace") not in (None, namespace)
    )
    if outside:
        raise ImportFailure(
            "import-manifests-invalid",
            f"objects outside namespace {namespace!r}: {outside}",
        )
    keys = [(item["kind"], item["metadata"]["name"]) for item in manifests]
    duplicates = sorted({f"{k}/{n}" for k, n in keys if keys.count((k, n)) > 1})
    if duplicates:
        raise ImportFailure(
            "import-manifests-invalid", f"objects declared twice: {duplicates}"
        )
    source = _filtered((scrub(item, namespace) for item in manifests), namespace)
    source.skipped = skipped + source.skipped
    return source
