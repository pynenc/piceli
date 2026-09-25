"""Pack a state directory into one archive, and unpack it into another.

A snapshot is a gzip-compressed tar of the directory's durable files: run
journals, receipts, the release catalog, the execution journal, the secret
store, stored discovery and backups. Excluded are files that only make sense
on the machine that wrote them: lock files, temporary files (``.name.*``),
SQLite side files (``-journal``, ``-wal``, ``-shm``) and build outputs and
logs (``builds/<name>/outputs/``, ``builds/<name>/build.log``).

SQLite databases are copied with SQLite's online backup, so a snapshot taken
between two transactions of an open connection is consistent. Members are
written with fixed metadata (mode ``0600``, mtime ``0``), so the same content
packs to the same bytes and an unchanged state is never written twice.

Unpacking accepts regular files with relative, normalized names only; every
file is written owner-only (``0600`` in ``0700`` directories).

Importing this module is side-effect free.
"""

from __future__ import annotations

import gzip
import io
import os
import sqlite3
import tarfile
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath

#: File name suffixes that never leave the machine that wrote them.
LOCAL_SUFFIXES = (".lock", "-journal", "-wal", "-shm")
#: Members of a snapshot that hold secret material (values, live Secret
#: bodies in stored discovery, backups of deleted objects, execution journals
#: with private manifests). ``piceli state export`` leaves them out unless an
#: encryption key is given.
PRIVATE_NAMES = ("secrets.sqlite", "journal.sqlite")
PRIVATE_DIRECTORIES = ("backups", "plans")
PRIVATE_SUFFIXES = (".discovery.json",)
SQLITE_HEADER = b"SQLite format 3\x00"
#: Largest snapshot accepted when unpacking (uncompressed bytes).
MAX_UNPACKED_BYTES = 512 * 1024 * 1024


class SnapshotError(ValueError):
    """A snapshot could not be packed or is not a valid snapshot."""

    code = "state-corrupt"


def excluded(relative: PurePosixPath) -> bool:
    """Whether ``relative`` (a path inside a state directory) stays local."""
    parts = relative.parts
    if not parts or any(part.startswith(".") for part in parts):
        return True
    if parts[-1].endswith(LOCAL_SUFFIXES):
        return True
    # builds/<name>/outputs/… and builds/<name>/build.log: rebuilt or not needed.
    return (
        len(parts) >= 3
        and parts[0] == "builds"
        and (parts[2] == "outputs" or parts[2].endswith(".log"))
    )


def private(relative: PurePosixPath) -> bool:
    """Whether a snapshot member holds secret material (see :data:`PRIVATE_NAMES`)."""
    parts = relative.parts
    return (
        parts[-1] in PRIVATE_NAMES
        or parts[-1].endswith(PRIVATE_SUFFIXES)
        or any(part in PRIVATE_DIRECTORIES for part in parts[:-1])
    )


def members(directory: Path) -> list[PurePosixPath]:
    """Every durable regular file under ``directory``, sorted; symlinks are refused."""
    if not directory.is_dir():
        return []
    found: list[PurePosixPath] = []
    for root, folders, files in os.walk(directory):
        base = Path(root)
        folders.sort()
        for name in [*folders, *files]:
            if (base / name).is_symlink():
                raise SnapshotError(
                    "the state directory holds a symlink; refusing to snapshot it"
                )
        for name in sorted(files):
            relative = PurePosixPath((base / name).relative_to(directory).as_posix())
            if not excluded(relative) and (base / name).is_file():
                found.append(relative)
    return sorted(found)


def _read(path: Path) -> bytes:
    """The file's bytes; a SQLite database through the online backup API."""
    with path.open("rb") as stream:
        header = stream.read(len(SQLITE_HEADER))
    if header != SQLITE_HEADER:
        return path.read_bytes()
    descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", suffix=".sqlite")
    os.close(descriptor)
    try:
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            target = sqlite3.connect(temporary)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        return Path(temporary).read_bytes()
    finally:
        Path(temporary).unlink(missing_ok=True)


def pack(
    directory: Path, *, include: Iterable[PurePosixPath] | None = None
) -> tuple[bytes, list[str]]:
    """``(archive bytes, member names)`` of ``directory``'s durable files.

    ``include`` restricts the archive to those members (default: every one).
    """
    names = members(directory) if include is None else sorted(include)
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
        with tarfile.open(
            fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
        ) as tar:
            for relative in names:
                data = _read(directory / Path(*relative.parts))
                info = tarfile.TarInfo(str(relative))
                info.size = len(data)
                info.mode = 0o600
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue(), [str(name) for name in names]


def entries(data: bytes) -> Iterator[tuple[PurePosixPath, bytes]]:
    """The ``(name, content)`` of every member of a snapshot, validated."""
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for info in tar:
                name = PurePosixPath(info.name)
                if (
                    not info.isreg()
                    or name.is_absolute()
                    or not name.parts
                    or any(part in {"", ".", ".."} for part in name.parts)
                    or str(name) != info.name
                ):
                    raise SnapshotError("the snapshot holds an unsafe member")
                total += info.size
                if total > MAX_UNPACKED_BYTES:
                    raise SnapshotError("the snapshot is larger than the limit")
                stream = tar.extractfile(info)
                if stream is None:
                    raise SnapshotError("the snapshot holds an unreadable member")
                yield name, stream.read()
    except (tarfile.TarError, OSError, EOFError, gzip.BadGzipFile) as error:
        raise SnapshotError(
            f"the snapshot is not a valid archive ({type(error).__name__})"
        ) from None


def private_dir(path: Path) -> None:
    """Create ``path`` (and parents) owner-only."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _write(path: Path, data: bytes) -> None:
    private_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def clear(directory: Path) -> None:
    """Remove the durable files of ``directory`` (local-only files stay)."""
    for relative in members(directory):
        (directory / Path(*relative.parts)).unlink(missing_ok=True)
    # Empty folders left behind (never the directory itself or build outputs).
    for root, folders, _files in os.walk(directory, topdown=False):
        for name in folders:
            folder = Path(root) / name
            relative = PurePosixPath(folder.relative_to(directory).as_posix())
            if not excluded(relative) and not any(folder.iterdir()):
                folder.rmdir()


def unpack(data: bytes, directory: Path, *, replace: bool = True) -> list[str]:
    """Write a snapshot into ``directory``; returns the member names.

    With ``replace`` the directory's durable files are removed first, so the
    directory then holds exactly the snapshot (plus its local-only files).
    The archive is validated completely before anything is removed.
    """
    files = list(entries(data))
    if replace:
        clear(directory)
    for name, content in files:
        _write(directory / Path(*name.parts), content)
    return [str(name) for name, _ in files]


def copy_tree(source: Path, target: Path) -> None:
    """Copy ``source``'s durable files into ``target`` (owner-only)."""
    for relative in members(source):
        path = source / Path(*relative.parts)
        _write(target / Path(*relative.parts), _read(path))


__all__ = [
    "SnapshotError",
    "clear",
    "copy_tree",
    "entries",
    "excluded",
    "members",
    "pack",
    "private",
    "unpack",
]
