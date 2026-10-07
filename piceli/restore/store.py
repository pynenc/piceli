"""Restore points on the local disk: archives, records, verification.

Layout, all owner-only (``0700`` directories, ``0600`` files)::

    <directory>/<point id>/record.json         piceli.restore-point.v1
    <directory>/<point id>/<nn>-<claim>.tar.gz one gzip tarball per claim

An archive is written to ``.<name>.partial`` (exclusive create) while it
streams, hashed on the way, fsynced and renamed. Verification reads it back:
the SHA-256 must match the record, every member must be a safe relative
path (no absolute names, no ``..``, no devices), and the *content digest*,
the SHA-256 of ``"<sha256>  ./<path>\\n"`` lines of every regular file sorted
by path, must equal the one computed in the cluster from the claim itself.
Nothing here prints or returns file contents.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tarfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from piceli.restore.model import RestorePointError

RECORD_SCHEMA = "piceli.restore-point.v1"
RECORD = "record.json"
_ID = re.compile(r"rp-[0-9]{8}t[0-9]{6}z-[0-9a-f]{6}")
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")
_CHUNK = 1024 * 1024


def new_point_id(at: datetime | None = None) -> str:
    """``rp-<UTC time>-<6 hex>``: sortable, unique, safe as a file name."""
    moment = (at or datetime.now(UTC)).strftime("%Y%m%dt%H%M%Sz")
    return f"rp-{moment}-{secrets.token_hex(3)}"


def valid_point_id(value: str) -> bool:
    return bool(_ID.fullmatch(value))


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _fsync_dir(path: Path) -> None:
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def archive_name(index: int, claim: str) -> str:
    return f"{index:02d}-{_SAFE.sub('_', claim)}.tar.gz"


class ArchiveWriter:
    """Stream one archive to disk, hashing it; :meth:`commit` publishes it.

    Use as a context manager: leaving without :meth:`commit` removes the
    partial file (also on an exception or interrupt).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.partial = path.with_name(f".{path.name}.partial")
        self.sha256 = hashlib.sha256()
        self.bytes = 0
        self._stream: Any = None
        self.committed = False

    def __enter__(self) -> ArchiveWriter:
        private_dir(self.path.parent)
        if self.path.exists():
            raise RestorePointError(
                "restore-point-exists",
                "an archive with this name already exists; restore points are "
                "never overwritten",
            )
        descriptor = os.open(self.partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._stream = os.fdopen(descriptor, "wb")
        return self

    def write(self, chunk: bytes) -> None:
        self._stream.write(chunk)
        self.sha256.update(chunk)
        self.bytes += len(chunk)

    def commit(self) -> str:
        """Fsync, rename into place; return the SHA-256 (hex)."""
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.close()
        os.replace(self.partial, self.path)
        _fsync_dir(self.path.parent)
        self.committed = True
        return self.sha256.hexdigest()

    def __exit__(self, *_exc: Any) -> None:
        if self._stream is not None and not self._stream.closed:
            self._stream.close()
        if not self.committed:
            self.partial.unlink(missing_ok=True)


def file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(_CHUNK), b""):
            value.update(block)
    return value.hexdigest()


def _member_name(name: str) -> str:
    """``./relative/path`` for a tar member; refuses unsafe names."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise RestorePointError(
            "restore-point-archive-invalid",
            "the archive holds an absolute or parent-relative path",
        )
    parts = [part for part in path.parts if part not in {".", ""}]
    return "./" + "/".join(parts) if parts else "."


def inspect_archive(path: Path) -> dict[str, Any]:
    """List an archive and compute its content digest (see the module doc).

    :raises RestorePointError: ``restore-point-archive-invalid`` when the
        archive cannot be read or holds an unsafe member.
    """
    lines: list[tuple[str, str]] = []
    entries = files = size = 0
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive:
                entries += 1
                name = _member_name(member.name)
                if member.islnk():
                    _member_name(member.linkname)
                if not (
                    member.isreg() or member.isdir() or member.issym() or member.islnk()
                ):
                    raise RestorePointError(
                        "restore-point-archive-invalid",
                        "the archive holds a device, FIFO or other special file",
                    )
                if not (member.isreg() or member.islnk()):
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise RestorePointError(
                        "restore-point-archive-invalid",
                        "an archive member cannot be read",
                    )
                digest = hashlib.sha256()
                with source:
                    while block := source.read(_CHUNK):
                        digest.update(block)
                        size += len(block)
                files += 1
                lines.append((name, digest.hexdigest()))
    except (tarfile.TarError, OSError, EOFError) as error:
        raise RestorePointError(
            "restore-point-archive-invalid",
            f"the archive cannot be read ({type(error).__name__})",
        ) from None
    content = hashlib.sha256()
    for name, digest in sorted(lines, key=lambda item: item[0].encode()):
        content.update(f"{digest}  {name}\n".encode())
    return {
        "entries": entries,
        "files": files,
        "file_bytes": size,
        "content_sha256": content.hexdigest(),
    }


def verify_claim(directory: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Read one recorded archive back and check it (see the module doc).

    :raises RestorePointError: ``restore-point-archive-missing``,
        ``restore-point-checksum-mismatch`` or ``restore-point-archive-invalid``.
    """
    path = directory / str(entry["archive"])
    if not path.is_file() or path.is_symlink():
        raise RestorePointError(
            "restore-point-archive-missing",
            f"the archive of claim {entry['claim']} is missing",
        )
    if file_sha256(path) != entry["sha256"]:
        raise RestorePointError(
            "restore-point-checksum-mismatch",
            f"the archive of claim {entry['claim']} does not match its SHA-256",
        )
    listing = inspect_archive(path)
    expected = entry.get("content_sha256")
    if expected is not None and listing["content_sha256"] != expected:
        raise RestorePointError(
            "restore-point-checksum-mismatch",
            f"the archive of claim {entry['claim']} does not hold the claim's "
            "content (content digest differs from the one computed in the cluster)",
        )
    return listing


def write_record(directory: Path, record: dict[str, Any]) -> Path:
    """Write ``record.json`` atomically (0600) in the point's directory."""
    private_dir(directory)
    path = directory / RECORD
    partial = directory / f".{RECORD}.partial"
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    _fsync_dir(directory)
    return path


def load_record(root: Path, point: str) -> tuple[Path, dict[str, Any]]:
    """The directory and record of restore point ``point``.

    :raises RestorePointError: ``restore-point-unknown``.
    """
    if not valid_point_id(point):
        raise RestorePointError(
            "restore-point-unknown", "not a restore point id (rp-<time>-<hex>)"
        )
    directory = root / point
    path = directory / RECORD
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        raise RestorePointError(
            "restore-point-unknown", f"no restore point {point} in the directory"
        ) from None
    if not isinstance(record, dict) or record.get("schema") != RECORD_SCHEMA:
        raise RestorePointError(
            "restore-point-unknown", f"restore point {point} has no valid record"
        )
    return directory, record


def points(root: Path) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Every restore point under ``root``, newest first (unreadable ones skipped)."""
    if not root.is_dir():
        return
    for child in sorted(root.iterdir(), reverse=True):
        if not child.is_dir() or not valid_point_id(child.name):
            continue
        try:
            yield load_record(root, child.name)
        except RestorePointError:
            continue


def replicas_before(root: Path) -> dict[str, int]:
    """Writers' replica counts recorded by restore points that never ended.

    A point still ``running`` belongs to a process that died mid-copy (a
    killed controller): its writers may still be at zero replicas. The
    newest positive count of each writer (``Kind/name``) wins.
    """
    found: dict[str, int] = {}
    for _directory, record in points(root):
        if record.get("state") != "running":
            continue
        for item in record.get("writers") or ():
            if not isinstance(item, dict):
                continue
            workload, replicas = item.get("workload"), item.get("replicas")
            if isinstance(workload, str) and isinstance(replicas, int) and replicas:
                found.setdefault(workload, replicas)
    return found
