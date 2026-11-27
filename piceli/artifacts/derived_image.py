"""Derived images (0.89.0): a built image plus one layer of files, without a build.

A derived image is an image the test environment already ran (its *base*,
pinned by digest) with a few small files added, such as a release number.
Nothing is compiled and nothing of the base changes, as ``crane append``
does:

* :func:`write_layer_of` writes the files as one deterministic gzip layer
  (the writer of :mod:`piceli.artifacts.oci_layers`: sorted entries, a fixed
  ``mtime``, root owner, modes ``0644``/``0755``, a gzip header without name
  or time). The same files give the same layer digest, so one layer blob is
  shared by every image derived with them.
* :func:`append` reads the base manifest from a registry, keeps its layers
  and config, appends the layer (the config's ``rootfs.diff_ids`` and
  ``history`` get one entry each), and pushes the new config and manifest by
  digest. A base that is an OCI image index gets one derived manifest per
  platform and a new index over them.

The base layers are never downloaded when the registry can mount them into
the derived image's repository (the OCI Distribution cross-repository
mount); otherwise they are streamed from the base's repository.

Importing this module runs nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from piceli.artifacts.image_manifest import (
    CONFIG_TYPES,
    DOCKER_LAYER_GZIP,
    DOCKER_MANIFEST,
    INDEX_TYPES,
    LAYER_TYPES,
    MANIFEST_TYPES,
    OCI_LAYER_GZIP,
)
from piceli.artifacts.oci_layers import (
    BlobStore,
    Descriptor,
    LayerError,
    WrittenLayer,
    collect,
    write_layer,
)
from piceli.artifacts.plan import canonical, digest, validate_digest

#: The most a derived image may add (it is meant for small files).
MAX_DERIVED_BYTES = 64 * 1024 * 1024
MAX_CONFIG_BYTES = 4 * 1024 * 1024
HISTORY_COMMENT = "piceli derived image"


@dataclass(frozen=True)
class DerivedFile:
    """One file a derived image adds: its image path, bytes and mode."""

    path: str
    data: bytes = field(repr=False)
    executable: bool = False

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.data).hexdigest()


def write_layer_of(
    store: BlobStore, files: Sequence[DerivedFile], *, epoch: int
) -> WrittenLayer:
    """The one layer of ``files`` (plus their parent directories), in ``store``.

    :raises LayerError: ``host-build-invalid`` for no file, two files at one
        path or more than :data:`MAX_DERIVED_BYTES`.
    """
    if not files:
        raise LayerError("host-build-invalid", "a derived image adds no file")
    if sum(len(item.data) for item in files) > MAX_DERIVED_BYTES:
        raise LayerError(
            "host-build-invalid",
            f"a derived image adds more than {MAX_DERIVED_BYTES} bytes",
        )
    with tempfile.TemporaryDirectory(prefix="piceli-derive-") as directory:
        entries = []
        ordered = sorted(files, key=lambda item: item.path)
        for index, item in enumerate(ordered):
            source = Path(directory) / str(index)
            source.write_bytes(item.data)
            os.chmod(source, 0o755 if item.executable else 0o644)
            entries.extend(collect(source, item.path))
        return write_layer(store, entries, mtime=epoch)


@dataclass(frozen=True)
class Derived:
    """The pushed derived image: its manifest (or index) and each platform's."""

    digest: str
    media_type: str
    size: int
    platforms: Mapping[str, str]
    layer: Descriptor
    blobs: Mapping[str, str] = field(default_factory=dict)
    """How each base blob reached the repository: ``present``, ``mounted``
    or ``copied``."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "media_type": self.media_type,
            "size": self.size,
            "platforms": dict(sorted(self.platforms.items())),
            "layer": {
                "digest": self.layer.digest,
                "diff_id": self.layer.diff_id,
                "size": self.layer.size,
            },
            "blobs": dict(sorted(self.blobs.items())),
        }


def _created(epoch: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Append:
    def __init__(
        self,
        client: Any,
        source: str,
        repository: str,
        store: BlobStore,
        layer: Descriptor,
        created_by: str,
        created: str,
        labels: Mapping[str, str],
    ) -> None:
        self.client = client
        self.source = source
        self.repository = repository
        self.store = store
        self.layer = layer
        self.created_by = created_by
        self.created = created
        self.labels = dict(labels)
        self.blobs: dict[str, str] = {}

    def manifest(self, reference: str) -> tuple[dict[str, Any], str]:
        from piceli.artifacts.registry import RegistryError

        try:
            body, media = self.client.get_manifest(self.source, reference)
        except RegistryError as error:
            raise LayerError(
                "derived-base-unavailable",
                f"the base {self.source}@{reference[:19]} cannot be read "
                f"({error.reason})",
            ) from None
        if digest(body) != reference:
            raise LayerError(
                "derived-base-invalid", "the registry served another base manifest"
            )
        document = json.loads(body)
        if not isinstance(document, dict):
            raise LayerError("derived-base-invalid", "invalid base manifest")
        kind = str(document.get("mediaType") or media.split(";")[0].strip())
        return document, kind

    def blob(self, item: Mapping[str, Any]) -> None:
        """Make the base blob ``item`` present in the derived repository."""
        value = validate_digest(str(item.get("digest")))
        if value in self.blobs:
            return
        if self.client.has_blob(self.repository, value):
            self.blobs[value] = "present"
            return
        mount = getattr(self.client, "mount_blob", None)
        if mount is not None and mount(self.repository, value, self.source):
            self.blobs[value] = "mounted"
            return
        with self.client.open_blob(self.source, value) as stream:
            self.client.push_blob(self.repository, value, int(item["size"]), stream)
        self.blobs[value] = "copied"

    def config(self, item: Mapping[str, Any]) -> dict[str, Any]:
        value = validate_digest(str(item.get("digest")))
        size = item.get("size")
        if not isinstance(size, int) or not 0 < size <= MAX_CONFIG_BYTES:
            raise LayerError("derived-base-invalid", "the base config is too large")
        body = b""
        with self.client.open_blob(self.source, value) as stream:
            while len(body) <= size and (block := stream.read(size + 1 - len(body))):
                body += block
        if len(body) != size or digest(body) != value:
            raise LayerError("derived-base-invalid", "the base config does not match")
        found = json.loads(body)
        if not isinstance(found, dict):
            raise LayerError("derived-base-invalid", "invalid base config")
        return found

    def push(self, body: bytes, media: str) -> str:
        value = digest(body)
        if self.client.manifest_digest(self.repository, value) != value:
            self.client.push_manifest(self.repository, value, body, media)
        return value

    def image(self, reference: str) -> tuple[str, int, str, str]:
        """Derive one image manifest: ``(digest, size, media type, platform)``."""
        document, media = self.manifest(reference)
        if media not in MANIFEST_TYPES:
            raise LayerError("derived-base-invalid", f"unsupported base type {media}")
        config_item = document.get("config")
        layers = document.get("layers")
        if (
            not isinstance(config_item, dict)
            or config_item.get("mediaType") not in CONFIG_TYPES
            or not isinstance(layers, list)
        ):
            raise LayerError("derived-base-invalid", "invalid base manifest")
        for item in layers:
            if (
                not isinstance(item, dict)
                or item.get("mediaType") not in LAYER_TYPES
                or "urls" in item
            ):
                raise LayerError(
                    "base-layer-unsupported",
                    "the base image uses a layer type a derived image cannot "
                    "keep (only tar and tar+gzip layers)",
                )
        config = self.config(config_item)
        rootfs = config.get("rootfs")
        diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
        if not isinstance(diff_ids, list) or len(diff_ids) != len(layers):
            raise LayerError(
                "derived-base-invalid", "base layers differ from its config"
            )
        config["rootfs"] = {**rootfs, "diff_ids": [*diff_ids, self.layer.diff_id]}  # type: ignore[dict-item]
        config["history"] = [
            *(config.get("history") or []),
            {
                "created": self.created,
                "created_by": self.created_by,
                "comment": HISTORY_COMMENT,
            },
        ]
        if self.labels:
            runtime = config.get("config")
            runtime = dict(runtime) if isinstance(runtime, dict) else {}
            runtime["Labels"] = {**(runtime.get("Labels") or {}), **self.labels}
            config["config"] = runtime
        for item in layers:
            self.blob(item)
        if not self.client.has_blob(self.repository, self.layer.digest):
            with self.store.path(self.layer.digest).open("rb") as stream:
                self.client.push_blob(
                    self.repository, self.layer.digest, self.layer.size, stream
                )
        config_body = canonical(config)
        config_digest = digest(config_body)
        if not self.client.has_blob(self.repository, config_digest):
            import io

            self.client.push_blob(
                self.repository,
                config_digest,
                len(config_body),
                io.BytesIO(config_body),
            )
        layer_type = DOCKER_LAYER_GZIP if media == DOCKER_MANIFEST else OCI_LAYER_GZIP
        manifest = {key: value for key, value in document.items() if key != "subject"}
        manifest["config"] = {
            **config_item,
            "digest": config_digest,
            "size": len(config_body),
        }
        manifest["layers"] = [
            *layers,
            {
                "mediaType": layer_type,
                "digest": self.layer.digest,
                "size": self.layer.size,
            },
        ]
        body = canonical(manifest)
        platform = f"{config.get('os')}/{config.get('architecture')}"
        return self.push(body, media), len(body), media, platform


def append(
    client: Any,
    *,
    source_repository: str,
    base_digest: str,
    repository: str,
    store: BlobStore,
    layer: Descriptor,
    epoch: int = 0,
    created_by: str = "piceli derive",
    labels: Mapping[str, str] | None = None,
) -> Derived:
    """Push ``source_repository@base_digest`` plus ``layer`` (in ``store``)
    to ``repository``; return the derived manifest or index.

    ``client`` speaks OCI Distribution
    (:class:`~piceli.artifacts.registry.StreamedOciRegistryClient`): both
    repositories are on its registry. The base is read by digest only.

    :raises LayerError: ``derived-base-unavailable``, ``derived-base-invalid``,
        ``base-layer-unsupported``.
    """
    validate_digest(base_digest)
    if layer.diff_id is None:
        raise LayerError("host-build-invalid", "a derived layer needs its diff_id")
    work = _Append(
        client,
        source_repository,
        repository,
        store,
        layer,
        created_by,
        _created(epoch),
        labels or {},
    )
    client.authenticate(repository)
    document, media = work.manifest(base_digest)
    if media not in INDEX_TYPES and "manifests" not in document:
        found, size, kind, platform = work.image(base_digest)
        return Derived(found, kind, size, {platform: found}, layer, work.blobs)
    entries: list[dict[str, Any]] = []
    platforms: dict[str, str] = {}
    for entry in document.get("manifests") or []:
        if not isinstance(entry, dict):
            continue
        platform = entry.get("platform")
        if (
            entry.get("mediaType") not in MANIFEST_TYPES
            or not isinstance(platform, dict)
            or platform.get("os") in (None, "unknown")
        ):
            # An attestation manifest (``unknown/unknown``) describes the
            # base, not the derived image: it is left out.
            continue
        found, size, kind, _ = work.image(validate_digest(str(entry.get("digest"))))
        entries.append({**entry, "mediaType": kind, "digest": found, "size": size})
        name = f"{platform.get('os')}/{platform.get('architecture')}"
        platforms[name] = found
    if not entries:
        raise LayerError("derived-base-invalid", "the base index has no image")
    index = {key: value for key, value in document.items() if key != "subject"}
    index["manifests"] = entries
    body = canonical(index)
    return Derived(
        work.push(body, media), media, len(body), platforms, layer, work.blobs
    )
