"""Hand rendered manifests to a GitOps controller (Flux or Argo CD).

``piceli render --out DIR`` writes the manifests as files (a directory to
commit to Git) and ``piceli publish`` pushes the same files as an OCI
artifact in the layout ``flux push artifact`` produces, which Flux's
``OCIRepository`` (and Argo CD's OCI sources) consume:

- an OCI image manifest (``application/vnd.oci.image.manifest.v1+json``)
- a config blob of media type ``application/vnd.cncf.flux.config.v1+json``
- one layer of media type ``application/vnd.cncf.flux.content.v1.tar+gzip``:
  a gzip-compressed tar of the manifest files.

Everything is deterministic: one YAML file per object with sorted keys and
a stable name, tar entries sorted with zero timestamps and owners, gzip with
no name and a zero mtime, canonical JSON for the config and the manifest. The
same render therefore always has the same digest, which is what
``piceli publish --approve DIGEST`` approves.

Content rules (the handoff boundary): a GitOps controller applies what it is
given, so the files must be complete and must never carry secret material.

- A ``Secret`` object is refused (``gitops-secrets-present``) unless the
  caller chooses ``secrets="external"``: then Secrets are left out and listed
  by name, to be provided by SOPS, an ``ExternalSecret`` or by hand.
- Any other object whose rendered form holds a redacted value or a private
  secret binding is refused (``gitops-secret-value``): the published file would
  differ from what Piceli would apply.
- A placeholder image (a pipeline build that has not run) is refused
  (``gitops-image-unresolved``).

Importing this module opens no socket and reads no file.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
FLUX_CONFIG = "application/vnd.cncf.flux.config.v1+json"
FLUX_CONTENT = "application/vnd.cncf.flux.content.v1.tar+gzip"
SOURCE_ANNOTATION = "org.opencontainers.image.source"
REVISION_ANNOTATION = "org.opencontainers.image.revision"
NAMESPACE_ANNOTATION = "io.piceli.render.namespace"
ENVIRONMENT_ANNOTATION = "io.piceli.render.environment"
SECRET_MODES = ("refuse", "external")

_SENTINELS = ("<redacted>", "<private>")
_UNRESOLVED = re.compile(r"\.piceli\.invalid(?:/|$)|@sha256:0{64}$")
_UNSAFE = re.compile(r"[^a-z0-9.-]+")
_ANNOTATION_VALUE_MAX = 1024


class GitOpsError(ValueError):
    """The render cannot be handed off. ``code`` is a registered error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ManifestFile:
    """One rendered object as a file of the handoff."""

    path: str
    content: bytes = field(repr=False)

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class Handoff:
    """The files to hand off and the Secrets left out (``namespace/name``)."""

    files: tuple[ManifestFile, ...]
    omitted_secrets: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "files": [item.path for item in self.files],
            "omitted_secrets": list(self.omitted_secrets),
        }


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


def _safe(text: str) -> str:
    return _UNSAFE.sub("-", text.lower()).strip("-.") or "x"


def file_name(component: str, manifest: Mapping[str, Any]) -> str:
    """``<component>_<kind>.<group>_<namespace>_<name>.yaml``, lowercase.

    Kubernetes names, namespaces, kinds and groups never contain ``_``, so the
    name is unique for every object of a render.
    """
    api_version = str(manifest.get("apiVersion", ""))
    group = api_version.rpartition("/")[0] or "core"
    metadata = manifest.get("metadata") or {}
    namespace = str(metadata.get("namespace") or "cluster")
    return (
        "_".join(
            (
                _safe(component),
                f"{_safe(str(manifest.get('kind', '')))}.{_safe(group)}",
                _safe(namespace),
                _safe(str(metadata.get("name", ""))),
            )
        )
        + ".yaml"
    )


def manifest_yaml(component: Mapping[str, Any], manifest: Mapping[str, Any]) -> bytes:
    """One object as deterministic YAML with a component header."""
    import yaml

    depends = ", ".join(component.get("dependencies") or ()) or "-"
    header = f"# piceli component: {component['name']} (depends on: {depends})\n"
    body = yaml.safe_dump(dict(manifest), sort_keys=True, default_flow_style=False)
    return (header + body).encode()


def handoff(
    components: Sequence[Mapping[str, Any]], secrets: str = "refuse"
) -> Handoff:
    """The files of a render (``piceli.app.render.rendered`` components).

    :raises GitOpsError: ``gitops-secrets-present``, ``gitops-secret-value``,
        ``gitops-image-unresolved`` or ``gitops-empty`` (see the module doc).
    """
    if secrets not in SECRET_MODES:
        raise ValueError("secrets must be 'refuse' or 'external'")
    files: dict[str, ManifestFile] = {}
    omitted: list[str] = []
    for component in components:
        for resource in component["resources"]:
            manifest = resource["manifest"]
            label = _label(manifest)
            if manifest.get("kind") == "Secret" and manifest.get("apiVersion") == "v1":
                if secrets == "refuse":
                    raise GitOpsError(
                        "gitops-secrets-present",
                        f"{label} is a Secret; a GitOps artifact never carries "
                        "secret values. Pass --secrets external and provide it "
                        "with SOPS, an ExternalSecret or by hand",
                    )
                metadata = manifest.get("metadata") or {}
                omitted.append(
                    f"{metadata.get('namespace') or ''}/{metadata.get('name')}"
                )
                continue
            strings = list(_strings(manifest))
            if resource.get("secret_bindings") or any(
                value in _SENTINELS for value in strings
            ):
                raise GitOpsError(
                    "gitops-secret-value",
                    f"{label} holds a value Piceli redacts or injects at apply "
                    "time; move it to a Secret (provided outside the artifact) "
                    "or, if it is not secret, list the field in the "
                    "piceli.io/public-fields annotation",
                )
            if any(_UNRESOLVED.search(value) for value in strings):
                raise GitOpsError(
                    "gitops-image-unresolved",
                    f"{label} uses a placeholder image; build and pin the image "
                    "(a release spec or receipt) before handing it off",
                )
            item = ManifestFile(
                file_name(component["name"], manifest),
                manifest_yaml(component, manifest),
            )
            if item.path in files:
                raise GitOpsError(
                    "render-model-invalid", f"{label} is rendered twice ({item.path})"
                )
            files[item.path] = item
    if not files:
        raise GitOpsError("gitops-empty", "the render has no object to hand off")
    return Handoff(tuple(files[path] for path in sorted(files)), tuple(sorted(omitted)))


# ------------------------------------------------------------- Git directory

MARKER = ".piceli-render"
MARKER_SCHEMA = "piceli.render-out.v1"


def _previous(directory: Path) -> set[str] | None:
    """Files a previous ``render --out`` wrote, ``set()`` for an empty or absent
    directory, ``None`` when the directory holds files Piceli did not write."""
    if not directory.exists():
        return set()
    if directory.is_symlink() or not directory.is_dir():
        return None
    entries = {entry.name for entry in directory.iterdir()}
    if not entries:
        return set()
    if MARKER not in entries:
        return None
    try:
        value = json.loads((directory / MARKER).read_text())
        listed = value["files"] if value.get("schema") == MARKER_SCHEMA else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if not isinstance(listed, list) or not all(
        isinstance(name, str) and name == Path(name).name and name.endswith(".yaml")
        for name in listed
    ):
        return None
    return set(listed)


def write_directory(directory: Path, result: Handoff) -> dict[str, Any]:
    """Write the files of ``result`` into ``directory`` (the Git directory).

    The directory must be absent, empty, or one a previous ``render --out``
    wrote (it holds the ``.piceli-render`` marker): then only the files that
    render listed are replaced, other files (a README, a kustomization) stay,
    and a new file never overwrites one Piceli did not write.

    :raises GitOpsError: ``render-out-refused``.
    """
    previous = _previous(directory)
    if previous is None:
        raise GitOpsError(
            "render-out-refused",
            f"{directory} is not empty and was not written by `piceli render --out`",
        )
    names = [item.path for item in result.files]
    for name in names:
        target = directory / name
        if name not in previous and (target.exists() or target.is_symlink()):
            raise GitOpsError(
                "render-out-refused",
                f"{name} exists in {directory} and was not written by piceli",
            )
    directory.mkdir(parents=True, exist_ok=True)
    for name in sorted(previous - set(names)):
        (directory / name).unlink(missing_ok=True)
    for item in result.files:
        target = directory / item.path
        if target.is_symlink():
            target.unlink()
        target.write_bytes(item.content)
    marker = {"schema": MARKER_SCHEMA, "files": names}
    (directory / MARKER).write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n")
    return {
        "files": names,
        "removed": sorted(previous - set(names)),
        "omitted_secrets": list(result.omitted_secrets),
    }


# ---------------------------------------------------------------- OCI layout


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sha(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def tar_bytes(files: Sequence[ManifestFile]) -> bytes:
    """An uncompressed, reproducible tar of ``files`` (sorted, no owners or times)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for item in sorted(files, key=lambda entry: entry.path):
            info = tarfile.TarInfo(item.path)
            info.size = len(item.content)
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(item.content))
    return buffer.getvalue()


def gzip_bytes(data: bytes) -> bytes:
    """gzip with no file name and a zero mtime, so equal input gives equal bytes."""
    buffer = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", fileobj=buffer, compresslevel=9, mtime=0
    ) as stream:
        stream.write(data)
    return buffer.getvalue()


@dataclass(frozen=True)
class Blob:
    media_type: str
    body: bytes = field(repr=False)

    @property
    def digest(self) -> str:
        return _sha(self.body)

    @property
    def size(self) -> int:
        return len(self.body)

    def descriptor(self) -> dict[str, Any]:
        return {"mediaType": self.media_type, "digest": self.digest, "size": self.size}


@dataclass(frozen=True)
class FluxArtifact:
    """A Flux-compatible OCI artifact: config, one content layer, the manifest."""

    config: Blob
    layer: Blob
    manifest: bytes = field(repr=False)
    files: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        return _sha(self.manifest)

    def public(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "media_type": OCI_MANIFEST,
            "config": self.config.descriptor(),
            "layer": self.layer.descriptor(),
            "files": list(self.files),
        }


def artifact_annotations(
    *,
    namespace: str | None = None,
    environment: str | None = None,
    source: str | None = None,
    revision: str | None = None,
) -> dict[str, str]:
    """The manifest annotations; values must be short printable text."""
    values = {
        NAMESPACE_ANNOTATION: namespace,
        ENVIRONMENT_ANNOTATION: environment,
        SOURCE_ANNOTATION: source,
        REVISION_ANNOTATION: revision,
    }
    result: dict[str, str] = {}
    for key, value in values.items():
        if value is None:
            continue
        if (
            not isinstance(value, str)
            or not value
            or len(value) > _ANNOTATION_VALUE_MAX
            or not value.isprintable()
        ):
            raise ValueError(f"invalid value for {key}")
        result[key] = value
    return result


def flux_artifact(
    files: Sequence[ManifestFile], metadata: Mapping[str, str] | None = None
) -> FluxArtifact:
    """Package ``files`` exactly as ``flux push artifact`` lays them out.

    The config blob mirrors the one Flux writes (an empty image config whose
    ``rootfs`` names the layer's uncompressed digest) without a creation time,
    so the digest depends only on the files and ``metadata``.
    """
    if not files:
        raise ValueError("an artifact needs at least one file")
    raw = tar_bytes(files)
    layer = Blob(FLUX_CONTENT, gzip_bytes(raw))
    config = Blob(
        FLUX_CONFIG,
        _canonical(
            {
                "architecture": "",
                "config": {},
                "os": "",
                "rootfs": {"type": "layers", "diff_ids": [_sha(raw)]},
            }
        ),
    )
    document: dict[str, Any] = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "config": config.descriptor(),
        "layers": [layer.descriptor()],
    }
    if metadata:
        document["annotations"] = dict(sorted(metadata.items()))
    return FluxArtifact(
        config,
        layer,
        _canonical(document),
        tuple(sorted(item.path for item in files)),
    )


def push(
    client: Any, repository: str, artifact: FluxArtifact, tag: str | None
) -> dict[str, Any]:
    """Push ``artifact`` by digest (then tag it) with a ``StreamedOciRegistryClient``.

    Blobs the registry already has are skipped; the manifest is ``PUT`` under
    its digest, then under ``tag``, and read back to verify both.
    """
    from piceli.artifacts.registry import RegistryError

    pushed = 0
    for blob in (artifact.config, artifact.layer):
        if client.has_blob(repository, blob.digest):
            continue
        client.push_blob(repository, blob.digest, blob.size, io.BytesIO(blob.body))
        pushed += 1
    digest = client.push_manifest(
        repository, artifact.digest, artifact.manifest, OCI_MANIFEST
    )
    if digest != artifact.digest:
        raise RegistryError("registry-digest-mismatch")
    if tag is not None:
        client.push_manifest(repository, tag, artifact.manifest, OCI_MANIFEST)
    for reference in (artifact.digest, tag):
        if reference is not None and (
            client.manifest_digest(repository, reference) != artifact.digest
        ):
            raise RegistryError("registry-digest-mismatch")
    return {"blobs_pushed": pushed, "blobs_present": 2 - pushed}
