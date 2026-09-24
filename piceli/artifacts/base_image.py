"""Bounded offline composition of a pinned Docker base archive into OCI."""

from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import Any

from piceli.artifacts.oci import (
    CONFIG,
    LAYER,
    MANIFEST,
    OciReceipt,
    RunnableOciReceipt,
    inspect_runnable_oci,
)
from piceli.artifacts.plan import (
    BuildPlan,
    canonical,
    digest,
    relative,
    validate_digest,
)


def _put(layout: Path, body: bytes, media_type: str) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "mediaType": media_type,
        "digest": digest(body),
        "size": len(body),
    }
    (layout / "blobs/sha256" / descriptor["digest"][7:]).write_bytes(body)
    return descriptor


def _extract_archive(archive_path: Path, output: Path, max_bytes: int) -> None:
    if (
        archive_path.is_symlink()
        or not archive_path.is_file()
        or archive_path.stat().st_size > max_bytes
    ):
        raise ValueError("invalid bounded Docker archive")
    output.mkdir()
    total = 0
    with tarfile.open(archive_path, "r:") as archive:
        for count, info in enumerate(archive, start=1):
            if count > 8192 or not (info.isfile() or info.isdir()):
                raise ValueError("unsafe Docker archive entry")
            name = relative(info.name)
            target = output / name
            total += info.size
            if total > max_bytes:
                raise ValueError("Docker archive exceeds byte limit")
            if info.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(info)
            if source is None:
                raise ValueError("missing Docker archive file")
            with target.open("xb") as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)


def _application_layer(
    plan: BuildPlan, root: Path, cancel: threading.Event | None
) -> bytes:
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
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
        for item in sorted(plan.files, key=lambda value: value.destination):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("runnable image build cancelled")
            value = item.source.read(root)
            info = tarfile.TarInfo(item.destination)
            info.size = len(value)
            info.mode = 0o755 if item.executable else 0o644
            archive.addfile(info, io.BytesIO(value))
    return layer.getvalue()


class DockerArchiveOciBuilder:
    """Adds a pinned public application layer to a complete offline base image."""

    def build(
        self,
        plan: BuildPlan,
        root: Path,
        base_archive: Path,
        base_config_digest: str,
        output: Path,
        *,
        max_base_bytes: int = 1024 * 1024 * 1024,
        cancel: threading.Event | None = None,
    ) -> RunnableOciReceipt:
        validate_digest(base_config_digest)
        if output.exists() or output.is_symlink():
            raise ValueError("artifact destination must not exist")
        if cancel is not None and cancel.is_set():
            raise InterruptedError("runnable image build cancelled")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".piceli-runtime-", dir=output.parent))
        extracted_root = Path(tempfile.mkdtemp(prefix="piceli-docker-archive-"))
        extracted = extracted_root / "archive"
        try:
            _extract_archive(base_archive, extracted, max_base_bytes)
            manifests = json.loads((extracted / "manifest.json").read_text())
            if not isinstance(manifests, list) or len(manifests) != 1:
                raise ValueError("exactly one Docker base image required")
            record = manifests[0]
            config_path = extracted / relative(record["Config"])
            config_raw = config_path.read_bytes()
            actual_config = digest(config_raw)
            if actual_config != base_config_digest:
                raise ValueError("Docker base image identity changed")
            config = json.loads(config_raw)
            platform = f"{config.get('os')}/{config.get('architecture')}"
            if platform != plan.platform:
                raise ValueError("Docker base architecture differs from plan")
            layer_names = record.get("Layers")
            if not isinstance(layer_names, list) or not 0 < len(layer_names) <= 128:
                raise ValueError("invalid Docker base layers")

            (temporary / "blobs/sha256").mkdir(parents=True)
            layers = []
            diff_ids = []
            for name in layer_names:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("runnable image build cancelled")
                body = (extracted / relative(name)).read_bytes()
                layers.append(_put(temporary, body, LAYER))
                diff_ids.append(digest(body))
            app_layer = _application_layer(plan, root, cancel)
            layers.append(_put(temporary, app_layer, LAYER))
            diff_ids.append(digest(app_layer))

            runtime = config.setdefault("config", {})
            runtime["Entrypoint"] = list(plan.entrypoint)
            runtime["Cmd"] = None
            runtime["User"] = plan.user
            runtime["WorkingDir"] = "/"
            labels = runtime.setdefault("Labels", {})
            if not isinstance(labels, dict):
                raise ValueError("Docker base labels must be an object")
            labels["org.piceli.plan-hash"] = plan.plan_hash
            config["rootfs"] = {"type": "layers", "diff_ids": diff_ids}
            config.pop("container_config", None)
            config_descriptor = _put(temporary, canonical(config), CONFIG)
            manifest = {
                "schemaVersion": 2,
                "mediaType": MANIFEST,
                "config": config_descriptor,
                "layers": layers,
            }
            manifest_descriptor = _put(temporary, canonical(manifest), MANIFEST)
            index = {
                "schemaVersion": 2,
                "manifests": [
                    manifest_descriptor
                    | {
                        "platform": {
                            "architecture": config["architecture"],
                            "os": config["os"],
                        }
                    }
                ],
            }
            (temporary / "index.json").write_bytes(canonical(index))
            (temporary / "oci-layout").write_bytes(
                canonical({"imageLayoutVersion": "1.0.0"})
            )
            receipt = inspect_runnable_oci(
                temporary,
                expected_platform=plan.platform,
                max_bytes=max_base_bytes + plan.max_bytes + 8 * 1024 * 1024,
            )
            for item in plan.files:
                item.source.read(root)
            if cancel is not None and cancel.is_set():
                raise InterruptedError("runnable image build cancelled")
            os.rename(temporary, output)
            return RunnableOciReceipt(
                OciReceipt(
                    receipt.oci.manifest_digest,
                    receipt.oci.config_digest,
                    receipt.oci.platform,
                    receipt.oci.layers,
                    receipt.oci.bytes,
                    plan.plan_hash,
                ),
                receipt.entrypoint,
                receipt.user,
            )
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
            shutil.rmtree(extracted_root, ignore_errors=True)
