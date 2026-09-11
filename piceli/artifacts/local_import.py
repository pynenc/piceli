"""Explicit local Docker import of a validated OCI artifact, without tags or push."""
from __future__ import annotations

from dataclasses import dataclass, field
import io
import json
from pathlib import Path
import stat
import tarfile
import tempfile
import threading
import time
from typing import Any

from piceli.artifacts.oci import (
    blob_path,
    inspect_oci,
    inspect_runnable_oci,
    unpack_oci_archive,
)
from piceli.artifacts.plan import canonical, validate_digest
from piceli.artifacts.process import ProcessLimits, ToolPin, run_process


@dataclass(frozen=True)
class LocalImportGrant:
    manifest_digest: str
    socket: Path = field(repr=False)
    expires_at: float

    def __post_init__(self) -> None:
        validate_digest(self.manifest_digest)
        if not self.socket.is_absolute() or not 0 < self.expires_at < 10_000_000_000:
            raise ValueError("explicit local socket and expiry required")


@dataclass(frozen=True)
class DockerLocalImporter:
    tool: ToolPin
    socket: Path = field(repr=False)

    def preview(self, layout: Path) -> dict[str, Any]:
        receipt = inspect_oci(layout)
        return {
            "manifest_digest": receipt.manifest_digest,
            "tool_sha256": self.tool.sha256,
            "capability": "docker-unix-local-load",
            "push": False,
            "requires_explicit_import_grant": True,
        }

    def import_image(
        self,
        layout: Path,
        grant: LocalImportGrant,
        *,
        limits: ProcessLimits | None = None,
        cancel: threading.Event | None = None,
        _runtime_layers: bool = False,
    ) -> dict[str, Any]:
        receipt = (
            inspect_runnable_oci(layout).oci if _runtime_layers else inspect_oci(layout)
        )
        if (
            grant.manifest_digest != receipt.manifest_digest
            or grant.socket != self.socket
            or grant.expires_at <= time.time()
        ):
            raise ValueError("exact unexpired local-import grant required")
        if not self.socket.is_absolute() or not stat.S_ISSOCK(
            self.socket.stat().st_mode
        ):
            raise ValueError("local Docker Unix socket required")
        self.tool.verify()
        index = json.loads((layout / "index.json").read_text())
        manifest = json.loads(blob_path(layout, index["manifests"][0]).read_text())
        with tempfile.TemporaryDirectory(prefix="piceli-local-import-") as directory:
            archive_path = Path(directory) / "image.tar"
            # Docker's legacy store also accepts this transport. The image config and
            # uncompressed layer bytes remain exactly those of the verified OCI layout.
            config_name = receipt.config_digest[7:] + ".json"
            layer_names = [layer[7:] + ".tar" for layer in receipt.layers]
            transport = [{"Config": config_name, "RepoTags": [], "Layers": layer_names}]
            with tarfile.open(
                archive_path, "w", format=tarfile.USTAR_FORMAT
            ) as archive:
                raw = canonical(transport)
                info = tarfile.TarInfo("manifest.json")
                info.size = len(raw)
                archive.addfile(info, io.BytesIO(raw))
                for desc, name in [
                    (manifest["config"], config_name),
                    *zip(manifest["layers"], layer_names),
                ]:
                    archive.add(blob_path(layout, desc), arcname=name, recursive=False)
            checked = (
                inspect_runnable_oci(layout).oci
                if _runtime_layers
                else inspect_oci(layout)
            )
            if checked != receipt:
                raise ValueError("OCI source changed during import preparation")
            result = run_process(
                [
                    str(self.tool.path),
                    "--host",
                    f"unix://{self.socket}",
                    "image",
                    "load",
                    "--input",
                    str(archive_path),
                ],
                Path(directory),
                limits or ProcessLimits(),
                cancel=cancel,
                expires_at=grant.expires_at,
            )
        self.tool.verify()
        return {
            "manifest_digest": receipt.manifest_digest,
            "image_id": receipt.config_digest,
            "platform": receipt.platform,
            "pushed": False,
            "imported": result["state"] == "succeeded",
            **result,
        }

    def import_runnable_image(
        self,
        layout: Path,
        grant: LocalImportGrant,
        *,
        limits: ProcessLimits | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Import a runnable runtime-closure layout after stricter metadata checks."""
        return self.import_image(
            layout,
            grant,
            limits=limits,
            cancel=cancel,
            _runtime_layers=True,
        )

    def import_archive(
        self,
        archive: Path,
        grant: LocalImportGrant,
        *,
        limits: ProcessLimits | None = None,
        cancel: threading.Event | None = None,
        max_bytes: int = 1024 * 1024 * 1024,
    ) -> dict[str, Any]:
        """Validate a bounded OCI tar, then use the same exact import grant."""
        with tempfile.TemporaryDirectory(prefix="piceli-oci-archive-") as directory:
            layout = Path(directory) / "oci"
            receipt = unpack_oci_archive(archive, layout, max_bytes=max_bytes)
            if receipt.manifest_digest != grant.manifest_digest:
                raise ValueError("exact OCI archive manifest grant required")
            return self.import_image(layout, grant, limits=limits, cancel=cancel)
