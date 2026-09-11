"""Deterministic offline OCI image-layout production and bounded inspection."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import stat
import tarfile
import tempfile
import threading
from typing import Any

from piceli.artifacts.plan import (
    BuildPlan,
    canonical,
    digest,
    relative,
    validate_digest,
)

MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
LAYER = "application/vnd.oci.image.layer.v1.tar"


@dataclass(frozen=True)
class OciReceipt:
    manifest_digest: str
    config_digest: str
    platform: str
    layers: tuple[str, ...]
    bytes: int
    plan_hash: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "revision": "piceli.oci-receipt.v1",
            **asdict(self),
            "pushed": False,
            "imported": False,
        }


@dataclass(frozen=True)
class RunnableOciReceipt:
    """Validated executable image metadata; runtime readiness is separate evidence."""

    oci: OciReceipt
    entrypoint: tuple[str, ...]
    user: str

    def summary(self) -> dict[str, Any]:
        return {
            **self.oci.summary(),
            "revision": "piceli.runnable-oci-receipt.v1",
            "entrypoint": self.entrypoint,
            "user": self.user,
            "runtime_ready": False,
        }


def blob_path(layout: Path, descriptor: dict[str, Any]) -> Path:
    validate_digest(descriptor["digest"])
    return layout / "blobs/sha256" / descriptor["digest"][7:]


def _put(layout: Path, body: bytes, media: str) -> dict[str, Any]:
    desc = {"mediaType": media, "digest": digest(body), "size": len(body)}
    blob_path(layout, desc).write_bytes(body)
    return desc


class OciBuilder:
    """Assembles explicitly public files, never runs a tool or reads kubeconfig."""

    def build(
        self,
        plan: BuildPlan,
        root: Path,
        output: Path,
        *,
        cancel: threading.Event | None = None,
    ) -> OciReceipt:
        if output.exists() or output.is_symlink():
            raise ValueError("artifact destination must not exist")
        if cancel is not None and cancel.is_set():
            raise InterruptedError("artifact build cancelled")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".piceli-oci-", dir=output.parent))
        try:
            (temporary / "blobs/sha256").mkdir(parents=True)
            layer = io.BytesIO()
            with tarfile.open(
                fileobj=layer, mode="w", format=tarfile.USTAR_FORMAT
            ) as archive:
                directories = sorted(
                    {
                        str(parent)
                        for item in plan.files
                        for parent in PurePosixPath(item.destination).parents
                        if str(parent) != "."
                    }
                )
                for directory in directories:
                    info = tarfile.TarInfo(directory)
                    info.type, info.mode = tarfile.DIRTYPE, 0o755
                    archive.addfile(info)
                for item in sorted(plan.files, key=lambda entry: entry.destination):
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("artifact build cancelled")
                    value = item.source.read(root)
                    info = tarfile.TarInfo(item.destination)
                    info.size, info.mode = (
                        len(value),
                        0o755 if item.executable else 0o644,
                    )
                    archive.addfile(info, io.BytesIO(value))
            layer_desc = _put(temporary, layer.getvalue(), LAYER)
            config = {
                "architecture": plan.platform.split("/")[1],
                "os": "linux",
                "config": {
                    "Entrypoint": plan.entrypoint,
                    "User": plan.user,
                    "WorkingDir": "/",
                },
                "rootfs": {"type": "layers", "diff_ids": [layer_desc["digest"]]},
            }
            config_desc = _put(temporary, canonical(config), CONFIG)
            manifest = {
                "schemaVersion": 2,
                "mediaType": MANIFEST,
                "config": config_desc,
                "layers": [layer_desc],
            }
            manifest_desc = _put(temporary, canonical(manifest), MANIFEST)
            index = {
                "schemaVersion": 2,
                "manifests": [
                    manifest_desc
                    | {
                        "platform": {
                            "architecture": config["architecture"],
                            "os": "linux",
                        }
                    }
                ],
            }
            (temporary / "index.json").write_bytes(canonical(index))
            (temporary / "oci-layout").write_bytes(
                canonical({"imageLayoutVersion": "1.0.0"})
            )
            for item in plan.files:
                item.source.read(root)
            receipt = inspect_oci(temporary, max_bytes=plan.max_bytes + 8 * 1024 * 1024)
            if receipt.platform != plan.platform:
                raise ValueError("output platform differs from plan")
            # Publish only a complete validated layout. Caller owns destination parent.
            if cancel is not None and cancel.is_set():
                raise InterruptedError("artifact build cancelled")
            if output.exists() or output.is_symlink():
                raise ValueError("artifact destination appeared during build")
            os.rename(temporary, output)
            return OciReceipt(
                receipt.manifest_digest,
                receipt.config_digest,
                receipt.platform,
                receipt.layers,
                receipt.bytes,
                plan.plan_hash,
            )
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def _json(path: Path, maximum: int = 1_048_576) -> Any:
    if (
        path.is_symlink()
        or not stat.S_ISREG(path.stat().st_mode)
        or path.stat().st_size > maximum
    ):
        raise ValueError("invalid metadata file")
    from piceli.k8s.ops.bounds import strict_json

    return strict_json(path.read_text(), maximum)


def inspect_oci(
    layout: Path,
    *,
    max_bytes: int = 1024 * 1024 * 1024,
    runtime_layers: bool = False,
) -> OciReceipt:
    """Strict single-platform, uncompressed OCI profile; never extracts tar entries."""
    from piceli.k8s.ops.bounds import positive

    positive(max_bytes, "OCI bytes", 2 * 1024 * 1024 * 1024)
    if layout.is_symlink() or not layout.is_dir():
        raise ValueError("OCI layout must be a real directory")
    if _json(layout / "oci-layout") != {"imageLayoutVersion": "1.0.0"}:
        raise ValueError("unsupported OCI layout")
    index = _json(layout / "index.json")
    if index.get("schemaVersion") != 2 or len(index.get("manifests", [])) != 1:
        raise ValueError("exactly one image manifest required")
    total = 0

    def verify(desc: dict[str, Any], media: str) -> Path:
        nonlocal total
        path = blob_path(layout, desc)
        size = desc["size"]
        if (
            desc["mediaType"] != media
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise ValueError("unsupported OCI descriptor")
        total += size
        if (
            total > max_bytes
            or path.is_symlink()
            or path.parent.is_symlink()
            or path.parent.parent.is_symlink()
            or not path.is_file()
            or path.stat().st_size != size
        ):
            raise ValueError("OCI blob exceeds bounds or descriptor size")
        with path.open("rb") as stream:
            actual = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != desc["digest"]:
            raise ValueError("OCI blob digest mismatch")
        return path

    manifest_desc = index["manifests"][0]
    manifest = _json(verify(manifest_desc, MANIFEST))
    if (
        manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") != MANIFEST
        or not 0 < len(manifest.get("layers", [])) <= 128
    ):
        raise ValueError("invalid image manifest")
    config = _json(verify(manifest["config"], CONFIG))
    platform = f"{config['os']}/{config['architecture']}"
    if platform not in {"linux/amd64", "linux/arm64"}:
        raise ValueError("unsupported platform")
    if manifest_desc.get("platform") != {
        "os": config["os"],
        "architecture": config["architecture"],
    }:
        raise ValueError("index/config platform mismatch")
    layers = []
    for layer in manifest["layers"]:
        path = verify(layer, LAYER)
        members = 0
        with tarfile.open(path, "r:") as archive:
            for info in archive:
                members += 1
                if info.name != ".":
                    relative(info.name)
                link_is_safe = True
                if info.issym():
                    parent = PurePosixPath(info.name).parent
                    candidate = (
                        info.linkname.lstrip("/")
                        if info.linkname.startswith("/")
                        else str(parent / info.linkname)
                    )
                    resolved = posixpath.normpath(candidate)
                    link_is_safe = (
                        bool(info.linkname)
                        and len(info.linkname) <= 4096
                        and "\x00" not in info.linkname
                        and resolved != ".."
                        and not resolved.startswith("../")
                    )
                elif info.islnk():
                    try:
                        relative(info.linkname)
                    except (TypeError, ValueError):
                        link_is_safe = False
                if (
                    members > 8192
                    or not (
                        info.isfile()
                        or info.isdir()
                        or (runtime_layers and (info.issym() or info.islnk()))
                    )
                    or not link_is_safe
                    or info.size > max_bytes
                    or (not runtime_layers and info.mode & 0o6000)
                ):
                    raise ValueError("unsafe OCI layer member")
        layers.append(layer["digest"])
    if config.get("rootfs") != {"type": "layers", "diff_ids": layers}:
        raise ValueError("layer DiffIDs do not match config")
    return OciReceipt(
        manifest_desc["digest"],
        manifest["config"]["digest"],
        platform,
        tuple(layers),
        total,
    )


def inspect_runnable_oci(
    layout: Path,
    *,
    expected_platform: str | None = None,
    max_bytes: int = 1024 * 1024 * 1024,
) -> RunnableOciReceipt:
    """Validate OCI structure plus an explicit executable entrypoint and user."""
    receipt = inspect_oci(layout, max_bytes=max_bytes, runtime_layers=True)
    if expected_platform is not None and receipt.platform != expected_platform:
        raise ValueError("runnable image platform differs from requested platform")
    index = _json(layout / "index.json")
    manifest = _json(blob_path(layout, index["manifests"][0]))
    config = _json(blob_path(layout, manifest["config"]))
    runtime = config.get("config")
    if not isinstance(runtime, dict):
        raise ValueError("runnable image configuration required")
    entrypoint = runtime.get("Entrypoint")
    user = runtime.get("User")
    labels = runtime.get("Labels", {})
    if not isinstance(labels, dict):
        raise ValueError("runnable image labels must be an object")
    plan_hash = labels.get("org.piceli.plan-hash", "")
    if plan_hash:
        validate_digest(plan_hash)
    if (
        not isinstance(entrypoint, list)
        or not entrypoint
        or len(entrypoint) > 32
        or not all(isinstance(arg, str) and len(arg) <= 4096 for arg in entrypoint)
        or not entrypoint[0].startswith("/")
        or not isinstance(user, str)
        or not user
    ):
        raise ValueError("explicit runnable entrypoint and user required")
    return RunnableOciReceipt(
        OciReceipt(
            receipt.manifest_digest,
            receipt.config_digest,
            receipt.platform,
            receipt.layers,
            receipt.bytes,
            plan_hash,
        ),
        tuple(entrypoint),
        user,
    )


def unpack_oci_archive(
    archive_path: Path,
    output: Path,
    *,
    max_bytes: int = 1024 * 1024 * 1024,
) -> OciReceipt:
    """Unpack a bounded regular-file OCI tar without links or special entries."""
    from piceli.k8s.ops.bounds import positive

    positive(max_bytes, "OCI archive bytes", 2 * 1024 * 1024 * 1024)
    if (
        archive_path.is_symlink()
        or not archive_path.is_file()
        or archive_path.stat().st_size > max_bytes
        or output.exists()
        or output.is_symlink()
    ):
        raise ValueError("invalid OCI archive input or destination")
    output.mkdir(parents=True)
    total = 0
    try:
        with tarfile.open(archive_path, "r:") as archive:
            for count, info in enumerate(archive, start=1):
                if count > 8192 or not (info.isfile() or info.isdir()):
                    raise ValueError("unsafe OCI archive entry")
                name = relative(info.name)
                target = output / name
                total += info.size
                if total > max_bytes:
                    raise ValueError("OCI archive exceeds byte limit")
                if info.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(info)
                if source is None:
                    raise ValueError("missing OCI archive file")
                with target.open("xb") as destination:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
        return inspect_oci(output, max_bytes=max_bytes)
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise
