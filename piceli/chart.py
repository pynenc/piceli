"""Render an app as a Helm chart, and as plain manifests with values.

``piceli chart`` turns the same render ``piceli render`` prints into:

- a Helm chart (``Chart.yaml``, ``values.yaml``, ``values.schema.json``,
  ``templates/``) whose templates, rendered with the default values, produce
  the objects Piceli renders (plus the ``helm.sh/chart`` and
  ``app.kubernetes.io/managed-by`` labels on each object's own metadata);
- plain manifests (no Helm, no Kustomize) with a values file applied by
  Piceli itself, validated against the same schema;
- a reproducible chart archive (``<name>-<version>.tgz``) and the OCI layout
  ``helm push`` produces, for ``oci://`` registries.

What a client sets (the values): each image's ``repository``, ``tag`` and
``digest``; ``imagePullSecrets``; each workload's ``replicas``, container
``resources`` and ``nodeSelector``; storage (``storageClassName`` and
``size`` of claim templates and claims); each Ingress's ``hosts`` and
``className`` and each HTTPRoute's ``hostnames``; every ConfigMap key
(``config``); and the name of each Secret the app reads (``secrets``).

Secrets: the chart never renders a Secret. Each Secret the app declares is
left out and referenced by name (``secrets.<declared name>.name``); the client
creates it (``kubectl create secret``, an ExternalSecret, SOPS) with the keys
the chart's README lists. Any other object that holds a redacted value or a
secret bound at apply time is refused (``chart-secret-value``).

Images: an image Piceli has not resolved (a pipeline build that has not run)
becomes a required value with no default, so ``helm install`` (and
``piceli chart manifests``) refuse to render until the client sets it.

Everything is deterministic: the same render, name and version give the same
files, the same archive bytes and the same OCI digest.

Importing this module opens no socket and reads no file.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from piceli.gitops import (
    OCI_MANIFEST,
    Blob,
    FluxArtifact,
    ManifestFile,
    file_name,
    gzip_bytes,
    tar_bytes,
)

HELM_CONFIG = "application/vnd.cncf.helm.config.v1+json"
HELM_CONTENT = "application/vnd.cncf.helm.chart.content.v1.tar+gzip"
#: Labels the chart adds to each object's own metadata (never to selectors
#: or pod templates).
HELM_LABELS = ("app.kubernetes.io/managed-by", "helm.sh/chart")
SCHEMA_DRAFT = "http://json-schema.org/draft-07/schema#"
MARKER = ".piceli-chart"
MARKER_SCHEMA = "piceli.chart-out.v1"

NAME = re.compile(r"^[a-z]([-a-z0-9]{0,51}[a-z0-9])?$")
# Semantic Versioning 2.0.0, as Helm requires for a chart version.
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
_APP_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,127}$")
_SENTINELS = ("<redacted>", "<private>")
_UNRESOLVED = re.compile(r"\.piceli\.invalid(?:/|$)|@sha256:0{64}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_KEY_UNSAFE = re.compile(r"[^a-z0-9-]+")
_SCALAR = re.compile(r"'?__piceli_site_(\d+)__'?")
_KEY = re.compile(r"^(?P<indent> *)'?~~piceli_site_(?P<site>\d+)'?: null$", re.M)

_QUANTITY = r"^[+]?[0-9.]+([eE][-+]?[0-9]+|[numkMGTPE]|[KMGTPE]i)?$"
_DNS_SUBDOMAIN = r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$"
_HOST = r"^(\*\.)?[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$"
_TAG = r"^([A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?$"
_POD_KINDS = {
    ("apps/v1", "Deployment"): ("spec", "template", "spec"),
    ("apps/v1", "StatefulSet"): ("spec", "template", "spec"),
    ("apps/v1", "DaemonSet"): ("spec", "template", "spec"),
    ("batch/v1", "Job"): ("spec", "template", "spec"),
    ("batch/v1", "CronJob"): ("spec", "jobTemplate", "spec", "template", "spec"),
}


class ChartError(ValueError):
    """The render cannot become a chart. ``code`` is a registered error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ sites


@dataclass(frozen=True)
class Site:
    """One place of a manifest that the chart fills from its values.

    ``kind``:

    - ``value``: the value at ``path``;
    - ``image``: the image ``images.<path[0]>`` (``repository[:tag][@digest]``);
    - ``namespace``: the release namespace;
    - ``literal``: ``text`` (a string holding ``{{``, escaped for Helm);
    - ``optional``: the key ``key`` with the value at ``path``, left out when
      the value is empty (``transform`` ``pull-secrets`` turns a list of names
      into ``[{"name": …}]``);
    - ``label``: the Helm label ``key`` (charts only; left out of manifests).
    """

    kind: str
    path: tuple[str | int, ...] = ()
    key: str = ""
    transform: str = ""
    text: str = ""


def _scalar(index: int) -> str:
    return f"__piceli_site_{index}__"


def _key(index: int) -> str:
    return f"~~piceli_site_{index}"


def _site_index(value: Any) -> int | None:
    if isinstance(value, str):
        match = _SCALAR.fullmatch(value)
        if match:
            return int(match.group(1))
    return None


def _key_index(key: Any) -> int | None:
    if isinstance(key, str) and key.startswith("~~piceli_site_"):
        return int(key.removeprefix("~~piceli_site_"))
    return None


# ----------------------------------------------------------------- images


def split_image(image: str) -> tuple[str, str, str]:
    """``(repository, tag, digest)`` of ``repository[:tag][@digest]``."""
    rest, _, digest = image.partition("@")
    last = rest.rsplit("/", 1)[-1]
    if ":" in last:
        repository, _, tag = rest.rpartition(":")
    else:
        repository, tag = rest, ""
    return repository, tag, digest


def join_image(repository: str, tag: str, digest: str) -> str:
    """The reference the chart's image helper renders."""
    return repository + (f":{tag}" if tag else "") + (f"@{digest}" if digest else "")


def _image_key(image: str) -> str:
    repository = split_image(image)[0]
    base = repository.rsplit("/", 1)[-1]
    return _KEY_UNSAFE.sub("-", base.lower()).strip("-") or "image"


# ----------------------------------------------------------------- schema


def _object(properties: Mapping[str, Any], description: str = "") -> dict[str, Any]:
    node: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(properties),
        "required": sorted(properties),
    }
    if description:
        node["description"] = description
    return node


def _string(description: str, pattern: str | None = None, **extra: Any) -> dict:
    node: dict[str, Any] = {"type": "string", "description": description, **extra}
    if pattern is not None:
        node["pattern"] = pattern
    return node


_QUANTITY_MAP = {
    "type": "object",
    "additionalProperties": {"type": ["string", "number"]},
}
_RESOURCES = {
    "type": "object",
    "description": "Container resources (requests and limits); values merge "
    "over the defaults, null removes them",
    "additionalProperties": False,
    "properties": {"limits": _QUANTITY_MAP, "requests": _QUANTITY_MAP},
}
_NODE_SELECTOR = {
    "type": "object",
    "description": "Pod node selector; values merge over the defaults and "
    "null removes an entry (for example a kubernetes.io/hostname pin that "
    "names a node of another cluster)",
    "additionalProperties": {"type": "string"},
}
_PULL_SECRETS = {
    "type": "array",
    "description": "Names of existing Secrets of type "
    "kubernetes.io/dockerconfigjson, added to every pod (imagePullSecrets)",
    "items": {"type": "string", "pattern": _DNS_SUBDOMAIN},
}


def _image_schema(required: bool) -> dict[str, Any]:
    repository = _string(
        "Image repository, such as registry.example/team/api",
        r"^[^\s@]+$",
        minLength=1,
    )
    node = _object(
        {
            "repository": repository,
            "tag": _string("Image tag; empty when pinned by digest only", _TAG),
            "digest": _string(
                "Image digest (sha256:…); recommended", r"^(sha256:[a-f0-9]{64})?$"
            ),
        },
        "Image reference: repository[:tag][@digest]"
        + ("; required: this image has no default" if required else ""),
    )
    node["anyOf"] = [
        {"properties": {"tag": {"minLength": 1}}},
        {"properties": {"digest": {"minLength": 1}}},
    ]
    return node


# ----------------------------------------------------------------- values


class _Tree:
    """Default values and their schema, built together.

    A value is required unless it is optional (a key the templates leave out
    when it is empty); an object is required when one of its values is. Only
    required keys are listed in ``required``, so a client can remove an
    optional key (or one of its entries) with ``null``, as Helm allows.
    """

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.leaves: dict[tuple[str, ...], tuple[dict[str, Any], bool]] = {}

    def set(
        self,
        path: Sequence[str],
        default: Any,
        schema: Mapping[str, Any],
        required: bool = True,
    ) -> None:
        values = self.values
        for part in path[:-1]:
            values = values.setdefault(part, {})
        values[path[-1]] = default
        self.leaves[tuple(path)] = (dict(schema), required)

    def has(self, path: Sequence[str]) -> bool:
        return tuple(path) in self.leaves

    def _node(self, prefix: tuple[str, ...]) -> tuple[dict[str, Any], bool]:
        if prefix in self.leaves:
            return self.leaves[prefix]
        children = sorted(
            {
                path[len(prefix)]
                for path in self.leaves
                if len(path) > len(prefix) and path[: len(prefix)] == prefix
            }
        )
        properties: dict[str, Any] = {}
        required: list[str] = []
        for child in children:
            node, needed = self._node((*prefix, child))
            properties[child] = node
            if needed:
                required.append(child)
        node = {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
        }
        if required:
            node["required"] = required
        return node, bool(required)

    def document(self) -> dict[str, Any]:
        root, _ = self._node(())
        # Helm may pass a ``global`` map to every chart.
        root["properties"]["global"] = {"type": "object"}
        return {"$schema": SCHEMA_DRAFT, "title": "Values", **root}


@dataclass(frozen=True)
class SecretRef:
    """A Secret the app reads and the client provides."""

    name: str
    type: str
    keys: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "keys": list(self.keys)}


@dataclass(frozen=True)
class ChartObject:
    """One object of the chart: its template file and its marked manifest."""

    component: str
    dependencies: tuple[str, ...]
    path: str
    manifest: dict[str, Any] = field(repr=False)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _label(manifest: Mapping[str, Any]) -> str:
    metadata = manifest.get("metadata") or {}
    namespace = metadata.get("namespace")
    name = metadata.get("name", "?")
    kind = manifest.get("kind", "?")
    return f"{kind}/{namespace}/{name}" if namespace else f"{kind}/{name}"


def _dig(value: Any, path: Sequence[str]) -> Any:
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


class _Builder:
    def __init__(
        self,
        namespace: str,
        secrets: Mapping[str, SecretRef],
        claims: set[str],
    ) -> None:
        self.namespace = namespace
        self.secrets = secrets
        self.claims = claims  # PersistentVolumeClaims the chart renders
        self.existing_claims: list[str] = []
        self.sites: list[Site] = []
        self.tree = _Tree()
        self.images: dict[str, str] = {}  # image reference -> key
        self.required: list[str] = []
        self.pull_secrets = False

    def site(self, site: Site) -> int:
        self.sites.append(site)
        return len(self.sites) - 1

    def scalar(self, site: Site) -> str:
        return _scalar(self.site(site))

    def value(self, path: Sequence[str | int], default: Any, schema: Mapping) -> str:
        keys = [part for part in path if isinstance(part, str)]
        if not any(isinstance(part, int) for part in path):
            self.tree.set(keys, default, schema)
        return self.scalar(Site("value", tuple(path)))

    def optional(
        self,
        mapping: dict[str, Any],
        key: str,
        path: Sequence[str],
        default: Any,
        schema: Mapping,
        transform: str = "",
    ) -> None:
        if not self.tree.has(path):
            self.tree.set(path, default, schema, required=False)
        mapping.pop(key, None)
        mapping[_key(self.site(Site("optional", tuple(path), key, transform)))] = None

    def image(self, image: str) -> str:
        key = self.images.get(image)
        if key is None:
            base = _image_key(image)
            key, number = base, 1
            while key in self.images.values():
                number += 1
                key = f"{base}-{number}"
            self.images[image] = key
            unresolved = bool(_UNRESOLVED.search(image))
            repository, tag, digest = ("", "", "") if unresolved else split_image(image)
            if unresolved:
                self.required.append(f"images.{key}")
            self.tree.set(
                ("images", key),
                {"repository": repository, "tag": tag, "digest": digest},
                _image_schema(unresolved),
            )
        return self.scalar(Site("image", (key,)))

    # ------------------------------------------------------------- objects

    def object(self, manifest: dict[str, Any]) -> None:
        kind = manifest.get("kind")
        api_version = manifest.get("apiVersion")
        metadata = manifest.setdefault("metadata", {})
        name = str(metadata.get("name", ""))
        if metadata.get("namespace") == self.namespace:
            metadata["namespace"] = self.scalar(Site("namespace"))
        labels = metadata.get("labels")
        if not isinstance(labels, dict):
            labels = metadata["labels"] = {}
        for label in HELM_LABELS:
            if label not in labels:
                labels[_key(self.site(Site("label", key=label)))] = None
        if kind in {"RoleBinding", "ClusterRoleBinding"}:
            for subject in manifest.get("subjects") or []:
                if isinstance(subject, dict) and subject.get("namespace") == (
                    self.namespace
                ):
                    subject["namespace"] = self.scalar(Site("namespace"))
        pod_path = _POD_KINDS.get((str(api_version), str(kind)))
        if pod_path is not None:
            self.workload(manifest, name, pod_path)
        elif (api_version, kind) == ("v1", "PersistentVolumeClaim"):
            self.claim(manifest.get("spec") or {}, ("claims", name))
        elif (api_version, kind) == ("v1", "ConfigMap"):
            data = manifest.get("data") or {}
            for key in sorted(data):
                if isinstance(data[key], str):
                    data[key] = self.value(
                        ("config", name, key),
                        data[key],
                        _string(f"ConfigMap {name}, key {key}"),
                    )
        elif (api_version, kind) == ("networking.k8s.io/v1", "Ingress"):
            self.ingress(manifest.get("spec") or {}, name)
        elif kind == "HTTPRoute" and str(api_version).startswith(
            "gateway.networking.k8s.io/"
        ):
            spec = manifest.get("spec") or {}
            if isinstance(spec.get("hostnames"), list):
                spec["hostnames"] = self.value(
                    ("httpRoutes", name, "hostnames"),
                    list(spec["hostnames"]),
                    {
                        "type": "array",
                        "description": f"HTTPRoute {name}: hostnames",
                        "items": {"type": "string", "pattern": _HOST},
                    },
                )

    def workload(
        self, manifest: dict[str, Any], name: str, pod_path: Sequence[str]
    ) -> None:
        spec = manifest.get("spec") or {}
        base = ("workloads", name)
        if manifest.get("kind") in {"Deployment", "StatefulSet"} and isinstance(
            spec.get("replicas"), int
        ):
            spec["replicas"] = self.value(
                (*base, "replicas"),
                spec["replicas"],
                {
                    "type": "integer",
                    "minimum": 0,
                    "description": f"{manifest['kind']} {name}: replicas",
                },
            )
        pod = _dig(manifest, pod_path)
        if not isinstance(pod, dict):
            return
        for group in ("initContainers", "containers"):
            for container in pod.get(group) or []:
                if not isinstance(container, dict):
                    continue
                if isinstance(container.get("image"), str):
                    container["image"] = self.image(container["image"])
                cname = str(container.get("name", ""))
                self.optional(
                    container,
                    "resources",
                    (*base, "containers", cname, "resources"),
                    container.get("resources") or {},
                    _RESOURCES,
                )
        self.optional(
            pod,
            "nodeSelector",
            (*base, "nodeSelector"),
            pod.get("nodeSelector") or {},
            _NODE_SELECTOR,
        )
        if "imagePullSecrets" not in pod:
            self.pull_secrets = True
            self.optional(
                pod,
                "imagePullSecrets",
                ("imagePullSecrets",),
                [],
                _PULL_SECRETS,
                "pull-secrets",
            )
        self.secret_refs(pod)
        self.claim_refs(pod)
        for template in spec.get("volumeClaimTemplates") or []:
            if isinstance(template, dict):
                claim = str((template.get("metadata") or {}).get("name", ""))
                self.claim(template.get("spec") or {}, (*base, "storage", claim))

    def claim(self, spec: dict[str, Any], path: Sequence[str]) -> None:
        label = ".".join(path)
        requests = _dig(spec, ("resources", "requests"))
        if isinstance(requests, dict) and isinstance(requests.get("storage"), str):
            requests["storage"] = self.value(
                (*path, "size"),
                requests["storage"],
                _string(f"{label}: requested storage", _QUANTITY),
            )
        self.optional(
            spec,
            "storageClassName",
            (*path, "storageClassName"),
            spec.get("storageClassName") or "",
            _string(
                f"{label}: storage class; empty for the cluster default",
                r"^([a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?)?$",
            ),
        )

    def ingress(self, spec: dict[str, Any], name: str) -> None:
        hosts: list[str] = []
        for rule in spec.get("rules") or []:
            if isinstance(rule, dict) and isinstance(rule.get("host"), str):
                if rule["host"] not in hosts:
                    hosts.append(rule["host"])
        for tls in spec.get("tls") or []:
            for host in (tls.get("hosts") or []) if isinstance(tls, dict) else []:
                if isinstance(host, str) and host not in hosts:
                    hosts.append(host)
        path = ("ingresses", name)
        if hosts:
            self.tree.set(
                (*path, "hosts"),
                hosts,
                {
                    "type": "array",
                    "description": f"Ingress {name}: hosts, in the order the "
                    "rules and TLS entries use them",
                    "items": {"type": "string", "pattern": _HOST},
                    "minItems": len(hosts),
                    "maxItems": len(hosts),
                },
            )
            for rule in spec.get("rules") or []:
                if isinstance(rule, dict) and isinstance(rule.get("host"), str):
                    rule["host"] = self.scalar(
                        Site("value", (*path, "hosts", hosts.index(rule["host"])))
                    )
            for tls in spec.get("tls") or []:
                if isinstance(tls, dict) and isinstance(tls.get("hosts"), list):
                    tls["hosts"] = [
                        self.scalar(Site("value", (*path, "hosts", hosts.index(host))))
                        if isinstance(host, str)
                        else host
                        for host in tls["hosts"]
                    ]
        for tls in spec.get("tls") or []:
            if isinstance(tls, dict) and tls.get("secretName") in self.secrets:
                tls["secretName"] = self.secret_name(tls["secretName"])
        self.optional(
            spec,
            "ingressClassName",
            (*path, "className"),
            spec.get("ingressClassName") or "",
            _string(f"Ingress {name}: ingress class; empty for the default"),
        )

    def claim_refs(self, pod: dict[str, Any]) -> None:
        """Claims the pod mounts that the chart does not render: existing ones."""
        for volume in pod.get("volumes") or []:
            claim = _dig(volume, ("persistentVolumeClaim",))
            name = claim.get("claimName") if isinstance(claim, dict) else None
            if not isinstance(name, str) or name in self.claims:
                continue
            if name not in self.existing_claims:
                self.existing_claims.append(name)
            claim["claimName"] = self.value(
                ("existingClaims", name, "claimName"),
                name,
                _string(
                    f"Name of the existing PersistentVolumeClaim for {name!r}; "
                    "the chart never creates it",
                    _DNS_SUBDOMAIN,
                    minLength=1,
                ),
            )

    def secret_refs(self, pod: dict[str, Any]) -> None:
        def ref(mapping: Any, key: str) -> None:
            if isinstance(mapping, dict) and mapping.get(key) in self.secrets:
                mapping[key] = self.secret_name(mapping[key])

        for group in ("initContainers", "containers"):
            for container in pod.get(group) or []:
                if not isinstance(container, dict):
                    continue
                for item in container.get("env") or []:
                    ref(_dig(item, ("valueFrom", "secretKeyRef")), "name")
                for item in container.get("envFrom") or []:
                    ref(
                        item.get("secretRef") if isinstance(item, dict) else None,
                        "name",
                    )
        for volume in pod.get("volumes") or []:
            if not isinstance(volume, dict):
                continue
            ref(volume.get("secret"), "secretName")
            for source in _dig(volume, ("projected", "sources")) or []:
                ref(source.get("secret") if isinstance(source, dict) else None, "name")

    def secret_name(self, name: str) -> str:
        secret = self.secrets[name]
        keys = ", ".join(secret.keys) or "-"
        return self.value(
            ("secrets", name, "name"),
            name,
            _string(
                f"Name of the existing Secret for {name!r} (type {secret.type}; "
                f"keys: {keys}); the chart never creates it",
                _DNS_SUBDOMAIN,
                minLength=1,
            ),
        )

    def escape(self, value: Any) -> Any:
        """Replace literal strings holding ``{{`` with literal sites."""
        if isinstance(value, dict):
            for key in list(value):
                if isinstance(key, str) and "{{" in key:
                    raise ChartError(
                        "chart-invalid",
                        "a mapping key holds '{{'; Helm cannot escape it",
                    )
                value[key] = self.escape(value[key])
            return value
        if isinstance(value, list):
            return [self.escape(item) for item in value]
        if isinstance(value, str) and "{{" in value and _site_index(value) is None:
            return self.scalar(Site("literal", text=value))
        return value


# ------------------------------------------------------------------ chart


@dataclass(frozen=True)
class Chart:
    """A chart built from one render. See :func:`build_chart`."""

    name: str
    version: str
    app_version: str
    namespace: str
    objects: tuple[ChartObject, ...] = field(repr=False)
    sites: tuple[Site, ...] = field(repr=False)
    values: dict[str, Any] = field(repr=False)
    schema: dict[str, Any] = field(repr=False)
    secrets: tuple[SecretRef, ...] = ()
    required: tuple[str, ...] = ()
    existing_claims: tuple[str, ...] = ()

    # ------------------------------------------------------------- files

    def chart_yaml(self) -> bytes:
        import yaml

        document = self.metadata()
        return yaml.safe_dump(document, sort_keys=True).encode()

    def metadata(self) -> dict[str, Any]:
        return {
            "apiVersion": "v2",
            "name": self.name,
            "version": self.version,
            "appVersion": self.app_version,
            "type": "application",
            "description": f"{self.name}, rendered by Piceli from a typed app",
        }

    def values_yaml(self) -> bytes:
        import yaml

        header = (
            f"# Values of the {self.name} chart (generated by piceli chart).\n"
            "# values.schema.json documents and validates every key.\n"
        )
        if self.required:
            header += "# Required (no default): " + ", ".join(self.required) + "\n"
        if self.secrets:
            header += (
                "# Existing Secrets the chart reads (never created by it): "
                + ", ".join(item.name for item in self.secrets)
                + "\n"
            )
        if self.existing_claims:
            header += (
                "# Existing volume claims the pods mount (never created by it): "
                + ", ".join(self.existing_claims)
                + "\n"
            )
        body = yaml.safe_dump(self.values, sort_keys=True, width=1000)
        return (header + body).encode()

    def schema_json(self) -> bytes:
        return (json.dumps(self.schema, indent=2, sort_keys=True) + "\n").encode()

    def helpers(self) -> bytes:
        return (
            f'{{{{- define "{self.name}.image" -}}}}\n'
            "{{- .repository -}}\n"
            "{{- with .tag }}:{{ . }}{{ end -}}\n"
            "{{- with .digest }}@{{ . }}{{ end -}}\n"
            "{{- end -}}\n"
        ).encode()

    def template(self, item: ChartObject) -> bytes:
        import yaml

        depends = ", ".join(item.dependencies) or "-"
        header = f"# piceli component: {item.component} (depends on: {depends})\n"
        body = yaml.safe_dump(item.manifest, sort_keys=True, width=100000)
        body = _KEY.sub(self._key_text, body)
        body = _SCALAR.sub(lambda match: self._scalar_text(int(match.group(1))), body)
        return (header + body).encode()

    def _key_text(self, match: re.Match[str]) -> str:
        indent = match.group("indent")
        site = self.sites[int(match.group("site"))]
        if site.kind == "label":
            expression = (
                '(printf "%s-%s" .Chart.Name (.Chart.Version | replace "+" "_"))'
                if site.key == "helm.sh/chart"
                else ".Release.Service"
            )
            return f"{indent}{site.key}: {{{{ {expression} | toJson }}}}"
        value = _index(site.path)
        if site.transform == "pull-secrets":
            body = (
                "{{ $names := list }}{{ range . }}"
                '{{ $names = append $names (dict "name" .) }}{{ end }}'
                "{{ toJson $names }}"
            )
        else:
            body = "{{ toJson . }}"
        return (
            f"{indent}{{{{- with {value} }}}}\n"
            f"{indent}{site.key}: {body}\n"
            f"{indent}{{{{- end }}}}"
        )

    def _scalar_text(self, index: int) -> str:
        site = self.sites[index]
        if site.kind == "value":
            return f"{{{{ {_index(site.path)[1:-1]} | toJson }}}}"
        if site.kind == "image":
            images = _index(("images", *site.path))
            return f'{{{{ include "{self.name}.image" {images} | toJson }}}}'
        if site.kind == "namespace":
            return "{{ .Release.Namespace | toJson }}"
        if site.kind == "literal":
            return "{{ " + json.dumps(site.text, ensure_ascii=False) + " | toJson }}"
        raise ValueError(f"site {index} is not a scalar")  # pragma: no cover

    def readme(self) -> bytes:
        lines = [
            f"# {self.name}",
            "",
            "Generated by `piceli chart` from a typed app. Install it with",
            "",
            "```sh",
            f"helm install {self.name} <this chart> --namespace <namespace> "
            "--values my-values.yaml",
            "```",
            "",
            "`values.yaml` holds the defaults and `values.schema.json` documents "
            "and validates every value (images, pull secrets, replicas, "
            "resources, node selectors, storage, hosts, config, Secret names).",
            "",
        ]
        if self.required:
            lines += ["## Required values", ""]
            lines += [f"- `{path}`" for path in self.required]
            lines.append("")
        if self.secrets:
            lines += [
                "## Secrets to create first",
                "",
                "The chart never renders a Secret. Create these in the release "
                "namespace (or set `secrets.<name>.name` to one you have):",
                "",
            ]
            for item in self.secrets:
                keys = ", ".join(f"`{key}`" for key in item.keys) or "none"
                lines.append(f"- `{item.name}` (type `{item.type}`): keys {keys}")
            lines.append("")
        if self.existing_claims:
            lines += [
                "## Existing volume claims",
                "",
                "The app mounts these PersistentVolumeClaims and the chart never "
                "creates them. Create them first (or set "
                "`existingClaims.<name>.claimName` to one you have):",
                "",
            ]
            lines += [f"- `{name}`" for name in self.existing_claims]
            lines.append("")
        return "\n".join(lines).encode()

    def files(self) -> tuple[ManifestFile, ...]:
        """The chart's files, relative to the chart directory, sorted."""
        items = [
            ManifestFile(".helmignore", f"{MARKER}\n".encode()),
            ManifestFile("Chart.yaml", self.chart_yaml()),
            ManifestFile("README.md", self.readme()),
            ManifestFile("values.yaml", self.values_yaml()),
            ManifestFile("values.schema.json", self.schema_json()),
            ManifestFile("templates/_helpers.tpl", self.helpers()),
        ]
        for item in self.objects:
            items.append(ManifestFile(item.path, self.template(item)))
        paths = [item.path for item in items]
        if len(set(paths)) != len(paths):
            raise ChartError("chart-invalid", "two objects render to the same file")
        return tuple(sorted(items, key=lambda item: item.path))

    @property
    def archive_name(self) -> str:
        return f"{self.name}-{self.version}.tgz"

    def archive(self) -> bytes:
        """The chart archive ``helm package`` would write; byte-identical per chart."""
        files = [
            ManifestFile(f"{self.name}/{item.path}", item.content)
            for item in self.files()
        ]
        return gzip_bytes(tar_bytes(files))

    def oci(self) -> FluxArtifact:
        """The OCI layout of ``helm push``: Helm config and one chart layer."""
        layer = Blob(HELM_CONTENT, self.archive())
        config = Blob(HELM_CONFIG, _canonical(self.metadata()))
        document = {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": config.descriptor(),
            "layers": [layer.descriptor()],
            "annotations": {
                "org.opencontainers.image.description": self.metadata()["description"],
                "org.opencontainers.image.title": self.name,
                "org.opencontainers.image.version": self.version,
            },
        }
        return FluxArtifact(config, layer, _canonical(document), (self.archive_name,))

    @property
    def tag(self) -> str:
        """The OCI tag of this version (``+`` is not allowed in a tag)."""
        return self.version.replace("+", "_")

    def public(self) -> dict[str, Any]:
        return {
            "chart": self.name,
            "version": self.version,
            "app_version": self.app_version,
            "namespace": self.namespace,
            "objects": len(self.objects),
            "required_values": list(self.required),
            "secrets": [item.public() for item in self.secrets],
            "existing_claims": list(self.existing_claims),
        }

    # --------------------------------------------------------- manifests

    def manifests(
        self, values: Mapping[str, Any] | None = None, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Components (as ``piceli.app.render.rendered``) with values applied.

        ``values`` is merged over the defaults like Helm does (maps merge,
        anything else replaces, ``null`` removes a key) and validated against
        the schema.

        :raises ChartError: ``chart-values-invalid``.
        """
        merged = merge_values(self.values, values or {})
        problems = validate(merged, self.schema)
        if problems:
            raise ChartError(
                "chart-values-invalid", "values do not match the chart: " + problems[0]
            )
        target = namespace or self.namespace
        components: dict[str, dict[str, Any]] = {}
        for item in self.objects:
            component = components.setdefault(
                item.component,
                {
                    "name": item.component,
                    "dependencies": list(item.dependencies),
                    "resources": [],
                },
            )
            component["resources"].append(
                {
                    "manifest": self._resolve(item.manifest, merged, target),
                    "secret_bindings": [],
                }
            )
        return list(components.values())

    def _only_labels(self, value: Any) -> bool:
        if not isinstance(value, dict) or not value:
            return False
        indexes = [_key_index(key) for key in value]
        return all(
            index is not None and self.sites[index].kind == "label" for index in indexes
        )

    def _resolve(self, value: Any, values: Mapping[str, Any], namespace: str) -> Any:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, child in value.items():
                index = _key_index(key)
                if index is None:
                    if self._only_labels(child):
                        continue  # labels the chart created for its own labels
                    result[key] = self._resolve(child, values, namespace)
                    continue
                site = self.sites[index]
                if site.kind != "optional":
                    continue  # Helm labels exist only in the chart
                found = _lookup(values, site.path)
                if found:
                    result[site.key] = (
                        [{"name": name} for name in found]
                        if site.transform == "pull-secrets"
                        else copy.deepcopy(found)
                    )
            return result
        if isinstance(value, list):
            return [self._resolve(item, values, namespace) for item in value]
        index = _site_index(value)
        if index is None:
            return value
        site = self.sites[index]
        if site.kind == "namespace":
            return namespace
        if site.kind == "literal":
            return site.text
        if site.kind == "image":
            image = _lookup(values, ("images", *site.path))
            return join_image(image["repository"], image["tag"], image["digest"])
        return copy.deepcopy(_lookup(values, site.path))


def _index(path: Sequence[str | int]) -> str:
    parts = " ".join(
        json.dumps(part) if isinstance(part, str) else str(part) for part in path
    )
    return f"(index .Values {parts})"


def _lookup(values: Any, path: Sequence[str | int]) -> Any:
    for part in path:
        if isinstance(part, int):
            values = (
                values[part]
                if isinstance(values, list) and 0 <= part < len(values)
                else None
            )
        else:
            values = values.get(part) if isinstance(values, Mapping) else None
    return values


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def merge_values(defaults: Mapping[str, Any], override: Mapping[str, Any]) -> dict:
    """Helm's coalescing: maps merge, other values replace, ``None`` removes."""
    result = copy.deepcopy(dict(defaults))
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = merge_values(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def validate(value: Any, schema: Mapping[str, Any], where: str = "values") -> list[str]:
    """Problems of ``value`` against the subset of JSON Schema the chart uses.

    Messages name the path and the rule, never the value.
    """
    problems: list[str] = []
    kind = schema.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(_is(value, item) for item in kinds):
            return [f"{where}: must be {' or '.join(kinds)}"]
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            problems.append(f"{where}: must not be empty")
        pattern = schema.get("pattern")
        if pattern is not None and not re.search(pattern, value):
            problems.append(f"{where}: does not match {pattern}")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{where}: must be at least {schema['minimum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            problems.append(f"{where}: needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{where}: allows at most {schema['maxItems']} item(s)")
        if "items" in schema:
            for number, item in enumerate(value):
                problems += validate(item, schema["items"], f"{where}[{number}]")
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for key in schema.get("required") or ():
            if key not in value:
                problems.append(f"{where}.{key}: is required")
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                problems += validate(item, properties[key], f"{where}.{key}")
            elif extra is False:
                problems.append(f"{where}.{key}: is not a value of this chart")
            elif isinstance(extra, dict):
                problems += validate(item, extra, f"{where}.{key}")
    if "anyOf" in schema and not any(
        not validate(value, option, where) for option in schema["anyOf"]
    ):
        problems.append(f"{where}: needs a tag or a digest")
    return problems


def _is(value: Any, kind: str) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    return kind == "null" and value is None


def chart_name(components: Sequence[Mapping[str, Any]]) -> str | None:
    """The app name: the one ``app.kubernetes.io/part-of`` label of the render."""
    names = {
        ((resource["manifest"].get("metadata") or {}).get("labels") or {}).get(
            "app.kubernetes.io/part-of"
        )
        for component in components
        for resource in component["resources"]
    }
    return names.pop() if len(names) == 1 and None not in names else None


def build_chart(
    components: Sequence[Mapping[str, Any]],
    *,
    name: str,
    version: str,
    namespace: str,
    app_version: str | None = None,
) -> Chart:
    """Turn a render (``piceli.app.render.rendered`` components) into a chart.

    :raises ChartError: ``chart-invalid`` (name or version), ``chart-secret-value``
        (a non-Secret object holds a redacted or apply-time secret value) or
        ``chart-empty``.
    """
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ChartError(
            "chart-invalid",
            "the chart name must be lowercase letters, digits and '-', start "
            "with a letter, at most 53 characters",
        )
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        raise ChartError("chart-invalid", "the chart version must be SemVer 2")
    app_version = version if app_version is None else app_version
    if not _APP_VERSION.fullmatch(app_version):
        raise ChartError(
            "chart-invalid", "the app version must be short printable text"
        )
    secrets: dict[str, SecretRef] = {}
    kept: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for component in components:
        for resource in component["resources"]:
            manifest = resource["manifest"]
            if manifest.get("kind") == "Secret" and manifest.get("apiVersion") == "v1":
                metadata = manifest.get("metadata") or {}
                keys = {
                    *(manifest.get("data") or {}),
                    *(manifest.get("stringData") or {}),
                }
                secrets[str(metadata.get("name"))] = SecretRef(
                    str(metadata.get("name")),
                    str(manifest.get("type") or "Opaque"),
                    tuple(sorted(keys)),
                )
                continue
            if resource.get("secret_bindings") or any(
                value in _SENTINELS for value in _strings(manifest)
            ):
                raise ChartError(
                    "chart-secret-value",
                    f"{_label(manifest)} holds a value Piceli redacts or injects "
                    "at apply time; a chart never carries secret values. Move it "
                    "to a Secret (the chart references it by name)",
                )
            kept.append((component, manifest))
    if not kept:
        raise ChartError("chart-empty", "the render has no object for a chart")
    claims = {
        str((manifest.get("metadata") or {}).get("name"))
        for _, manifest in kept
        if (manifest.get("apiVersion"), manifest.get("kind"))
        == ("v1", "PersistentVolumeClaim")
    }
    builder = _Builder(namespace, secrets, claims)
    objects = []
    for component, manifest in kept:
        marked = copy.deepcopy(dict(manifest))
        builder.object(marked)
        marked = builder.escape(marked)
        objects.append(
            ChartObject(
                str(component["name"]),
                tuple(component.get("dependencies") or ()),
                "templates/" + file_name(str(component["name"]), manifest),
                marked,
            )
        )
    for secret in secrets.values():
        if not builder.tree.has(("secrets", secret.name, "name")):
            builder.secret_name(secret.name)
    if not builder.pull_secrets:
        builder.tree.set(("imagePullSecrets",), [], _PULL_SECRETS, required=False)
    return Chart(
        name=name,
        version=version,
        app_version=app_version,
        namespace=namespace,
        objects=tuple(objects),
        sites=tuple(builder.sites),
        values=builder.tree.values,
        schema=builder.tree.document(),
        secrets=tuple(secrets[key] for key in sorted(secrets)),
        required=tuple(builder.required),
        existing_claims=tuple(sorted(builder.existing_claims)),
    )


def _previous(directory: Any) -> set[str] | None:
    """Files a previous chart render wrote; ``set()`` when absent or empty;
    ``None`` when the directory holds files Piceli did not write."""
    if not directory.exists():
        return set()
    if directory.is_symlink() or not directory.is_dir():
        return None
    if not any(directory.iterdir()):
        return set()
    try:
        value = json.loads((directory / MARKER).read_text())
        listed = value["files"] if value.get("schema") == MARKER_SCHEMA else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if not isinstance(listed, list) or not all(
        isinstance(name, str)
        and name
        and not name.startswith("/")
        and ".." not in name.split("/")
        for name in listed
    ):
        return None
    return set(listed)


def write_chart(directory: Any, chart: Chart) -> list[str]:
    """Write ``chart`` into ``directory`` (a ``pathlib.Path``); the file list.

    The directory must be absent, empty or written by a previous chart render
    (it holds ``.piceli-chart``): then files that render wrote and this one
    does not are removed, and other files are refused.

    :raises ChartError: ``chart-out-refused``.
    """
    previous = _previous(directory)
    if previous is None:
        raise ChartError(
            "chart-out-refused",
            f"{directory} is not empty and was not written by `piceli chart render`",
        )
    files = chart.files()
    names = [item.path for item in files]
    for name in names:
        target = directory / name
        if name not in previous and (target.exists() or target.is_symlink()):
            raise ChartError(
                "chart-out-refused", f"{name} exists in {directory} and is not piceli's"
            )
    directory.mkdir(parents=True, exist_ok=True)
    for name in sorted(previous - set(names)):
        (directory / name).unlink(missing_ok=True)
    for item in files:
        target = directory / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            target.unlink()
        target.write_bytes(item.content)
    marker = {"schema": MARKER_SCHEMA, "files": names}
    (directory / MARKER).write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n")
    return names


def archive_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def strip_helm_labels(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """``manifest`` without the labels the chart adds (for comparisons)."""
    result = copy.deepcopy(dict(manifest))
    labels = (result.get("metadata") or {}).get("labels")
    if isinstance(labels, dict):
        for label in HELM_LABELS:
            labels.pop(label, None)
    return result
