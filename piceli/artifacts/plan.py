"""Immutable, JSON-readable build inputs. Planning never imports repository code."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from piceli.k8s.ops.bounds import object_keys, positive, text


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def validate_digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
        raise ValueError("expected an immutable sha256 digest")
    return value


def relative(value: str) -> str:
    text(value, "relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or str(path) != value
        or value == "."
        or "\\" in value
    ):
        raise ValueError("expected canonical relative POSIX path")
    return value


def public_path(value: str) -> None:
    relative(value)
    if any(
        part in {".git", ".ssh", ".kube", "secrets"}
        or part == ".env"
        or part.startswith(".env.")
        or part.endswith((".key", ".pem"))
        for part in PurePosixPath(value).parts
    ):
        raise ValueError("private input path cannot be packaged")


def read_public(root: Path, path: str, maximum: int) -> bytes:
    """Reject links/devices, including parent links; read a bounded regular file."""
    public_path(path)
    root = root.resolve(strict=True)
    parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # Relative directory descriptors prevent parent-symlink swap races.
        for part in PurePosixPath(path).parts[:-1]:
            try:
                child_fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
                )
            except OSError as error:
                raise ValueError("source parent must be a real directory") from error
            os.close(parent_fd)
            parent_fd = child_fd
        fd = os.open(
            PurePosixPath(path).name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)
    with os.fdopen(fd, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ValueError("source is not a bounded regular file")
        value = source.read(maximum + 1)
    if len(value) > maximum:
        raise ValueError("source exceeds byte limit")
    return value


@dataclass(frozen=True)
class SourcePin:
    path: str
    sha256: str
    size: int
    public: bool

    def __post_init__(self) -> None:
        public_path(self.path)
        validate_digest(self.sha256)
        if (
            not isinstance(self.size, int)
            or isinstance(self.size, bool)
            or not 0 <= self.size <= 256 * 1024 * 1024
            or self.public is not True
        ):
            raise ValueError("only explicitly public bounded inputs may be packaged")

    @classmethod
    def capture(cls, root: Path, path: str, *, public: bool = False) -> SourcePin:
        if public is not True:
            raise ValueError("explicit public source classification required")
        raw = read_public(root, path, 256 * 1024 * 1024)
        return cls(path, digest(raw), len(raw), True)

    def read(self, root: Path) -> bytes:
        raw = read_public(root, self.path, self.size)
        if len(raw) != self.size or digest(raw) != self.sha256:
            raise ValueError("source pin changed")
        return raw


@dataclass(frozen=True)
class ArtifactFile:
    source: SourcePin
    destination: str
    executable: bool = False

    def __post_init__(self) -> None:
        public_path(self.destination)
        if not isinstance(self.source, SourcePin) or not isinstance(
            self.executable, bool
        ):
            raise ValueError("invalid artifact file")


@dataclass(frozen=True)
class BuildPlan:
    """A pinned file-assembly plan, independent of any builder or infrastructure."""

    platform: str
    files: tuple[ArtifactFile, ...]
    entrypoint: tuple[str, ...]
    revision: str = ""
    user: str = "65532:65532"
    max_bytes: int = 256 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.platform not in {"linux/amd64", "linux/arm64"}:
            raise ValueError("explicit linux/amd64 or linux/arm64 target required")
        if (
            not isinstance(self.files, tuple)
            or not 0 < len(self.files) <= 4096
            or not all(isinstance(item, ArtifactFile) for item in self.files)
        ):
            raise ValueError("expected 1-4096 immutable file entries")
        destinations = [item.destination for item in self.files]
        if len(set(destinations)) != len(destinations) or any(
            any(
                str(p) in destinations
                for p in PurePosixPath(dest).parents
                if str(p) != "."
            )
            for dest in destinations
        ):
            raise ValueError("duplicate or overlapping image destinations")
        if self.revision and not re.fullmatch(
            r"[a-f0-9]{40}|[a-f0-9]{64}", self.revision
        ):
            raise ValueError("revision must be an immutable full Git object ID")
        if (
            not isinstance(self.entrypoint, tuple)
            or not self.entrypoint
            or len(self.entrypoint) > 32
        ):
            raise ValueError("explicit immutable entrypoint required")
        for arg in self.entrypoint:
            text(arg, "entrypoint")
        if (
            not self.entrypoint[0].startswith("/")
            or self.entrypoint[0][1:] not in destinations
        ):
            raise ValueError("entrypoint must name a packaged absolute file")
        if not next(
            item for item in self.files if item.destination == self.entrypoint[0][1:]
        ).executable:
            raise ValueError("entrypoint file must be executable")
        if not re.fullmatch(r"[0-9]{1,10}:[0-9]{1,10}", self.user):
            raise ValueError("explicit numeric UID:GID required")
        positive(self.max_bytes, "artifact bytes", 1024 * 1024 * 1024)
        if sum(item.source.size for item in self.files) > self.max_bytes:
            raise ValueError("artifact exceeds byte budget")

    @property
    def plan_hash(self) -> str:
        return digest(canonical(asdict(self)))

    def preview(self) -> dict[str, Any]:
        return {
            "revision": "piceli.build-plan.v1",
            "plan_hash": self.plan_hash,
            "platform": self.platform,
            "source_revision": self.revision,
            "file_count": len(self.files),
            "source_bytes": sum(item.source.size for item in self.files),
            "network": False,
            "execute_repository_code": False,
            "push": False,
            "import": False,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BuildPlan:
        object_keys(
            value,
            {"platform", "files", "entrypoint"},
            frozenset({"revision", "user", "max_bytes"}),
        )
        if (
            not isinstance(value["files"], list)
            or len(value["files"]) > 4096
            or not isinstance(value["entrypoint"], list)
        ):
            raise ValueError("invalid build plan collections")
        files = []
        for item in value["files"]:
            object_keys(item, {"source", "destination"}, frozenset({"executable"}))
            object_keys(item["source"], {"path", "sha256", "size", "public"})
            files.append(
                ArtifactFile(
                    SourcePin(**item["source"]),
                    item["destination"],
                    item.get("executable", False),
                )
            )
        return cls(
            value["platform"],
            tuple(files),
            tuple(value["entrypoint"]),
            value.get("revision", ""),
            value.get("user", "65532:65532"),
            value.get("max_bytes", 256 * 1024 * 1024),
        )
