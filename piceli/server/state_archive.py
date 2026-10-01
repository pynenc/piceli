"""Offline, verified archive of a single UI installation's private state.

The dispatcher locks must be free. An archive contains the operation store,
evaluation evidence and release journals, but never an active token file.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

SCHEMA = "piceli.ui-state-backup.v1"
MAX_MEMBER = 512 * 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024 * 1024
MAX_FILES = 10000
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _members(directory: Path) -> list[Path]:
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("UI control directory does not exist")
    result: list[Path] = []
    total = 0
    for path in directory.rglob("*"):
        info = path.lstat()
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError("UI state contains an unsupported entry")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ValueError("UI state has another owner")
        if path.is_file() and not path.name.endswith(".runner.lock"):
            if info.st_size > MAX_MEMBER:
                raise ValueError("UI state member exceeds the backup limit")
            result.append(path)
            total += info.st_size
            if len(result) > MAX_FILES or total > MAX_TOTAL:
                raise ValueError("UI state exceeds the backup limit")
    return sorted(result)


def backup(directory: Path, output: Path) -> Path:
    """Copy an offline UI control directory to a mode-0600 verified archive."""
    directory = directory.absolute()
    output = output.absolute()
    if output.exists() or output.is_relative_to(directory):
        raise ValueError("backup output exists or is inside UI state")
    databases = [
        name
        for name in ("operations.sqlite3", "pipeline-control.sqlite3")
        if (directory / name).is_file()
    ]
    if not databases:
        raise ValueError("UI operation store does not exist")
    with ExitStack() as stack:
        # The running server holds this lock for its lifetime. A second server
        # and an offline backup cannot observe a half-written journal.
        for database in databases:
            path = directory / f"{database}.runner.lock"
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            handle = stack.enter_context(os.fdopen(descriptor, "rb+"))
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(
                    "UI dispatcher is running; stop it before backup"
                ) from None
        files = _members(directory)
        with tempfile.TemporaryDirectory(
            prefix="piceli-ui-backup-", dir=output.parent
        ) as scratch:
            staged = Path(scratch) / "state.tar.gz"
            manifest: dict[str, object] = {"schema": SCHEMA, "files": {}}
            with tarfile.open(staged, "w:gz") as archive:
                for path in files:
                    relative = path.relative_to(directory).as_posix()
                    raw = path.read_bytes()
                    manifest["files"][relative] = hashlib.sha256(raw).hexdigest()  # type: ignore[index]
                    item = tarfile.TarInfo(relative)
                    item.mode = 0o600
                    item.size = len(raw)
                    archive.addfile(item, io.BytesIO(raw))
                raw_manifest = json.dumps(manifest, sort_keys=True).encode()
                item = tarfile.TarInfo("manifest.json")
                item.mode = 0o600
                item.size = len(raw_manifest)
                archive.addfile(item, io.BytesIO(raw_manifest))
            os.chmod(staged, 0o600)
            os.link(staged, output)
    return output


def restore(archive_path: Path, destination: Path) -> Path:
    """Validate all members and digests before creating a fresh state tree."""
    destination = destination.absolute()
    if destination.exists() and (
        not destination.is_dir() or any(destination.iterdir())
    ):
        raise ValueError("restore destination must be empty")
    with tempfile.TemporaryDirectory(
        prefix="piceli-ui-restore-", dir=destination.parent
    ) as scratch:
        staging = Path(scratch) / "state"
        staging.mkdir(mode=0o700)
        with tarfile.open(archive_path, "r:gz") as archive:
            entries = archive.getmembers()
            names = [entry.name for entry in entries]
            if (
                len(names) != len(set(names))
                or names.count("manifest.json") != 1
                or len(names) > MAX_FILES + 1
                or sum(entry.size for entry in entries) > MAX_TOTAL + 1024 * 1024
            ):
                raise ValueError("UI backup has duplicate or missing members")
            manifest_info = archive.getmember("manifest.json")
            if not manifest_info.isfile() or manifest_info.size > 1024 * 1024:
                raise ValueError("UI backup manifest is invalid")
            manifest_member = archive.extractfile("manifest.json")
            if manifest_member is None:
                raise ValueError("UI backup manifest cannot be read")
            try:
                manifest = json.loads(manifest_member.read(1024 * 1024 + 1))
            except (ValueError, UnicodeDecodeError):
                raise ValueError("UI backup manifest is invalid") from None
            if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
                raise ValueError("UI backup schema is invalid")
            expected = manifest.get("files")
            if not isinstance(expected, dict) or not all(
                isinstance(name, str)
                and isinstance(value, str)
                and _DIGEST.fullmatch(value)
                for name, value in expected.items()
            ):
                raise ValueError("UI backup schema is invalid")
            if set(names) != set(expected) | {"manifest.json"}:
                raise ValueError("UI backup members differ from the manifest")
            for entry in entries:
                if entry.name == "manifest.json":
                    continue
                relative = PurePosixPath(entry.name)
                if (
                    not entry.isfile()
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or relative.as_posix() != entry.name
                    or entry.size > MAX_MEMBER
                    or entry.name.endswith(".runner.lock")
                ):
                    raise ValueError("UI backup contains an unsafe member")
                source = archive.extractfile(entry)
                if source is None:
                    raise ValueError("UI backup member cannot be read")
                raw = source.read(MAX_MEMBER + 1)
                if (
                    len(raw) != entry.size
                    or hashlib.sha256(raw).hexdigest() != expected[entry.name]
                ):
                    raise ValueError("UI backup digest mismatch")
                path = staging.joinpath(*relative.parts)
                # Every directory is private, not only the deepest one
                # (``mkdir(parents=True)`` gives the others the umask's 0755).
                for directory in reversed(path.relative_to(staging).parents[:-1]):
                    folder = staging / directory
                    if not folder.is_dir():
                        folder.mkdir(mode=0o700)
                        folder.chmod(0o700)
                path.write_bytes(raw)
                path.chmod(0o600)
        if not any(
            (staging / name).is_file()
            for name in ("operations.sqlite3", "pipeline-control.sqlite3")
        ):
            raise ValueError("UI backup has no operation store")
        if destination.exists():
            destination.rmdir()
        os.replace(staging, destination)
    return destination
