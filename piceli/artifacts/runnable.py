"""Explicit build, inspection and local execution of runnable OCI artifacts."""

from __future__ import annotations

import json
import shutil
import stat
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from piceli.artifacts.oci import inspect_runnable_oci, unpack_oci_archive
from piceli.artifacts.plan import validate_digest
from piceli.artifacts.process import (
    BuildCommand,
    ExecutionGrant,
    ProcessLimits,
    ToolPin,
    _run_process,
)


@dataclass(frozen=True)
class RunnableBuild:
    """A pinned external build whose only accepted product is one OCI tar."""

    command: BuildCommand
    output_archive: Path = field(repr=False)
    platform: str

    def __post_init__(self) -> None:
        if not self.output_archive.is_absolute() or self.platform not in {
            "linux/amd64",
            "linux/arm64",
        }:
            raise ValueError("absolute output and supported Linux platform required")

    def execute(
        self,
        root: Path,
        grant: ExecutionGrant,
        *,
        limits: ProcessLimits | None = None,
        cancel: threading.Event | None = None,
        max_bytes: int = 1024 * 1024 * 1024,
    ) -> dict[str, Any]:
        if self.output_archive.exists() or self.output_archive.is_symlink():
            raise ValueError("runnable image output must not exist")
        result = self.command.execute(root, grant, limits=limits, cancel=cancel)
        if result["state"] != "succeeded":
            self._discard_output()
            return result | {"runtime_ready": False, "imported": False, "pushed": False}
        temporary = Path(tempfile.mkdtemp(prefix="piceli-runnable-inspect-"))
        try:
            unpack_oci_archive(
                self.output_archive, temporary / "oci", max_bytes=max_bytes
            )
            receipt = inspect_runnable_oci(
                temporary / "oci",
                expected_platform=self.platform,
                max_bytes=max_bytes,
            )
            return result | receipt.summary()
        except Exception:
            self._discard_output()
            raise
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def _discard_output(self) -> None:
        if self.output_archive.is_symlink() or self.output_archive.is_file():
            self.output_archive.unlink(missing_ok=True)
        elif self.output_archive.is_dir():
            shutil.rmtree(self.output_archive)


@dataclass(frozen=True)
class LocalRunGrant:
    """Short-lived authority to execute one exact locally imported image."""

    image_id: str
    platform: str
    socket: Path = field(repr=False)
    expires_at: float

    def __post_init__(self) -> None:
        validate_digest(self.image_id)
        if (
            self.platform not in {"linux/amd64", "linux/arm64"}
            or not self.socket.is_absolute()
            or not 0 < self.expires_at < 10_000_000_000
        ):
            raise ValueError("exact local image, platform, socket and expiry required")


@dataclass(frozen=True)
class DockerLocalRunner:
    """Pinned local-daemon adapter with architecture checks and owned cleanup."""

    tool: ToolPin
    socket: Path = field(repr=False)

    def inspect_image(
        self,
        image_id: str,
        expected_platform: str,
        *,
        limits: ProcessLimits | None = None,
    ) -> dict[str, Any]:
        validate_digest(image_id)
        if not self.socket.is_absolute() or not stat.S_ISSOCK(
            self.socket.stat().st_mode
        ):
            raise ValueError("local Docker Unix socket required")
        self.tool.verify()
        receipt, stdout, _ = _run_process(
            self._docker("image", "inspect", image_id),
            Path("/"),
            limits or ProcessLimits(10, 1_048_576),
        )
        if receipt["state"] != "succeeded":
            raise ValueError("local image inspection failed")
        try:
            values = json.loads(stdout)
            image = values[0]
            actual_id = image["Id"]
            platform = f"{image['Os']}/{image['Architecture']}"
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("invalid local image inspection output") from error
        if actual_id != image_id or platform != expected_platform:
            raise ValueError("local image identity or architecture mismatch")
        self.tool.verify()
        return {"image_id": actual_id, "platform": platform, **receipt}

    def run_image(
        self,
        grant: LocalRunGrant,
        *,
        arguments: tuple[str, ...] = (),
        limits: ProcessLimits | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(arguments, tuple)
            or len(arguments) > 32
            or not all(
                isinstance(argument, str) and len(argument) <= 4096
                for argument in arguments
            )
        ):
            raise ValueError("bounded public runtime arguments required")
        if (
            grant.socket != self.socket
            or grant.expires_at <= time.time()
            or self.inspect_image(grant.image_id, grant.platform)["state"]
            != "succeeded"
        ):
            raise ValueError("exact unexpired local-run grant required")
        name = f"piceli-runnable-{uuid.uuid4().hex}"
        try:
            receipt, _, _ = _run_process(
                self._docker(
                    "run",
                    "--rm",
                    "--name",
                    name,
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    grant.image_id,
                    *arguments,
                ),
                Path("/"),
                limits or ProcessLimits(),
                cancel=cancel,
                expires_at=grant.expires_at,
            )
            return {
                "image_id": grant.image_id,
                "platform": grant.platform,
                "runtime_ready": receipt["state"] == "succeeded",
                "pushed": False,
                **receipt,
            }
        finally:
            _run_process(
                self._docker("container", "rm", "--force", name),
                Path("/"),
                ProcessLimits(5, 4096),
            )
            self.tool.verify()

    def _docker(self, *arguments: str) -> list[str]:
        return [
            str(self.tool.path),
            "--host",
            f"unix://{self.socket}",
            *arguments,
        ]
