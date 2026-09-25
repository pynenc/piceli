"""Copy a third-party image by digest from its registry into a delivery registry.

A *mirror* makes an image the app does not build (``redis@sha256:…``) pullable
from the same place as the built images, for example a node-loopback registry
on a node without internet access. Only digest-pinned references are accepted:
a tag can move, a digest cannot.

The copy runs on the machine running Piceli and speaks OCI Distribution v2 on
both sides (:class:`~piceli.artifacts.registry.StreamedOciRegistryClient`); no
container engine is involved:

1. Read the source manifest **by digest** and check that its bytes hash to it.
2. For an image index (multi-arch), keep the index (so the digest the app
   pinned stays the digest it pulls) and copy the child manifests of the
   requested platforms, or all of them. A single-platform manifest must match
   the requested platform.
3. Copy every missing blob (config and layers), streamed and hashed while it
   is sent; blobs the destination already has are skipped.
4. ``PUT`` the child manifests, then the index, by digest, and read the top
   manifest back to verify its digest.

A manifest already present in the destination is only verified
(``already-present``): registries are content-addressed. The receipt
(``piceli.mirror-delivery.v1``) records the digests, platforms and blob
counts; never credentials.

Importing this module opens no socket and reads no file.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from piceli.artifacts.image_manifest import (
    CONFIG_TYPES,
    INDEX_TYPES,
    MANIFEST_TYPES,
    OCI_MANIFEST,
)
from piceli.artifacts.registry import (
    REPOSITORY,
    RegistryError,
    StreamedOciRegistryClient,
    is_loopback,
    validate_host,
)

SCHEMA = "piceli.mirror-delivery.v1"
DOCKER_HUB = "docker.io"
DOCKER_HUB_ENDPOINT = "registry-1.docker.io"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_MAX_CONFIG = 4 * 1024 * 1024
_MAX_CHILDREN = 64


class MirrorError(ValueError):
    """A mirror reference or copy was refused. ``code`` is a registered code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class MirrorSource:
    """A digest-pinned image reference, normalized like ``docker pull`` does.

    ``domain`` is the registry as written (``docker.io`` for short names),
    ``repository`` the path in it (``library/redis`` for ``redis``) and
    ``digest`` the pinned manifest or index digest. A tag next to the digest
    (``redis:7.2@sha256:…``) is informational and dropped.
    """

    domain: str
    repository: str
    digest: str

    @classmethod
    def parse(cls, reference: str) -> MirrorSource:
        """Parse ``[registry/]repository[:tag]@sha256:<64 hex>``.

        :raises MirrorError: ``pipeline-mirror-not-pinned`` for a reference
            without a digest; ``pipeline-invalid`` for a malformed one.
        """
        if (
            not isinstance(reference, str)
            or not reference
            or len(reference) > 512
            or not reference.isprintable()
            or any(char.isspace() for char in reference)
        ):
            raise MirrorError("pipeline-invalid", "invalid mirror reference")
        name, sep, digest = reference.partition("@")
        if not sep:
            raise MirrorError(
                "pipeline-mirror-not-pinned",
                f"mirror {reference!r} is not pinned by digest; use "
                "repository@sha256:<digest> (a tag can move)",
            )
        if not _DIGEST.fullmatch(digest):
            raise MirrorError(
                "pipeline-mirror-not-pinned",
                f"mirror {reference!r} must be pinned as @sha256:<64 lowercase hex>",
            )
        slash, colon = name.rfind("/"), name.rfind(":")
        if colon > slash:
            name, tag = name[:colon], name[colon + 1 :]
            if not _TAG.fullmatch(tag):
                raise MirrorError("pipeline-invalid", f"invalid tag in {reference!r}")
        first, _, rest = name.partition("/")
        if rest and ("." in first or ":" in first or first == "localhost"):
            domain, path = first, rest
        else:
            domain, path = DOCKER_HUB, name
        domain = domain.lower()
        if domain in {"index.docker.io", DOCKER_HUB_ENDPOINT}:
            domain = DOCKER_HUB
        if domain == DOCKER_HUB and "/" not in path:
            path = f"library/{path}"
        host = domain.rsplit(":", 1)[0] if domain.count(":") == 1 else domain
        try:
            validate_host(host.strip("[]"))
        except ValueError:
            raise MirrorError(
                "pipeline-invalid", f"invalid registry in {reference!r}"
            ) from None
        if len(path) > 255 or not REPOSITORY.fullmatch(path):
            raise MirrorError(
                "pipeline-invalid", f"invalid repository in {reference!r}"
            )
        return cls(domain, path, digest)

    @property
    def key(self) -> str:
        """The canonical reference: ``domain/repository@digest``."""
        return f"{self.domain}/{self.repository}@{self.digest}"

    @property
    def endpoint_host(self) -> str:
        """``host[:port]`` of the registry API (Docker Hub's API host for docker.io)."""
        return DOCKER_HUB_ENDPOINT if self.domain == DOCKER_HUB else self.domain

    @property
    def url(self) -> str:
        """The ``oci://`` URL of the source repository (plain HTTP only on loopback)."""
        host = self.endpoint_host
        bare = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        tls = "false" if is_loopback(bare.strip("[]")) else "true"
        return f"oci://{host}/{self.repository}?tls={tls}"

    def mirrored_repository(self, prefix: str = "mirror") -> str:
        """Where the copy lives: ``<prefix>/<domain>/<repository>``.

        The domain keeps copies of equally named repositories of different
        registries apart; a port becomes ``-<port>``.
        """
        domain = self.domain.lower().replace(":", "-").strip("[]")
        repository = (
            f"{prefix}/{domain}/{self.repository}"
            if prefix
            else (f"{domain}/{self.repository}")
        )
        if not REPOSITORY.fullmatch(repository):
            raise MirrorError(
                "pipeline-invalid", f"cannot mirror {self.key!r} into {repository!r}"
            )
        return repository


def same_image(reference: str, source: MirrorSource) -> bool:
    """Whether ``reference`` (as an app writes it) names the mirrored image."""
    try:
        return MirrorSource.parse(reference).key == source.key
    except MirrorError:
        return False


# ------------------------------------------------------------------ platforms


@dataclass(frozen=True)
class Platform:
    """``os/architecture[/variant]`` of a node or an image."""

    os: str
    architecture: str
    variant: str | None = None

    @classmethod
    def parse(cls, value: str) -> Platform:
        parts = value.split("/") if isinstance(value, str) else []
        if len(parts) not in {2, 3} or not all(
            re.fullmatch(r"[a-z0-9_.-]{1,32}", part) for part in parts
        ):
            raise MirrorError("pipeline-invalid", f"invalid platform {value!r}")
        return cls(parts[0], parts[1], parts[2] if len(parts) == 3 else None)

    def __str__(self) -> str:
        return "/".join(
            part for part in (self.os, self.architecture, self.variant) if part
        )

    def accepts(self, entry: Mapping[str, Any]) -> bool:
        """Whether an index entry's ``platform`` runs on this platform."""
        if entry.get("os") != self.os or entry.get("architecture") != self.architecture:
            return False
        variant = entry.get("variant")
        if self.variant is None:
            # Kubernetes reports no variant: arm64 nodes run v8 (or unset).
            return variant in (None, "", "v8") if self.architecture == "arm64" else True
        return variant in (None, "", self.variant)


def _media_type(body: bytes, header: str) -> tuple[str, dict[str, Any]]:
    try:
        document = json.loads(body)
    except ValueError:
        raise MirrorError("mirror-manifest-invalid") from None
    if not isinstance(document, dict):
        raise MirrorError("mirror-manifest-invalid")
    media = document.get("mediaType") or header.split(";")[0].strip()
    if media not in MANIFEST_TYPES | INDEX_TYPES:
        # An OCI manifest may omit mediaType; recognise it by its shape.
        if "config" in document and "layers" in document:
            media = OCI_MANIFEST
        elif "manifests" in document:
            media = "application/vnd.oci.image.index.v1+json"
        else:
            raise MirrorError("mirror-manifest-invalid")
    return media, document


def _descriptor(value: Any) -> tuple[str, int, str]:
    if not isinstance(value, dict):
        raise MirrorError("mirror-manifest-invalid")
    digest, size = value.get("digest"), value.get("size")
    if (
        not isinstance(digest, str)
        or not _DIGEST.fullmatch(digest)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
    ):
        raise MirrorError("mirror-manifest-invalid")
    return digest, size, str(value.get("mediaType") or "")


def _sha(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


# ------------------------------------------------------------------- copying

ClientFactory = Callable[[str], StreamedOciRegistryClient]


@dataclass
class _Copy:
    source: StreamedOciRegistryClient
    target: StreamedOciRegistryClient
    source_repository: str
    target_repository: str
    deadline: float
    cancel: threading.Event | None
    blobs: dict[str, Any]

    def check(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise MirrorError("cancelled")
        if time.time() > self.deadline:
            raise MirrorError("timed-out")

    def manifest(self, digest: str) -> tuple[bytes, str, dict[str, Any]]:
        self.check()
        body, header = self.source.get_manifest(self.source_repository, digest)
        if _sha(body) != digest:
            raise MirrorError(
                "mirror-digest-mismatch",
                "the source registry served a manifest whose bytes do not hash "
                "to the pinned digest",
            )
        media, document = _media_type(body, header)
        return body, media, document

    def blob(self, digest: str, size: int, media: str) -> None:
        self.check()
        present = self.target.blob_size(self.target_repository, digest)
        entry = {"digest": digest, "size": size, "media_type": media}
        if present is not None:
            self.blobs["skipped"] += 1
            self.blobs["skipped_bytes"] += size
            self.blobs["items"].append(entry | {"action": "skipped"})
            return
        with self.source.open_blob(self.source_repository, digest) as stream:
            upload = self.target.push_blob(
                self.target_repository, digest, size, stream, cancel=self.cancel
            )
        self.blobs["uploaded"] += 1
        self.blobs["uploaded_bytes"] += size
        self.blobs["items"].append(entry | {"action": "uploaded", "mode": upload.mode})

    def config_platform(self, digest: str, size: int) -> str | None:
        if size > _MAX_CONFIG:
            raise MirrorError("mirror-manifest-invalid")
        with self.source.open_blob(self.source_repository, digest) as stream:
            body = stream.read(size + 1)
        if len(body) != size or _sha(body) != digest:
            raise MirrorError("mirror-digest-mismatch")
        try:
            config = json.loads(body)
        except ValueError:
            raise MirrorError("mirror-manifest-invalid") from None
        if not isinstance(config, dict):
            raise MirrorError("mirror-manifest-invalid")
        os_name, arch = config.get("os"), config.get("architecture")
        if not isinstance(os_name, str) or not isinstance(arch, str):
            return None
        variant = config.get("variant")
        return "/".join(
            part
            for part in (os_name, arch, variant if isinstance(variant, str) else None)
            if part
        )

    def image(
        self, digest: str, document: dict[str, Any], platform: Platform | None
    ) -> str | None:
        """Copy one image manifest's blobs; returns its config platform if read."""
        config_digest, config_size, config_media = _descriptor(document.get("config"))
        layers = document.get("layers")
        if not isinstance(layers, list) or len(layers) > 1024:
            raise MirrorError("mirror-manifest-invalid")
        seen = None
        if platform is not None and config_media in CONFIG_TYPES | {""}:
            seen = self.config_platform(config_digest, config_size)
            if seen is not None and not platform.accepts(
                dict(
                    zip(
                        ("os", "architecture", "variant"), seen.split("/"), strict=False
                    )
                )
            ):
                raise MirrorError(
                    "mirror-platform-unavailable",
                    f"the image is {seen}; the node runs {platform}",
                )
        self.blob(config_digest, config_size, config_media)
        for layer in layers:
            layer_digest, layer_size, layer_media = _descriptor(layer)
            self.blob(layer_digest, layer_size, layer_media)
        return seen

    def put(self, digest: str, body: bytes, media: str) -> None:
        self.check()
        pushed = self.target.push_manifest(self.target_repository, digest, body, media)
        if pushed != digest:
            raise MirrorError("mirror-digest-mismatch")


def mirror_image(
    source: MirrorSource,
    source_client: StreamedOciRegistryClient,
    target_client: StreamedOciRegistryClient,
    target_repository: str,
    *,
    platform: Platform | None,
    target_registry: str,
    node_registry: str,
    max_seconds: float = 3600.0,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Copy ``source`` into ``target_repository``; returns the receipt.

    :param platform: The node platform to copy from an index, or ``None`` for
        every platform (a complete copy of the index).
    :param target_registry: ``host[:port]`` the copy is pushed to (receipt).
    :param node_registry: ``host[:port]`` nodes pull from; the receipt's
        ``pull_ref`` is ``<node_registry>/<target_repository>@<digest>``.

    Every operational outcome returns a receipt with ``state`` ``succeeded``,
    ``failed`` or ``rejected`` and a registered ``reason``.
    """
    started, clock = _now(), time.monotonic()
    blobs: dict[str, Any] = {
        "uploaded": 0,
        "skipped": 0,
        "uploaded_bytes": 0,
        "skipped_bytes": 0,
        "items": [],
    }
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "source": {
            "reference": source.key,
            "registry": source.endpoint_host,
            "repository": source.repository,
            "digest": source.digest,
            "auth": source_client.endpoint.auth_scheme,
        },
        "platform": str(platform) if platform is not None else "all",
        "target": {"registry": target_registry, "repository": target_repository},
        "node_registry": node_registry,
        "image": {"digest": source.digest, "media_type": None, "manifests": []},
        "blobs": blobs,
        "pull_ref": None,
        "started_at": started,
    }
    copy = _Copy(
        source_client,
        target_client,
        source.repository,
        target_repository,
        time.time() + max_seconds,
        cancel,
        blobs,
    )
    try:
        result = _mirror(copy, source, platform, receipt)
        receipt.update(
            result=result,
            state="succeeded",
            reason=None,
            pull_ref=f"{node_registry}/{target_repository}@{source.digest}",
        )
    except MirrorError as error:
        rejected = error.code in {"mirror-digest-mismatch", "mirror-manifest-invalid"}
        receipt.update(
            result="rejected" if rejected else "failed",
            state="rejected" if rejected else "failed",
            reason=error.code,
            message=str(error) if str(error) != error.code else None,
        )
    except RegistryError as error:
        receipt.update(result="failed", state="failed", reason=error.reason)
    receipt["finished_at"] = _now()
    receipt["seconds"] = round(time.monotonic() - clock, 3)
    return receipt


def _mirror(
    copy: _Copy,
    source: MirrorSource,
    platform: Platform | None,
    receipt: dict[str, Any],
) -> str:
    copy.target.authenticate(copy.target_repository)
    if (
        copy.target.manifest_digest(copy.target_repository, source.digest)
        == source.digest
    ):
        body, header = copy.target.get_manifest(copy.target_repository, source.digest)
        if _sha(body) != source.digest:
            raise MirrorError("mirror-digest-mismatch")
        receipt["image"]["media_type"] = _media_type(body, header)[0]
        return "already-present"
    copy.source.authenticate(source.repository)
    body, media, document = copy.manifest(source.digest)
    receipt["image"]["media_type"] = media
    children: list[dict[str, Any]] = []
    if media in INDEX_TYPES:
        entries = document.get("manifests")
        if not isinstance(entries, list) or len(entries) > _MAX_CHILDREN:
            raise MirrorError("mirror-manifest-invalid")
        chosen = [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and (platform is None or platform.accepts(entry.get("platform") or {}))
        ]
        if platform is not None:
            chosen = _prefer(chosen, platform)
            if not chosen:
                raise MirrorError(
                    "mirror-platform-unavailable",
                    f"the index {source.digest[:19]}… has no manifest for {platform}",
                )
        for entry in chosen:
            digest, _, _ = _descriptor(entry)
            child, child_media, child_document = copy.manifest(digest)
            if child_media not in MANIFEST_TYPES:
                raise MirrorError("mirror-manifest-invalid")
            copy.image(digest, child_document, None)
            copy.put(digest, child, child_media)
            described = entry.get("platform") or {}
            children.append(
                {
                    "digest": digest,
                    "platform": "/".join(
                        str(described[key])
                        for key in ("os", "architecture", "variant")
                        if described.get(key)
                    )
                    or "unknown",
                }
            )
    else:
        seen = copy.image(source.digest, document, platform)
        children.append({"digest": source.digest, "platform": seen or "unknown"})
    receipt["image"]["manifests"] = children
    copy.put(source.digest, body, media)
    served, _ = copy.target.get_manifest(copy.target_repository, source.digest)
    if _sha(served) != source.digest:
        raise MirrorError("mirror-digest-mismatch")
    return "mirrored"


def _prefer(
    entries: Iterable[dict[str, Any]], platform: Platform
) -> list[dict[str, Any]]:
    """One entry for the platform: an exact variant first, then the first match."""
    items = list(entries)
    for entry in items:
        if (entry.get("platform") or {}).get("variant") == platform.variant:
            return [entry]
    return items[:1]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def public(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """The small, path-free view of a mirror receipt used in events."""
    return {
        key: value
        for key, value in {
            "result": receipt.get("result"),
            "state": receipt.get("state"),
            "reason": receipt.get("reason"),
            "digest": (receipt.get("image") or {}).get("digest"),
            "reference": receipt.get("pull_ref"),
            "seconds": receipt.get("seconds"),
        }.items()
        if value is not None
    }
