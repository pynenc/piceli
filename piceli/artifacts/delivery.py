"""Direct, digest-approved image delivery into a node's containerd, without a registry.

This is the fallback mode (bootstrap and air-gapped nodes). Registry delivery,
the default, lives in :mod:`piceli.artifacts.registry_delivery`.

The approved identity is the image **config digest**: the SHA-256 of the image
configuration blob. It is the only identifier that is byte-for-byte the same in
``docker save`` output (classic Docker's image ID), in the archive itself and in
containerd after ``ctr images import`` (the CRI image ID). Manifest digests are
not comparable: a Docker archive has no manifest or a locally written one, and
``ctr`` synthesizes its own manifest on import.

Delivery streams a Docker/OCI image tar through a validating relay into the
node runtime's ``images import -``. The relay forwards layer bytes as they
arrive but withholds the archive's index members (``manifest.json``,
``index.json``, ``oci-layout``) until the whole stream has been read and the
config digest recomputed from the streamed bytes. On a mismatch the index is
never sent, so the runtime rejects the stream and creates no image; the few
unreferenced content blobs it may have ingested are garbage-collected by
containerd.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tarfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from piceli.artifacts.delivery_inputs import DeliveryInputError, verify_tools
from piceli.artifacts.node_transport import (
    NodeTarget,
    Runner,
    RunResult,
    SubprocessRunner,
    Transport,
)
from piceli.artifacts.plan import validate_digest
from piceli.artifacts.process import ProcessLimits, ToolPin
from piceli.artifacts.registry import RegistryTarget
from piceli.bounds import seconds, text

SCHEMA = "piceli.node-delivery.v1"
IMAGE_NAME = "io.containerd.image.name"
REF_NAME = "org.opencontainers.image.ref.name"
INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
MANIFEST_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
_ENTRY_MEMBERS = ("oci-layout", "index.json", "manifest.json")
_METADATA_MEMBERS = {*_ENTRY_MEMBERS, "repositories"}
_SMALL = 4 * 1024 * 1024
_KEPT_TOTAL = 64 * 1024 * 1024
_MAX_MEMBERS = 65_536
_QUERY = ProcessLimits(60, _SMALL)
_ID = re.compile(r"(?:sha256:)?[a-f0-9]{12,64}")
_DOMAIN = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.?)+(?::\d{1,5})?")
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_PATH = re.compile(rf"{_COMPONENT}(?:/{_COMPONENT})*")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


class DeliveryRejected(ValueError):
    """The streamed or present image does not have the approved config digest."""


def normalize_reference(value: str) -> str:
    """Normalize a tagged name the way containerd names an imported image.

    ``app:1`` becomes ``docker.io/library/app:1``; a missing tag becomes
    ``latest``. Digest references are rejected: import names are tags.
    """
    text(value, "image reference")
    if "@" in value or value.startswith("-") or len(value) > 255:
        raise ValueError("image reference must be a tagged name")
    name, tag = value, "latest"
    if ":" in value.rsplit("/", 1)[-1]:
        name, tag = value.rsplit(":", 1)
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        domain, path = first, rest
    else:
        domain, path = "docker.io", name
    if domain == "docker.io" and "/" not in path:
        path = "library/" + path
    if not (
        _DOMAIN.fullmatch(domain) and _PATH.fullmatch(path) and _TAG.fullmatch(tag)
    ):
        raise ValueError("invalid image reference")
    return f"{domain}/{path}:{tag}"


@dataclass(frozen=True)
class DeliveryGrant:
    """Exact, expiring approval of one config digest for one delivery target.

    ``target`` is a node target (``ssh://``, ``docker://``) or a registry
    target (``oci://``).
    """

    approved_digest: str
    target: str
    expires_at: float

    def __post_init__(self) -> None:
        validate_digest(self.approved_digest)
        if isinstance(self.target, str) and self.target.startswith("oci://"):
            RegistryTarget.parse(self.target)
        else:
            NodeTarget.parse(self.target)
        seconds(self.expires_at, "grant expiry", 10_000_000_000)


@dataclass(frozen=True)
class DockerImageSource:
    """An image already present in a local Docker daemon, by name or image ID."""

    reference: str

    def __post_init__(self) -> None:
        text(self.reference, "image reference")
        if self.reference.startswith("-") or len(self.reference) > 255:
            raise ValueError("invalid image reference")

    @property
    def is_id(self) -> bool:
        return bool(_ID.fullmatch(self.reference))


@dataclass(frozen=True)
class ArchiveSource:
    """A ``docker save`` or OCI image-layout tar on local disk."""

    path: Path = field(repr=False)
    max_bytes: int = 16 * 1024**3

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("archive path must be explicit and absolute")


@dataclass(frozen=True)
class StreamIdentity:
    """What a validating pass over an image tar proved about it."""

    format: str
    config_digest: str
    names: tuple[str, ...]
    stream_sha256: str
    bytes: int


# --- content resolution shared by archives and nodes ---------------------------


def _json_object(body: bytes) -> dict[str, Any]:
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def resolve_configs(
    descriptor: Mapping[str, Any], fetch: Callable[[str], bytes], depth: int = 0
) -> list[str]:
    """Config digests reachable from a manifest or (nested) index descriptor.

    ``fetch`` must return bytes whose SHA-256 equals the digest, or raise
    ``LookupError`` for content that is absent (other platforms on a node).
    Attestation manifests (platform ``unknown``) are ignored.
    """
    if depth > 2:
        raise ValueError("image index nesting too deep")
    digest = validate_digest(descriptor.get("digest"))  # type: ignore[arg-type]
    body = fetch(digest)
    if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
        raise ValueError("content does not match its digest")
    document = _json_object(body)
    media = descriptor.get("mediaType") or document.get("mediaType")
    if media in INDEX_TYPES or (media is None and "manifests" in document):
        children = document.get("manifests")
        if not isinstance(children, list) or len(children) > 64:
            raise ValueError("invalid image index")
        found: list[str] = []
        for child in children:
            platform = child.get("platform") if isinstance(child, dict) else None
            if isinstance(platform, dict) and "unknown" in (
                platform.get("os"),
                platform.get("architecture"),
            ):
                continue
            try:
                found.extend(resolve_configs(child, fetch, depth + 1))
            except LookupError:
                continue
        return found
    if media in MANIFEST_TYPES or (media is None and "config" in document):
        config = document.get("config")
        if not isinstance(config, dict):
            raise ValueError("invalid image manifest")
        return [validate_digest(config.get("digest"))]  # type: ignore[arg-type]
    raise ValueError("unsupported image media type")


# --- validating tar relay -------------------------------------------------------


class _Reader(io.RawIOBase):
    """Count, hash, cap and cancel a sequential byte stream."""

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
        block = self.source.read(min(len(buffer), 1024 * 1024))
        size = len(block)
        self.count += size
        if self.count > self.max_bytes:
            raise ValueError("image stream exceeds byte limit")
        self.hash.update(block)
        buffer[:size] = block
        return size


def _member_name(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError("unsafe image archive member")
    cleaned = str(path)
    if cleaned in {"", "."}:
        raise ValueError("unsafe image archive member")
    return cleaned


def _header(info: tarfile.TarInfo, name: str) -> tarfile.TarInfo:
    clean = tarfile.TarInfo(name)
    clean.type = info.type
    clean.size = info.size if info.isfile() else 0
    clean.mode = info.mode & 0o755
    clean.mtime = info.mtime
    clean.linkname = info.linkname
    return clean


def _blob(kept: Mapping[str, bytes], digest: str) -> bytes:
    validate_digest(digest)
    try:
        return kept["blobs/sha256/" + digest[7:]]
    except KeyError as error:
        raise LookupError("blob absent from archive") from error


def _identify(
    metadata: Mapping[str, bytes], kept: Mapping[str, bytes], seen: set[str]
) -> tuple[str, str, tuple[str, ...]]:
    if "oci-layout" in metadata:
        index = _json_object(metadata.get("index.json", b"{}"))
        manifests = index.get("manifests")
        if not isinstance(manifests, list) or len(manifests) != 1:
            raise ValueError("expected exactly one image in the OCI archive")
        configs = resolve_configs(manifests[0], lambda d: _blob(kept, d))
        if len(configs) != 1:
            raise ValueError("expected exactly one runnable platform image")
        _blob(kept, configs[0])  # the config itself must be in the stream
        annotations = manifests[0].get("annotations") or {}
        name = annotations.get(IMAGE_NAME)
        return "oci", configs[0], (name,) if isinstance(name, str) else ()
    if "manifest.json" not in metadata:
        raise ValueError("unrecognized image archive format")
    entries = json.loads(metadata["manifest.json"])
    if not isinstance(entries, list) or len(entries) != 1:
        raise ValueError("expected exactly one image in the Docker archive")
    entry = entries[0]
    config_name = entry.get("Config") if isinstance(entry, dict) else None
    layers = entry.get("Layers") if isinstance(entry, dict) else None
    if not isinstance(config_name, str) or not isinstance(layers, list):
        raise ValueError("invalid Docker archive manifest")
    body = kept.get(_member_name(config_name))
    if body is None:
        raise ValueError("image config absent from archive")
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    if _member_name(config_name) not in {
        digest[7:] + ".json",
        "blobs/sha256/" + digest[7:],
    }:
        raise ValueError("image config name does not match its content")
    config = _json_object(body)
    rootfs = config.get("rootfs")
    diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
    if not isinstance(diff_ids, list) or len(diff_ids) != len(layers):
        raise ValueError("image config does not describe the archive layers")
    if any(
        not isinstance(layer, str) or _member_name(layer) not in seen
        for layer in layers
    ):
        raise ValueError("image layer absent from archive")
    tags = entry.get("RepoTags") or []
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("invalid Docker archive tags")
    return "docker", digest, tuple(tags)


def _rename(
    metadata: dict[str, bytes], archive_format: str, reference: str
) -> list[tuple[str, bytes]]:
    """Index members that name the image exactly ``reference`` and nothing else."""
    out: list[tuple[str, bytes]] = []
    if archive_format == "oci":
        index = _json_object(metadata["index.json"])
        descriptor = index["manifests"][0]
        annotations = {
            key: value
            for key, value in (descriptor.get("annotations") or {}).items()
            if key not in {IMAGE_NAME, REF_NAME}
        }
        annotations[IMAGE_NAME] = reference
        annotations[REF_NAME] = reference.rsplit(":", 1)[1]
        descriptor["annotations"] = annotations
        out.append(("oci-layout", metadata["oci-layout"]))
        out.append(("index.json", json.dumps(index, sort_keys=True).encode()))
    if "manifest.json" in metadata:
        entries = json.loads(metadata["manifest.json"])
        entries[0]["RepoTags"] = [reference]
        out.append(("manifest.json", json.dumps(entries, sort_keys=True).encode()))
    return out


class _Sink(io.RawIOBase):
    """Destination that drops every write once sealed after a failed check."""

    def __init__(self, destination: BinaryIO) -> None:
        self.destination = destination
        self.sealed = False

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        if self.sealed:
            return len(data)
        return self.destination.write(data)


def relay_image_stream(
    source: BinaryIO,
    destination: BinaryIO | None,
    *,
    approved_digest: str | None = None,
    reference: str | None = None,
    max_bytes: int = 16 * 1024**3,
    cancel: threading.Event | None = None,
) -> StreamIdentity:
    """Validate an image tar stream and, optionally, re-emit it for import.

    Layers are forwarded as they arrive; index members are withheld until the
    config digest recomputed from the stream equals ``approved_digest``. With a
    ``reference`` the emitted archive names the image exactly that.
    """
    if (destination is None) != (reference is None):
        raise ValueError("a relay destination needs exactly one image reference")
    reader = _Reader(source, max_bytes, cancel)
    buffered = io.BufferedReader(reader, 1024 * 1024)
    sink = _Sink(destination) if destination is not None else None
    output = (
        tarfile.open(fileobj=sink, mode="w|", format=tarfile.PAX_FORMAT)  # noqa: SIM115
        if sink is not None
        else None
    )
    try:
        return _relay(buffered, reader, output, approved_digest, reference)
    except BaseException:
        if sink is not None:
            sink.sealed = True  # a pending tar buffer must never reach the node
        raise


def _relay(
    buffered: io.BufferedIOBase,
    reader: _Reader,
    output: tarfile.TarFile | None,
    approved_digest: str | None,
    reference: str | None,
) -> StreamIdentity:
    metadata: dict[str, bytes] = {}
    kept: dict[str, bytes] = {}
    kept_total = 0
    seen: set[str] = set()
    with tarfile.open(fileobj=buffered, mode="r|") as archive:  # type: ignore[call-overload]
        for count, info in enumerate(archive, start=1):
            if count > _MAX_MEMBERS:
                raise ValueError("image archive has too many members")
            name = _member_name(info.name)
            if info.isdir() or info.issym() or info.islnk():
                if info.linkname.startswith("/"):
                    raise ValueError("unsafe image archive link")
                seen.add(name)
                if output is not None:
                    output.addfile(_header(info, name))
                continue
            if not info.isfile():
                raise ValueError("unsafe image archive member")
            stream = archive.extractfile(info)
            assert stream is not None
            if name in _METADATA_MEMBERS:
                if info.size > _SMALL or name in metadata:
                    raise ValueError("invalid image archive index")
                metadata[name] = stream.read()
                continue
            seen.add(name)
            if info.size <= _SMALL and kept_total + info.size <= _KEPT_TOTAL:
                body = stream.read()
                kept[name] = body
                kept_total += len(body)
                if output is not None:
                    output.addfile(_header(info, name), io.BytesIO(body))
            elif output is not None:
                output.addfile(_header(info, name), stream)
    while buffered.read(1024 * 1024):  # trailing tar padding is part of the hash
        pass
    archive_format, config_digest, names = _identify(metadata, kept, seen)
    if approved_digest is not None and config_digest != approved_digest:
        raise DeliveryRejected("image config digest differs from the approval")
    if output is not None:
        assert reference is not None
        for name, body in _rename(metadata, archive_format, reference):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o644
            output.addfile(info, io.BytesIO(body))
        output.close()  # end-of-archive blocks; the destination stays open
    return StreamIdentity(
        archive_format,
        config_digest,
        names,
        "sha256:" + reader.hash.hexdigest(),
        reader.count,
    )


# --- delivery -------------------------------------------------------------------


@dataclass(frozen=True)
class NodeImage:
    reference: str
    target_digest: str
    media_type: str
    config_digests: tuple[str, ...]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _Failure(Exception):
    def __init__(self, result: str, reason: str) -> None:
        super().__init__(reason)
        self.result = result
        self.reason = reason


@dataclass(frozen=True)
class NodeDelivery:
    """Deliver one image into one node's containerd through a pinned transport.

    ``docker`` is required for ``DockerImageSource`` and ``docker://`` targets;
    ``ssh`` for ``ssh://`` targets. ``ssh_agent_socket`` is the only
    credential-bearing environment forwarded, and only when given.
    """

    docker: ToolPin | None = None
    docker_socket: Path | None = field(default=None, repr=False)
    ssh: ToolPin | None = None
    ssh_agent_socket: Path | None = field(default=None, repr=False)
    runner: Runner = field(default_factory=SubprocessRunner, repr=False)

    def __post_init__(self) -> None:
        if self.docker_socket is not None and not self.docker_socket.is_absolute():
            raise DeliveryInputError(
                "docker-socket-required", "explicit absolute Docker socket required"
            )
        if (
            self.ssh_agent_socket is not None
            and not self.ssh_agent_socket.is_absolute()
        ):
            raise DeliveryInputError(
                "invalid-ssh-agent-socket",
                "explicit absolute ssh agent socket required",
            )

    def _transport(self, target: NodeTarget) -> Transport:
        return Transport(target, self.docker, self.docker_socket, self.ssh)

    def _env(self, target: NodeTarget) -> dict[str, str]:
        if target.transport == "ssh" and self.ssh_agent_socket is not None:
            return {"SSH_AUTH_SOCK": str(self.ssh_agent_socket)}
        return {}

    def _docker(self, *arguments: str) -> list[str]:
        if self.docker is None or self.docker_socket is None:
            raise DeliveryInputError("docker-tool-required")
        return [
            str(self.docker.path),
            "--host",
            f"unix://{self.docker_socket}",
            *arguments,
        ]

    def preview(
        self, target: NodeTarget, approved_digest: str, reference: str
    ) -> dict[str, Any]:
        """The node-side commands, without contacting anything."""
        validate_digest(approved_digest)
        name = normalize_reference(reference)
        return {
            "schema": SCHEMA,
            "target": target.public(),
            "reference": name,
            "approved_digest": approved_digest,
            "node_import_argv": target.runtime_argv("images", "import", "-"),
            "node_query_argv": target.runtime_argv("images", "ls"),
            "intermediate_registry": False,
            "pushed": False,
            "requires_explicit_grant": True,
        }

    # node queries -------------------------------------------------------------

    def _node_capture(self, target: NodeTarget, *arguments: str) -> bytes:
        argv = self._transport(target).argv(target.runtime_argv(*arguments))
        result = self.runner.capture(argv, _QUERY, self._env(target))
        if result.state != "succeeded":
            raise _Failure("failed", "node-query-failed")
        return result.stdout

    def inspect_node(self, target: NodeTarget, reference: str) -> NodeImage | None:
        """Resolve ``reference`` on the node to the config digest(s) it names."""
        listing = self._node_capture(target, "images", "ls").decode("utf-8", "replace")
        row = None
        for line in listing.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3 and parts[0] == reference:
                row = parts
                break
        if row is None:
            return None
        media, target_digest = row[1], row[2]

        def fetch(digest: str) -> bytes:
            argv = self._transport(target).argv(
                target.runtime_argv("content", "get", validate_digest(digest))
            )
            result = self.runner.capture(argv, _QUERY, self._env(target))
            if result.state != "succeeded":
                raise LookupError("content absent on node")
            return result.stdout

        try:
            configs = resolve_configs(
                {"digest": target_digest, "mediaType": media}, fetch
            )
        except (LookupError, ValueError) as error:
            raise _Failure("failed", "node-query-failed") from error
        return NodeImage(reference, target_digest, media, tuple(configs))

    # source identity ----------------------------------------------------------

    def _local_image_id(self, source: DockerImageSource) -> str:
        result = self.runner.capture(
            self._docker("image", "inspect", "--format", "{{.Id}}", source.reference),
            _QUERY,
            {},
        )
        if result.state != "succeeded":
            raise _Failure("failed", "source-unavailable")
        return validate_digest(result.stdout.decode().strip())

    def _containerd_image_store(self) -> bool:
        result = self.runner.capture(
            self._docker("info", "--format", "{{json .DriverStatus}}"), _QUERY, {}
        )
        return (
            result.state == "succeeded"
            and b"io.containerd.snapshotter" in result.stdout
        )

    def _scan_archive(
        self, source: ArchiveSource, cancel: threading.Event | None
    ) -> StreamIdentity:
        try:
            with _RegularFile(source.path, source.max_bytes) as stream:
                return relay_image_stream(
                    stream, None, max_bytes=source.max_bytes, cancel=cancel
                )
        except (ValueError, OSError, tarfile.TarError, LookupError) as error:
            raise _Failure("rejected", "invalid-archive") from error

    # delivery -----------------------------------------------------------------

    def deliver(
        self,
        source: DockerImageSource | ArchiveSource,
        target: NodeTarget,
        grant: DeliveryGrant,
        *,
        reference: str | None = None,
        limits: ProcessLimits | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Deliver, or confirm presence of, exactly the approved image.

        Returns a secret-safe receipt for every operational outcome. Invalid
        grants, tools or inputs raise ``ValueError`` before anything runs.
        """
        if (
            NodeTarget.parse(grant.target).identity != target.identity
            or grant.expires_at <= time.time()
        ):
            raise DeliveryInputError(
                "grant-mismatch", "exact unexpired delivery grant required"
            )
        self._transport(target)  # validates that the transport's tool is pinned
        tools: dict[str, ToolPin] = {}
        if target.transport == "ssh" and self.ssh is not None:
            tools["ssh"] = self.ssh
        if target.transport == "docker" or isinstance(source, DockerImageSource):
            if self.docker is None or self.docker_socket is None:
                raise DeliveryInputError("docker-tool-required")
            tools["docker"] = self.docker
        name = normalize_reference(reference) if reference is not None else None
        if isinstance(source, DockerImageSource) and name is None:
            if source.is_id:
                raise DeliveryInputError(
                    "reference-required",
                    "an image ID source needs an explicit reference",
                )
            name = normalize_reference(source.reference)
        verify_tools(tools)
        limits = limits or ProcessLimits(600, 1_048_576)
        started_at, started = _now(), time.monotonic()
        receipt: dict[str, Any] = {
            "schema": SCHEMA,
            "approved_digest": grant.approved_digest,
            "target": target.public(),
            "source": {
                "kind": "docker-image"
                if isinstance(source, DockerImageSource)
                else "archive"
            },
            "tools": {key: tool.sha256 for key, tool in sorted(tools.items())},
            "image": {"reference": name, "config_digest": None},
            "previous": None,
            "transfer": {
                "streamed": False,
                "bytes": 0,
                "intermediate_registry": False,
                "temporary_archive": False,
            },
            "pushed": False,
            "started_at": started_at,
        }
        try:
            result = self._deliver(source, target, grant, name, limits, cancel, receipt)
            receipt.update(result=result, reason=None, state="succeeded")
        except _Failure as failure:
            receipt.update(
                result=failure.result,
                reason=failure.reason,
                state="rejected" if failure.result == "rejected" else "failed",
            )
        finally:
            verify_tools(tools)
        receipt["finished_at"] = _now()
        receipt["seconds"] = round(time.monotonic() - started, 3)
        return receipt

    def _deliver(
        self,
        source: DockerImageSource | ArchiveSource,
        target: NodeTarget,
        grant: DeliveryGrant,
        name: str | None,
        limits: ProcessLimits,
        cancel: threading.Event | None,
        receipt: dict[str, Any],
    ) -> str:
        approved = grant.approved_digest
        save_argv: list[str] | None = None
        # 1. Prove the source locally before any byte leaves the machine.
        if isinstance(source, DockerImageSource):
            local_id = self._local_image_id(source)
            receipt["source"]["local_image_id"] = local_id
            if local_id != approved and not self._containerd_image_store():
                # Classic store: the image ID *is* the config digest.
                raise _Failure("rejected", "digest-mismatch")
            save_argv = self._docker("image", "save", local_id)
        else:
            scanned = self._scan_archive(source, cancel)
            receipt["source"].update(
                format=scanned.format,
                archive_sha256=scanned.stream_sha256,
                bytes=scanned.bytes,
            )
            if scanned.config_digest != approved:
                raise _Failure("rejected", "digest-mismatch")
            if name is None:
                if len(scanned.names) != 1:
                    raise _Failure("rejected", "reference-required")
                name = normalize_reference(scanned.names[0])
        assert name is not None
        receipt["image"]["reference"] = name
        # 2. Idempotency: the node already maps the name to the approved config.
        before = self.inspect_node(target, name)
        if before is not None and approved in before.config_digests:
            receipt["image"].update(
                config_digest=approved,
                node_target_digest=before.target_digest,
                node_media_type=before.media_type,
            )
            return "already-present"
        if before is not None:
            receipt["previous"] = {
                "node_target_digest": before.target_digest,
                "config_digests": list(before.config_digests),
            }
        # 3. Stream with the index withheld until the digest is proven.
        identity: list[StreamIdentity] = []

        def relay(source_stream: BinaryIO, sink: BinaryIO) -> None:
            identity.append(
                relay_image_stream(
                    source_stream,
                    sink,
                    approved_digest=approved,
                    reference=name,
                    max_bytes=(
                        source.max_bytes
                        if isinstance(source, ArchiveSource)
                        else 64 * 1024**3
                    ),
                    cancel=cancel,
                )
            )

        def writer(sink: BinaryIO) -> None:
            if save_argv is None:
                assert isinstance(source, ArchiveSource)
                with _RegularFile(source.path, source.max_bytes) as stream:
                    relay(stream, sink)
                return
            saved, _ = self.runner.read(
                save_argv, lambda stream: relay(stream, sink), limits, {}, cancel
            )
            if saved.state != "succeeded":
                raise _Failure("failed", "source-unavailable")

        remaining = max(0.001, min(limits.max_seconds, grant.expires_at - time.time()))
        import_limits = ProcessLimits(remaining, limits.max_output_bytes)
        argv = self._transport(target).argv(
            target.runtime_argv("images", "import", "-"), stdin=True
        )
        receipt["transfer"]["streamed"] = True
        try:
            imported: RunResult = self.runner.feed(
                argv, writer, import_limits, self._env(target), cancel
            )
        except DeliveryRejected as error:
            # The index was withheld: the runtime received no importable image.
            raise _Failure("rejected", "digest-mismatch") from error
        except InterruptedError as error:
            raise _Failure("failed", "cancelled") from error
        except (ValueError, tarfile.TarError, LookupError, OSError) as error:
            raise _Failure("failed", "invalid-image-stream") from error
        if identity:
            receipt["transfer"]["bytes"] = identity[0].bytes
            receipt["source"]["stream_sha256"] = identity[0].stream_sha256
            receipt["source"]["format"] = identity[0].format
        if imported.state != "succeeded":
            raise _Failure("failed", "import-" + imported.state)
        # 4. Verify on the node what it now calls ``name``.
        after = self.inspect_node(target, name)
        if after is None or approved not in after.config_digests:
            raise _Failure("failed", "verification-failed")
        receipt["image"].update(
            config_digest=approved,
            node_target_digest=after.target_digest,
            node_media_type=after.media_type,
        )
        return "imported"


class _RegularFile:
    """Open a bounded regular file without following a final symbolic link."""

    def __init__(self, path: Path, max_bytes: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.stream: BinaryIO | None = None

    def __enter__(self) -> BinaryIO:
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > self.max_bytes:
            os.close(fd)
            raise ValueError("image archive must be a bounded regular file")
        self.stream = os.fdopen(fd, "rb")
        return self.stream

    def __exit__(self, *_: object) -> None:
        assert self.stream is not None
        self.stream.close()


# --- receipts -------------------------------------------------------------------


def write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    """Atomically write one receipt as private, sorted JSON."""
    body = json.dumps(receipt, sort_keys=True, indent=2).encode() + b"\n"
    partial = path.with_name(f".{path.name}.{os.getpid()}.partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(partial, path)


def append_journal(path: Path, receipt: Mapping[str, Any]) -> None:
    """Append one receipt as a single JSON line and flush it to disk."""
    line = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
