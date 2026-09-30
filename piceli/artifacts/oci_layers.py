"""Deterministic OCI images assembled on the host, without a container engine.

A host build (:mod:`piceli.artifacts.host_build`) produces files on the
host; this module turns them into an image:

* :class:`BlobStore` is a content-addressed directory of blobs (base layers
  pulled by digest, layers written here), shared by every build that uses the
  same cache directory, so an unchanged layer is written and pulled once.
* :func:`write_layer` writes one gzip layer from files and directories. Entry
  order, times (``mtime``), owners and modes are fixed, and the gzip header
  carries no name or time: the same files always give the same layer digest.
* :func:`pull_base` fetches a base image by its pinned digest (index or
  manifest) over OCI Distribution and keeps the platform's config and layers.
* :func:`assemble` writes the image config and manifest; :func:`write_archive`
  writes an OCI image-layout tar that delivery reads
  (:class:`~piceli.artifacts.delivery.ArchiveSource`).

Each declared file mapping of an image is its own layer, so a change that
touches one output file changes one layer digest; the registry already has
the others and delivery skips them.

Importing this module runs nothing.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import stat
import tarfile
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from piceli.artifacts.image_manifest import (
    CONFIG_TYPES,
    INDEX_TYPES,
    LAYER_TYPES,
    MANIFEST_TYPES,
    OCI_CONFIG,
    OCI_LAYER_GZIP,
    OCI_MANIFEST,
)
from piceli.artifacts.plan import canonical, digest, relative, validate_digest

_BLOCK = 1024 * 1024
MAX_LAYER_ENTRIES = 65_536
MAX_METADATA_BYTES = 4 * 1024 * 1024


class LayerError(ValueError):
    """A layer or base image was refused. ``code`` is a registered code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Descriptor:
    """An OCI descriptor plus, for a layer, its uncompressed digest."""

    media_type: str
    digest: str
    size: int
    diff_id: str | None = None

    def to_oci(self) -> dict[str, Any]:
        return {"mediaType": self.media_type, "digest": self.digest, "size": self.size}


class BlobStore:
    """``<root>/blobs/sha256/<hex>``: blobs written once, by content."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.blobs = root / "blobs" / "sha256"

    def path(self, value: str) -> Path:
        validate_digest(value)
        return self.blobs / value[7:]

    def has(self, value: str, size: int | None = None) -> bool:
        path = self.path(value)
        return path.is_file() and (size is None or path.stat().st_size == size)

    def temporary(self) -> Path:
        self.blobs.mkdir(parents=True, exist_ok=True)
        return (
            self.blobs
            / f".tmp-{os.getpid()}-{threading.get_ident()}-{os.urandom(6).hex()}"
        )

    def commit(self, temporary: Path, value: str) -> Path:
        path = self.path(value)
        os.replace(temporary, path)
        return path

    def put(self, body: bytes) -> str:
        value = digest(body)
        if not self.has(value, len(body)):
            temporary = self.temporary()
            temporary.write_bytes(body)
            self.commit(temporary, value)
        return value

    def read(self, value: str, maximum: int = MAX_METADATA_BYTES) -> bytes:
        path = self.path(value)
        if path.stat().st_size > maximum:
            raise LayerError("base-image-invalid", "image metadata blob too large")
        body = path.read_bytes()
        if digest(body) != value:
            raise LayerError("base-image-invalid", "stored blob does not match")
        return body


class _Hashing(io.RawIOBase):
    """A write-through sink that hashes and counts what passes."""

    def __init__(self, sink: BinaryIO | io.RawIOBase | gzip.GzipFile) -> None:
        self.sink = sink
        self.hash = hashlib.sha256()
        self.size = 0

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        view = memoryview(data).cast("B")
        self.sink.write(view)
        self.hash.update(view)
        self.size += len(view)
        return len(view)

    @property
    def digest(self) -> str:
        return "sha256:" + self.hash.hexdigest()


class _HashingReader(io.RawIOBase):
    def __init__(self, source: BinaryIO) -> None:
        self.source = source
        self.hash = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        data = self.source.read(len(buffer))
        buffer[: len(data)] = data
        self.hash.update(data)
        return len(data)


@dataclass(frozen=True)
class LayerEntry:
    """One member of a layer: a file (``source``), a directory or a symlink."""

    path: str
    kind: str
    mode: int
    source: Path | None = field(default=None, compare=False)
    link: str = ""
    uid: int = 0
    gid: int = 0

    def __post_init__(self) -> None:
        relative(self.path)
        if self.kind not in {"file", "dir", "symlink"}:
            raise LayerError("host-build-invalid", "unknown layer entry kind")


def collect(
    source: Path, destination: str, *, max_entries: int = MAX_LAYER_ENTRIES
) -> list[LayerEntry]:
    """Entries that put ``source`` (a file or a directory tree) at ``destination``.

    Regular files keep only their executable bit (``0755`` or ``0644``);
    directories are ``0755``; relative symlinks inside a directory tree are
    kept, absolute or escaping ones are refused. Parent directories of
    ``destination`` are added as ``0755`` root directories.
    """
    target = relative(destination.lstrip("/"))
    info = source.lstat()
    entries: list[LayerEntry] = [
        LayerEntry(str(parent), "dir", 0o755)
        for parent in reversed(PurePosixPath(target).parents)
        if str(parent) != "."
    ]
    if stat.S_ISREG(info.st_mode):
        entries.append(LayerEntry(target, "file", _file_mode(info.st_mode), source))
        return entries
    if not stat.S_ISDIR(info.st_mode):
        raise LayerError("host-output-missing", "an image file is not a file or tree")
    entries.append(LayerEntry(target, "dir", 0o755))
    for current, directories, files in os.walk(source):
        directories.sort()
        here = Path(current)
        for name in sorted([*directories, *files]):
            path = here / name
            inner = f"{target}/{path.relative_to(source).as_posix()}"
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                link = os.readlink(path)
                resolved = os.path.normpath(os.path.join(os.path.dirname(inner), link))
                if link.startswith("/") or resolved.startswith(".."):
                    raise LayerError(
                        "host-output-missing", "a symlink leaves its image directory"
                    )
                entries.append(LayerEntry(inner, "symlink", 0o777, link=link))
            elif stat.S_ISDIR(mode):
                entries.append(LayerEntry(inner, "dir", 0o755))
            elif stat.S_ISREG(mode):
                entries.append(LayerEntry(inner, "file", _file_mode(mode), path))
            else:
                raise LayerError("host-output-missing", "unsupported file type")
            if len(entries) > max_entries:
                raise LayerError("host-output-missing", "too many files in one layer")
        directories[:] = [
            item for item in directories if not (here / item).is_symlink()
        ]
    return entries


def _file_mode(mode: int) -> int:
    return 0o755 if mode & 0o111 else 0o644


@dataclass(frozen=True)
class WrittenLayer:
    descriptor: Descriptor
    files: tuple[tuple[str, str, int], ...]
    """``(image path, sha256, size)`` of every regular file in the layer."""


def write_layer(
    store: BlobStore,
    entries: Sequence[LayerEntry],
    *,
    mtime: int,
    cancel: threading.Event | None = None,
) -> WrittenLayer:
    """Write one deterministic gzip layer into ``store``."""
    unique: dict[str, LayerEntry] = {}
    for entry in entries:
        if entry.path in unique and unique[entry.path] != entry:
            raise LayerError("host-build-invalid", "two entries for one image path")
        unique[entry.path] = entry
    ordered = [unique[key] for key in sorted(unique)]
    files: list[tuple[str, str, int]] = []
    temporary = store.temporary()
    try:
        with open(temporary, "xb") as raw:
            compressed = _Hashing(raw)
            with gzip.GzipFile(
                filename="", mode="wb", compresslevel=6, fileobj=compressed, mtime=0
            ) as zipped:
                plain = _Hashing(zipped)
                with tarfile.open(  # type: ignore[call-overload]
                    fileobj=plain, mode="w|", format=tarfile.PAX_FORMAT
                ) as archive:
                    for entry in ordered:
                        if cancel is not None and cancel.is_set():
                            raise InterruptedError("host build cancelled")
                        files.extend(_add(archive, entry, mtime))
            diff_id, layer_digest, size = (
                plain.digest,
                compressed.digest,
                compressed.size,
            )
        store.commit(temporary, layer_digest)
    finally:
        if temporary.exists():
            temporary.unlink()
    return WrittenLayer(
        Descriptor(OCI_LAYER_GZIP, layer_digest, size, diff_id), tuple(files)
    )


def _add(
    archive: tarfile.TarFile, entry: LayerEntry, mtime: int
) -> list[tuple[str, str, int]]:
    info = tarfile.TarInfo(entry.path)
    info.mtime, info.mode = mtime, entry.mode
    info.uid, info.gid, info.uname, info.gname = entry.uid, entry.gid, "", ""
    if entry.kind == "dir":
        info.type = tarfile.DIRTYPE
        archive.addfile(info)
        return []
    if entry.kind == "symlink":
        info.type, info.linkname = tarfile.SYMTYPE, entry.link
        archive.addfile(info)
        return []
    assert entry.source is not None
    with open(entry.source, "rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        info.size = size
        reader = _HashingReader(stream)
        archive.addfile(info, io.BufferedReader(reader, _BLOCK))
    return [(entry.path, "sha256:" + reader.hash.hexdigest(), size)]


# ------------------------------------------------------------------ base image


@dataclass(frozen=True)
class BaseImage:
    """A pulled base: its platform manifest, config and layers (all in the store)."""

    reference: str
    manifest_digest: str
    config: Mapping[str, Any]
    layers: tuple[Descriptor, ...]


def pull_base(
    reference: str,
    platform: str,
    store: BlobStore,
    *,
    client_factory: Callable[[str], Any] | None = None,
    timeout: float = 120.0,
    cancel: threading.Event | None = None,
) -> BaseImage:
    """Fetch the ``platform`` image of a digest-pinned ``reference`` into ``store``.

    Blobs already in the store (by digest and size) are not fetched again;
    every fetched blob is hashed and must match its digest.
    """
    from piceli.artifacts.mirror import MirrorError, MirrorSource
    from piceli.artifacts.registry import (
        RegistryError,
        RegistryTarget,
        StreamedOciRegistryClient,
    )

    try:
        source = MirrorSource.parse(reference)
    except MirrorError as error:
        raise LayerError("base-image-invalid", str(error)) from None

    def default(url: str) -> Any:
        return StreamedOciRegistryClient(
            RegistryTarget.parse(url).endpoint(timeout=timeout), actions="pull"
        )

    client = (client_factory or default)(source.url)
    try:
        client.authenticate(source.repository)
        return _pull(client, source, platform, store, cancel)
    except RegistryError as error:
        raise LayerError(
            "base-image-unavailable",
            f"the base image could not be read from its registry ({error.reason})",
        ) from None


def _pull(
    client: Any,
    source: Any,
    platform: str,
    store: BlobStore,
    cancel: threading.Event | None,
) -> BaseImage:
    repository = source.repository

    def manifest(value: str) -> dict[str, Any]:
        if store.has(value):
            body = store.read(value)
        else:
            body, _ = client.get_manifest(repository, value)
            if digest(body) != value:
                raise LayerError(
                    "base-image-invalid", "the registry served another manifest"
                )
            store.put(body)
        document = json.loads(body)
        if not isinstance(document, dict):
            raise LayerError("base-image-invalid", "invalid base manifest")
        return document

    document = manifest(source.digest)
    selected = source.digest
    if document.get("mediaType") in INDEX_TYPES or "manifests" in document:
        os_name, arch = platform.split("/", 1)
        matches = [
            item
            for item in document.get("manifests") or []
            if isinstance(item, dict)
            and isinstance(item.get("platform"), dict)
            and item["platform"].get("os") == os_name
            and item["platform"].get("architecture") == arch
        ]
        if not matches:
            raise LayerError(
                "base-platform-unavailable",
                f"the base image has no {platform} manifest",
            )
        selected = validate_digest(matches[0]["digest"])
        document = manifest(selected)
    if document.get("mediaType", OCI_MANIFEST) not in MANIFEST_TYPES:
        raise LayerError("base-image-invalid", "unsupported base manifest type")
    config_desc = _descriptor(document.get("config"), CONFIG_TYPES)
    layers = [_descriptor(item, LAYER_TYPES) for item in document.get("layers") or []]
    for item in (config_desc, *layers):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("host build cancelled")
        _fetch(client, repository, item, store)
    config = json.loads(store.read(config_desc.digest))
    if not isinstance(config, dict):
        raise LayerError("base-image-invalid", "invalid base config")
    if f"{config.get('os')}/{config.get('architecture')}" != platform:
        raise LayerError(
            "base-platform-unavailable", f"the base image is not {platform}"
        )
    diff_ids = (config.get("rootfs") or {}).get("diff_ids")
    if not isinstance(diff_ids, list) or len(diff_ids) != len(layers):
        raise LayerError("base-image-invalid", "base layers differ from its config")
    return BaseImage(
        source.key,
        selected,
        config,
        tuple(
            Descriptor(item.media_type, item.digest, item.size, validate_digest(diff))
            for item, diff in zip(layers, diff_ids, strict=True)
        ),
    )


def _descriptor(value: Any, allowed: Iterable[str]) -> Descriptor:
    if (
        not isinstance(value, dict)
        or value.get("mediaType") not in set(allowed)
        or not isinstance(value.get("size"), int)
        or "urls" in value
    ):
        raise LayerError(
            "base-layer-unsupported",
            "the base image uses a blob type a host build cannot use "
            "(only tar and tar+gzip layers)",
        )
    return Descriptor(
        str(value["mediaType"]), validate_digest(value["digest"]), value["size"]
    )


def _fetch(client: Any, repository: str, item: Descriptor, store: BlobStore) -> None:
    if store.has(item.digest, item.size):
        return
    temporary = store.temporary()
    try:
        hasher, size = hashlib.sha256(), 0
        with (
            client.open_blob(repository, item.digest) as stream,
            open(temporary, "xb") as sink,
        ):
            while block := stream.read(_BLOCK):
                size += len(block)
                if size > item.size:
                    break
                hasher.update(block)
                sink.write(block)
        if size != item.size or "sha256:" + hasher.hexdigest() != item.digest:
            raise LayerError("base-image-invalid", "a base blob does not match")
        store.commit(temporary, item.digest)
    finally:
        if temporary.exists():
            temporary.unlink()


# ------------------------------------------------------------------ image


def _created(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ImageSettings:
    """What an image sets over its base (``None`` keeps the base's value)."""

    entrypoint: tuple[str, ...] | None = None
    cmd: tuple[str, ...] | None = None
    user: str | None = None
    workdir: str | None = None
    env: tuple[tuple[str, str], ...] = ()
    labels: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Image:
    manifest: Descriptor
    config: Descriptor
    layers: tuple[Descriptor, ...]
    platform: str


def assemble(
    store: BlobStore,
    base: BaseImage | None,
    layers: Sequence[tuple[Descriptor, str]],
    settings: ImageSettings,
    platform: str,
    *,
    epoch: int,
) -> Image:
    """Write the config and manifest of ``base`` plus ``layers`` into ``store``.

    ``layers`` are ``(descriptor, history text)`` pairs. Setting an
    entrypoint without ``cmd`` clears the base's ``Cmd``, as a Dockerfile
    ``ENTRYPOINT`` does.
    """
    os_name, arch = platform.split("/", 1)
    config: dict[str, Any] = (
        json.loads(json.dumps(dict(base.config)))
        if base is not None
        else {"architecture": arch, "os": os_name, "config": {}}
    )
    for key in ("container", "container_config", "docker_version", "id", "parent"):
        config.pop(key, None)
    runtime = config.get("config")
    if not isinstance(runtime, dict):
        runtime = {}
    if settings.entrypoint is not None:
        runtime["Entrypoint"] = list(settings.entrypoint)
        runtime["Cmd"] = list(settings.cmd) if settings.cmd is not None else None
    elif settings.cmd is not None:
        runtime["Cmd"] = list(settings.cmd)
    if settings.user is not None:
        runtime["User"] = settings.user
    if settings.workdir is not None:
        runtime["WorkingDir"] = settings.workdir
    if settings.env:
        env = [
            item
            for item in runtime.get("Env") or []
            if isinstance(item, str)
            and item.split("=", 1)[0] not in {key for key, _ in settings.env}
        ]
        runtime["Env"] = env + [f"{key}={value}" for key, value in settings.env]
    if settings.labels:
        labels = runtime.get("Labels") or {}
        runtime["Labels"] = {**labels, **dict(settings.labels)}
    config["config"] = runtime
    created = _created(epoch)
    config["created"] = created
    base_layers = base.layers if base is not None else ()
    history = list(config.get("history") or []) if base is not None else []
    history += [
        {"created": created, "created_by": text, "comment": "piceli host build"}
        for _, text in layers
    ]
    config["history"] = history
    all_layers = (*base_layers, *(item for item, _ in layers))
    config["rootfs"] = {
        "type": "layers",
        "diff_ids": [item.diff_id for item in all_layers],
    }
    config_body = canonical(config)
    config_desc = Descriptor(OCI_CONFIG, store.put(config_body), len(config_body))
    manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "config": config_desc.to_oci(),
        "layers": [item.to_oci() for item in all_layers],
    }
    manifest_body = canonical(manifest)
    manifest_desc = Descriptor(
        OCI_MANIFEST, store.put(manifest_body), len(manifest_body)
    )
    return Image(manifest_desc, config_desc, tuple(all_layers), platform)


def write_archive(store: BlobStore, image: Image, output: Path) -> Path:
    """An OCI image-layout tar of ``image`` at ``output`` (fixed order and times)."""
    os_name, arch = image.platform.split("/", 1)
    index = canonical(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                image.manifest.to_oci()
                | {"platform": {"architecture": arch, "os": os_name}}
            ],
        }
    )
    blobs = sorted(
        {item.digest for item in (image.manifest, image.config, *image.layers)}
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.urandom(6).hex()}")
    try:
        with tarfile.open(temporary, "x", format=tarfile.PAX_FORMAT) as archive:
            for name, body in (
                ("oci-layout", canonical({"imageLayoutVersion": "1.0.0"})),
                ("index.json", index),
            ):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(body), 0o644, 0
                archive.addfile(info, io.BytesIO(body))
            for directory in ("blobs", "blobs/sha256"):
                info = tarfile.TarInfo(directory)
                info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, 0
                archive.addfile(info)
            for value in blobs:
                path = store.path(value)
                info = tarfile.TarInfo(f"blobs/sha256/{value[7:]}")
                info.size, info.mode, info.mtime = path.stat().st_size, 0o644, 0
                with open(path, "rb") as stream:
                    archive.addfile(info, stream)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return output
