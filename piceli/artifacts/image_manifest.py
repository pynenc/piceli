"""Turn a ``docker save`` or OCI image-layout tar into a pushable manifest and blobs.

One sequential pass over the tar hashes every member (and, for gzip members,
the uncompressed stream too) without writing anything to disk. The result is
a :class:`PushPlan`: the manifest bytes to ``PUT``, the config blob, and one
descriptor per layer with the tar member that holds it.

- **OCI image layout** (``oci-layout`` + ``index.json``; Docker ≥ 25 writes
  this too): the archive's own manifest is pushed byte for byte, so its
  digest is preserved. An index is followed to the one runnable platform
  manifest whose blobs are in the archive; attestation manifests are ignored.
- **Classic** ``docker save`` (``manifest.json`` only): there is no manifest,
  so a canonical OCI manifest is synthesized (config
  ``application/vnd.oci.image.config.v1+json``, layers
  ``application/vnd.oci.image.layer.v1.tar`` or ``…tar+gzip``). The same
  archive always yields the same bytes, hence the same manifest digest.

In both cases every layer's uncompressed digest must equal the config's
``rootfs.diff_ids`` entry, so the approved config digest fixes every layer.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, BinaryIO

from piceli.artifacts.plan import canonical, validate_digest

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar"
OCI_LAYER_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_CONFIG = "application/vnd.docker.container.image.v1+json"
DOCKER_LAYER = "application/vnd.docker.image.rootfs.diff.tar"
DOCKER_LAYER_GZIP = "application/vnd.docker.image.rootfs.diff.tar.gzip"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
MANIFEST_TYPES = {OCI_MANIFEST, DOCKER_MANIFEST}
INDEX_TYPES = {OCI_INDEX, DOCKER_LIST}
CONFIG_TYPES = {OCI_CONFIG, DOCKER_CONFIG}
# Layer media type -> compression the diff_id check must undo.
LAYER_TYPES = {
    OCI_LAYER: None,
    DOCKER_LAYER: None,
    OCI_LAYER_GZIP: "gzip",
    DOCKER_LAYER_GZIP: "gzip",
}
_METADATA = {"oci-layout", "index.json", "manifest.json", "repositories"}
_SMALL = 4 * 1024 * 1024
_KEPT_TOTAL = 64 * 1024 * 1024
_MAX_MEMBERS = 65_536
_MAX_LAYERS = 256
_MAX_EXPANSION = 64 * 1024**3
_BLOCK = 1024 * 1024


class ImageRejected(ValueError):
    """The image's config digest differs from the approval."""


@dataclass(frozen=True)
class BlobRef:
    """One blob of the image and the tar member that holds its bytes."""

    digest: str
    size: int
    media_type: str
    member: str = field(repr=False)

    def public(self) -> dict[str, Any]:
        return {"digest": self.digest, "size": self.size, "media_type": self.media_type}


@dataclass(frozen=True)
class PushPlan:
    """Everything a registry push needs, proven from one pass over the tar."""

    format: str
    manifest: bytes = field(repr=False)
    manifest_media_type: str
    manifest_digest: str
    manifest_origin: str
    config: BlobRef
    config_body: bytes = field(repr=False)
    layers: tuple[BlobRef, ...]
    platform: str
    names: tuple[str, ...]
    stream_sha256: str
    bytes: int

    @property
    def config_digest(self) -> str:
        return self.config.digest

    def blobs(self) -> tuple[BlobRef, ...]:
        """Config and layers, each digest once, in manifest order."""
        seen: set[str] = set()
        out: list[BlobRef] = []
        for blob in (self.config, *self.layers):
            if blob.digest not in seen:
                seen.add(blob.digest)
                out.append(blob)
        return tuple(out)


@dataclass
class _Member:
    digest: str
    size: int
    diff_id: str | None
    gzip: bool
    body: bytes | None = field(default=None, repr=False)


def member_name(name: str) -> str:
    """A safe, normalized tar member path (no absolute paths or ``..``)."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or "\0" in name:
        raise ValueError("unsafe image archive member")
    cleaned = str(path)
    if cleaned in {"", "."}:
        raise ValueError("unsafe image archive member")
    return cleaned


class _GunzipHash:
    """SHA-256 of the decompressed bytes of a (possibly multi-member) gzip stream."""

    def __init__(self) -> None:
        self.hash = hashlib.sha256()
        self.inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self.expanded = 0
        self.failed = False
        self.pending = False

    def update(self, data: bytes) -> None:
        if self.failed:
            return
        try:
            while data:
                self.pending = True
                self._add(self.inflater.decompress(data, _BLOCK))
                if self.inflater.eof:
                    # A multi-member gzip stream continues with a new member.
                    data = self.inflater.unused_data
                    self.inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    self.pending = False
                else:
                    data = self.inflater.unconsumed_tail
        except zlib.error:
            self.failed = True

    def _add(self, out: bytes) -> None:
        self.expanded += len(out)
        if self.expanded > _MAX_EXPANSION:
            raise ValueError("compressed layer expands beyond the limit")
        self.hash.update(out)

    def digest(self) -> str | None:
        if self.failed or self.pending:
            return None  # corrupt or truncated gzip stream
        return "sha256:" + self.hash.hexdigest()


class _Counter(io.RawIOBase):
    """Count, hash, cap and cancel the raw archive stream."""

    def __init__(
        self, source: BinaryIO, max_bytes: int, cancel: threading.Event | None
    ) -> None:
        self.source = source
        self.max_bytes = max_bytes
        self.cancel = cancel
        self.count = 0
        self.hash = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self.cancel is not None and self.cancel.is_set():
            raise InterruptedError("delivery cancelled")
        block = self.source.read(min(len(buffer), _BLOCK))
        self.count += len(block)
        if self.count > self.max_bytes:
            raise ValueError("image stream exceeds byte limit")
        self.hash.update(block)
        buffer[: len(block)] = block
        return len(block)


def _hash_member(stream: BinaryIO, size: int, keep: bool) -> _Member:
    digest = hashlib.sha256()
    gunzip: _GunzipHash | None = None
    kept = bytearray() if keep else None
    first = True
    while block := stream.read(_BLOCK):
        if first:
            first = False
            if block[:2] == b"\x1f\x8b":
                gunzip = _GunzipHash()
        digest.update(block)
        if gunzip is not None:
            gunzip.update(block)
        if kept is not None:
            kept.extend(block)
    plain = "sha256:" + digest.hexdigest()
    return _Member(
        plain,
        size,
        gunzip.digest() if gunzip is not None else plain,
        gunzip is not None,
        bytes(kept) if kept is not None else None,
    )


def _json(body: bytes | None) -> dict[str, Any]:
    if body is None:
        raise ValueError("image metadata absent from archive")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def scan_image_stream(
    source: BinaryIO,
    *,
    max_bytes: int = 16 * 1024**3,
    cancel: threading.Event | None = None,
) -> PushPlan:
    """Validate an image tar read once from ``source`` and plan its push."""
    counter = _Counter(source, max_bytes, cancel)
    buffered = io.BufferedReader(counter, _BLOCK)
    metadata: dict[str, bytes] = {}
    members: dict[str, _Member] = {}
    kept_total = 0
    with tarfile.open(fileobj=buffered, mode="r|") as archive:  # type: ignore[call-overload]
        for count, info in enumerate(archive, start=1):
            if count > _MAX_MEMBERS:
                raise ValueError("image archive has too many members")
            name = member_name(info.name)
            if info.isdir():
                continue
            if info.issym() or info.islnk():
                # Docker's classic format links duplicate layers; they are
                # never referenced by a manifest this module accepts.
                if info.linkname.startswith("/"):
                    raise ValueError("unsafe image archive link")
                continue
            if not info.isfile():
                raise ValueError("unsafe image archive member")
            stream = archive.extractfile(info)
            assert stream is not None
            if name in _METADATA:
                if info.size > _SMALL or name in metadata:
                    raise ValueError("invalid image archive index")
                metadata[name] = stream.read()
                continue
            if name in members:
                raise ValueError("duplicate image archive member")
            keep = info.size <= _SMALL and kept_total + info.size <= _KEPT_TOTAL
            members[name] = _hash_member(stream, info.size, keep)  # type: ignore[arg-type]
            if keep:
                kept_total += info.size
    while buffered.read(_BLOCK):  # trailing padding is part of the stream hash
        pass
    stream_sha = "sha256:" + counter.hash.hexdigest()
    if "oci-layout" in metadata:
        return _plan_oci(metadata, members, stream_sha, counter.count)
    if "manifest.json" in metadata:
        return _plan_docker(metadata, members, stream_sha, counter.count)
    raise ValueError("unrecognized image archive format")


def _platform(config: Mapping[str, Any]) -> str:
    os_name, arch = config.get("os"), config.get("architecture")
    if not isinstance(os_name, str) or not isinstance(arch, str):
        raise ValueError("image config has no platform")
    variant = config.get("variant")
    return f"{os_name}/{arch}" + (f"/{variant}" if isinstance(variant, str) else "")


def _diff_ids(config: Mapping[str, Any]) -> list[str]:
    rootfs = config.get("rootfs")
    diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
    if not isinstance(diff_ids, list) or len(diff_ids) > _MAX_LAYERS:
        raise ValueError("image config does not describe its layers")
    return [validate_digest(item) for item in diff_ids]


def _blob_member(members: Mapping[str, _Member], digest: str) -> tuple[str, _Member]:
    validate_digest(digest)
    name = "blobs/sha256/" + digest[7:]
    member = members.get(name)
    if member is None:
        raise LookupError("blob absent from archive")
    if member.digest != digest:
        raise ValueError("archive blob does not match its name")
    return name, member


def _select_manifest(
    descriptor: Mapping[str, Any],
    fetch: Callable[[str], bytes],
    depth: int = 0,
) -> list[tuple[str, bytes, str]]:
    """Runnable platform manifests (digest, bytes, media type) under a descriptor."""
    if depth > 2:
        raise ValueError("image index nesting too deep")
    digest = validate_digest(descriptor.get("digest"))  # type: ignore[arg-type]
    body = fetch(digest)
    document = _json(body)
    media = descriptor.get("mediaType") or document.get("mediaType")
    if media in INDEX_TYPES or (media is None and "manifests" in document):
        children = document.get("manifests")
        if not isinstance(children, list) or len(children) > 64:
            raise ValueError("invalid image index")
        found: list[tuple[str, bytes, str]] = []
        for child in children:
            if not isinstance(child, dict):
                raise ValueError("invalid image index")
            platform = child.get("platform")
            if isinstance(platform, dict) and "unknown" in (
                platform.get("os"),
                platform.get("architecture"),
            ):
                continue
            try:
                found.extend(_select_manifest(child, fetch, depth + 1))
            except LookupError:
                continue
        return found
    if media is None and "config" in document:
        media = OCI_MANIFEST
    if media not in MANIFEST_TYPES:
        raise ValueError("unsupported image media type")
    return [(digest, body, str(media))]


def _plan_oci(
    metadata: Mapping[str, bytes],
    members: Mapping[str, _Member],
    stream_sha: str,
    total: int,
) -> PushPlan:
    if _json(metadata.get("oci-layout")).get("imageLayoutVersion") != "1.0.0":
        raise ValueError("unsupported OCI layout")
    index = _json(metadata.get("index.json"))
    manifests = index.get("manifests")
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise ValueError("expected exactly one image in the OCI archive")

    def fetch(digest: str) -> bytes:
        _, member = _blob_member(members, digest)
        if member.body is None:
            raise ValueError("image metadata blob too large")
        return member.body

    candidates = _select_manifest(manifests[0], fetch)
    if len(candidates) != 1:
        raise ValueError("expected exactly one runnable platform image")
    manifest_digest, manifest_bytes, media = candidates[0]
    manifest = _json(manifest_bytes)
    if manifest.get("schemaVersion") != 2:
        raise ValueError("invalid image manifest")
    if manifest.get("mediaType") not in (None, media):
        raise ValueError("manifest media type differs from its descriptor")
    config_desc = manifest.get("config")
    layer_descs = manifest.get("layers")
    if not isinstance(config_desc, dict) or not isinstance(layer_descs, list):
        raise ValueError("invalid image manifest")
    if len(layer_descs) > _MAX_LAYERS:
        raise ValueError("too many image layers")

    def blob(desc: Any, allowed: Mapping[str, Any] | set[str]) -> BlobRef:
        if not isinstance(desc, dict) or "urls" in desc:
            raise ValueError("unsupported (foreign or invalid) descriptor")
        media_type = desc.get("mediaType")
        if media_type not in allowed:
            raise ValueError("unsupported blob media type")
        try:
            name, member = _blob_member(members, desc.get("digest"))  # type: ignore[arg-type]
        except LookupError as error:
            raise ValueError("image blob absent from archive") from error
        if desc.get("size") != member.size:
            raise ValueError("blob size differs from its descriptor")
        return BlobRef(member.digest, member.size, str(media_type), name)

    config = blob(config_desc, CONFIG_TYPES)
    config_body = members[config.member].body
    if config_body is None:
        raise ValueError("image config too large")
    config_json = _json(config_body)
    layers = tuple(blob(desc, LAYER_TYPES) for desc in layer_descs)
    computed = []
    for layer in layers:
        member = members[layer.member]
        expected = LAYER_TYPES[layer.media_type]
        if (expected == "gzip") != member.gzip:
            raise ValueError("layer compression differs from its media type")
        computed.append(member.diff_id)
    if computed != _diff_ids(config_json):
        raise ValueError("layer DiffIDs do not match the image config")
    annotations = manifests[0].get("annotations") or {}
    name = annotations.get("io.containerd.image.name")
    return PushPlan(
        "oci",
        manifest_bytes,
        media,
        manifest_digest,
        "archive",
        config,
        config_body,
        layers,
        _platform(config_json),
        (name,) if isinstance(name, str) else (),
        stream_sha,
        total,
    )


def _plan_docker(
    metadata: Mapping[str, bytes],
    members: Mapping[str, _Member],
    stream_sha: str,
    total: int,
) -> PushPlan:
    entries = json.loads(metadata["manifest.json"])
    if not isinstance(entries, list) or len(entries) != 1:
        raise ValueError("expected exactly one image in the Docker archive")
    entry = entries[0]
    config_name = entry.get("Config") if isinstance(entry, dict) else None
    layer_names = entry.get("Layers") if isinstance(entry, dict) else None
    if not isinstance(config_name, str) or not isinstance(layer_names, list):
        raise ValueError("invalid Docker archive manifest")
    if len(layer_names) > _MAX_LAYERS:
        raise ValueError("too many image layers")
    config_member = members.get(member_name(config_name))
    if config_member is None or config_member.body is None:
        raise ValueError("image config absent from archive")
    digest = config_member.digest
    if member_name(config_name) not in {
        digest[7:] + ".json",
        "blobs/sha256/" + digest[7:],
    }:
        raise ValueError("image config name does not match its content")
    config_json = _json(config_member.body)
    diff_ids = _diff_ids(config_json)
    if len(diff_ids) != len(layer_names):
        raise ValueError("image config does not describe the archive layers")
    layers: list[BlobRef] = []
    for layer_name, diff_id in zip(layer_names, diff_ids, strict=True):
        if not isinstance(layer_name, str):
            raise ValueError("invalid Docker archive manifest")
        name = member_name(layer_name)
        member = members.get(name)
        if member is None:
            raise ValueError("image layer absent from archive")
        if member.diff_id != diff_id:
            raise ValueError("layer DiffIDs do not match the image config")
        media = OCI_LAYER_GZIP if member.gzip else OCI_LAYER
        layers.append(BlobRef(member.digest, member.size, media, name))
    config = BlobRef(digest, config_member.size, OCI_CONFIG, member_name(config_name))
    manifest = canonical(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": OCI_CONFIG,
                "digest": config.digest,
                "size": config.size,
            },
            "layers": [
                {
                    "mediaType": layer.media_type,
                    "digest": layer.digest,
                    "size": layer.size,
                }
                for layer in layers
            ],
        }
    )
    tags = entry.get("RepoTags") or []
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("invalid Docker archive tags")
    return PushPlan(
        "docker",
        manifest,
        OCI_MANIFEST,
        "sha256:" + hashlib.sha256(manifest).hexdigest(),
        "synthesized",
        config,
        config_member.body,
        tuple(layers),
        _platform(config_json),
        tuple(tags),
        stream_sha,
        total,
    )


def manifest_config_digest(body: bytes) -> str:
    """The config digest a single-platform manifest names."""
    document = _json(body)
    config = document.get("config")
    if not isinstance(config, dict):
        raise ValueError("not an image manifest")
    return validate_digest(config.get("digest"))  # type: ignore[arg-type]
