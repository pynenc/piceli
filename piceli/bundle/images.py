"""Image archives of a client bundle: one OCI image-layout tar per image.

The images come from a build receipt (``piceli artifacts build-spec run``,
one or several platforms, the same receipt ``piceli artifacts publish``
reads; :func:`piceli.artifacts.publish._grouped` groups it). For each image
and each chosen platform the archive is read once and checked
(:func:`~piceli.artifacts.image_manifest.scan_image_stream`: every layer
against the config's ``diff_ids``, the config digest against the receipt),
then its manifest, config and layers are copied byte for byte into
``images/<name>.oci.tar``:

- one platform: ``index.json`` names the platform manifest, whose digest is
  the image digest;
- several platforms: the image index of
  :func:`~piceli.artifacts.multi_platform.image_index` is a blob and
  ``index.json`` names it; its digest is the image digest.

The digest survives ``skopeo copy --all --preserve-digests`` (and ``crane``,
``oras``) into the client's registry. The SBOM and provenance of each
platform, recorded in the receipt, are copied next to the archive after
their sha256 is checked. Archives are deterministic (fixed order, modes and
times). Importing this module runs nothing.
"""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from piceli.artifacts.image_manifest import (
    OCI_INDEX,
    PushPlan,
    member_name,
    scan_image_stream,
)
from piceli.artifacts.multi_platform import IndexEntry, image_index
from piceli.artifacts.plan import canonical

REF_NAME = "org.opencontainers.image.ref.name"
DEFAULT_PLATFORMS = ("linux/amd64",)
_CHUNK = 1024 * 1024


class ImageExportError(ValueError):
    """An image cannot be exported. ``code`` is a registered error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExportedImage:
    """One image archive of the bundle."""

    name: str
    archive: str
    digest: str
    media_type: str
    platforms: tuple[Mapping[str, Any], ...]
    attestations: tuple[Mapping[str, Any], ...] = field(default=())

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "archive": self.archive,
            "digest": self.digest,
            "media_type": self.media_type,
            "platforms": [dict(item) for item in self.platforms],
            "attestations": [dict(item) for item in self.attestations],
        }


def receipt_images(receipt: Any, output_dir: Path) -> dict[str, Any]:
    """``{image name: (source repository, platform images)}`` of a build receipt."""
    from piceli.artifacts.publish import PublishError, _grouped

    try:
        return _grouped(receipt, output_dir, True)
    except PublishError as error:
        raise ImageExportError("bundle-receipt-invalid", str(error)) from None


def _info(name: str, size: int, *, directory: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
    if directory:
        info.type, info.mode = tarfile.DIRTYPE, 0o755
    else:
        info.size, info.mode = size, 0o644
    return info


def _add_bytes(archive: tarfile.TarFile, name: str, body: bytes) -> None:
    archive.addfile(_info(name, len(body)), io.BytesIO(body))


class _Verified(io.RawIOBase):
    """Reads ``size`` bytes of ``stream`` and checks their sha256 at the end."""

    def __init__(self, stream: Any, digest: str, size: int) -> None:
        self.stream, self.digest, self.left = stream, digest, size
        self.hash = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self.left <= 0:
            return 0
        chunk = self.stream.read(min(len(buffer), self.left, _CHUNK))
        if not chunk:
            raise ImageExportError("bundle-image-mismatch", "image archive ended early")
        self.hash.update(chunk)
        self.left -= len(chunk)
        if self.left == 0 and "sha256:" + self.hash.hexdigest() != self.digest:
            raise ImageExportError(
                "bundle-image-mismatch", "a layer changed while copied"
            )
        buffer[: len(chunk)] = chunk
        return len(chunk)


def _scan(path: Path) -> PushPlan:
    try:
        with open(path, "rb") as stream:
            return scan_image_stream(stream)
    except OSError:
        raise ImageExportError(
            "bundle-receipt-invalid", "cannot read an image archive"
        ) from None
    except ValueError as error:
        raise ImageExportError(
            "bundle-image-mismatch", f"invalid image archive: {error}"
        ) from None


def _copy_blobs(
    archive: tarfile.TarFile, source: Path, plan: PushPlan, written: set[str]
) -> None:
    wanted = {blob.member: blob for blob in plan.layers if blob.digest not in written}
    if not wanted:
        return
    with tarfile.open(source, "r|*") as reader:
        for info in reader:
            try:
                blob = wanted.pop(member_name(info.name), None)
            except ValueError:
                blob = None
            if blob is None or blob.digest in written:
                continue
            stream = reader.extractfile(info)
            assert stream is not None
            archive.addfile(
                _info(f"blobs/sha256/{blob.digest[7:]}", blob.size),
                _Verified(stream, blob.digest, blob.size),  # type: ignore[arg-type]
            )
            written.add(blob.digest)
    missing = [blob for blob in wanted.values() if blob.digest not in written]
    if missing:
        raise ImageExportError(
            "bundle-image-mismatch", "a layer is missing from its archive"
        )


def _attestation(
    platform: Any, kind: str, directory: Path, name: str
) -> dict[str, Any] | None:
    for item in platform.attestations:
        if item.kind != kind:
            continue
        try:
            body = item.path.read_bytes()
        except OSError:
            raise ImageExportError(
                "bundle-receipt-invalid", f"cannot read the {kind} of {name}"
            ) from None
        if "sha256:" + hashlib.sha256(body).hexdigest() != item.sha256:
            raise ImageExportError(
                "bundle-image-mismatch",
                f"the {kind} of {name} differs from the receipt",
            )
        suffix = "sbom.spdx.json" if kind == "sbom" else "provenance.json"
        file = f"{name}.{platform.platform.replace('/', '-')}.{suffix}"
        (directory / file).write_bytes(body)
        return {
            "platform": platform.platform,
            "kind": kind,
            "file": f"images/{file}",
            "sha256": item.sha256,
        }
    return None


def export_image(
    name: str,
    platforms: Sequence[Any],
    directory: Path,
    *,
    tag: str,
) -> ExportedImage:
    """Write ``images/<name>.oci.tar`` (and attestations) under ``directory``.

    ``platforms`` are :class:`~piceli.artifacts.publish.PlatformImage` items,
    each with an ``archive``.
    """
    images = directory / "images"
    images.mkdir(parents=True, exist_ok=True)
    archive_path = images / f"{name}.oci.tar"
    temporary = images / f".{name}.oci.tar.{os.urandom(6).hex()}"
    entries: list[IndexEntry] = []
    attestations: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    try:
        with tarfile.open(temporary, "x", format=tarfile.PAX_FORMAT) as archive:
            _add_bytes(
                archive, "oci-layout", canonical({"imageLayoutVersion": "1.0.0"})
            )
            archive.addfile(_info("blobs", 0, directory=True))
            archive.addfile(_info("blobs/sha256", 0, directory=True))
            written: set[str] = set()
            for platform in sorted(platforms, key=lambda item: item.platform):
                if platform.archive is None:
                    raise ImageExportError(
                        "bundle-image-archive-missing",
                        f"image {name} ({platform.platform}) has no archive in the "
                        "receipt (a Docker build): build it with the host builder "
                        "or save it to an archive",
                    )
                plan = _scan(platform.archive)
                if plan.config_digest != platform.config_digest or (
                    platform.manifest_digest is not None
                    and plan.manifest_digest != platform.manifest_digest
                ):
                    raise ImageExportError(
                        "bundle-image-mismatch",
                        f"the archive of {name} ({platform.platform}) is not the image "
                        "the receipt records",
                    )
                for digest, body in (
                    (plan.manifest_digest, plan.manifest),
                    (plan.config.digest, plan.config_body),
                ):
                    if digest not in written:
                        _add_bytes(archive, f"blobs/sha256/{digest[7:]}", body)
                        written.add(digest)
                _copy_blobs(archive, platform.archive, plan, written)
                entries.append(
                    IndexEntry(
                        platform.platform,
                        plan.manifest_digest,
                        len(plan.manifest),
                        plan.manifest_media_type,
                    )
                )
                summaries.append(
                    {
                        "platform": platform.platform,
                        "manifest_digest": plan.manifest_digest,
                        "config_digest": plan.config_digest,
                        "layers": len(plan.layers),
                    }
                )
                for kind in ("sbom", "provenance"):
                    found = _attestation(platform, kind, images, name)
                    if found is not None:
                        attestations.append(found)
            if len(entries) == 1:
                top = entries[0].to_oci()
                digest, media = entries[0].digest, entries[0].media_type
            else:
                body = image_index(entries)
                digest = "sha256:" + hashlib.sha256(body).hexdigest()
                media = OCI_INDEX
                _add_bytes(archive, f"blobs/sha256/{digest[7:]}", body)
                top = {"mediaType": OCI_INDEX, "digest": digest, "size": len(body)}
            top["annotations"] = {REF_NAME: tag}
            _add_bytes(
                archive,
                "index.json",
                canonical(
                    {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": [top]}
                ),
            )
        os.replace(temporary, archive_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return ExportedImage(
        name,
        f"images/{archive_path.name}",
        digest,
        media,
        tuple(summaries),
        tuple(attestations),
    )


def choose_platforms(
    name: str, available: Sequence[Any], wanted: Sequence[str] | None
) -> list[Any]:
    """The platform images to export: ``wanted`` (default amd64), or all with ``None``."""
    if wanted is None:
        return list(available)
    by_platform = {item.platform: item for item in available}
    missing = [item for item in wanted if item not in by_platform]
    if missing:
        raise ImageExportError(
            "bundle-image-platform-missing",
            f"image {name} was not built for {missing}; built: {sorted(by_platform)}",
        )
    return [by_platform[item] for item in wanted]
