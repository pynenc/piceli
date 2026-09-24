"""Minimal-by-construction build contexts.

BuildKit applies the main ``.dockerignore`` only to the main context; named
contexts (``--build-context name=dir``) are streamed whole. A context that
points at a workspace therefore ships every virtualenv and ``target/`` tree
below it. Piceli never passes a user directory to BuildKit. It walks the
declared directory, selects only files matching the declared include globs,
enforces a file-count and byte budget, and copies exactly those files into a
private staging directory that becomes the context.

Selection policy (``piceli.build-context.v1``):

* Patterns are relative POSIX globs matched against file paths: ``*`` and
  ``?`` stay within one path component, ``**`` spans components. A pattern
  selects files only; use ``dir/**`` for a subtree.
* Heavy or VCS directories (``.git``, ``target``, ``.venv``, ``node_modules``
  and similar, see `PRUNED_DIRECTORIES`) are never entered unless an include
  pattern names them literally (e.g. ``target/release/app``). A ``**``
  pattern does not count.
* Private paths (``.git``, ``.ssh``, ``.env*``, ``*.key``, ``*.pem``,
  ``secrets``) are never staged; they are counted in the manifest.
* Symbolic links are never followed: one matching a pattern is an error,
  others (including linked directories) are skipped and counted.
* Staged files get mode 0644 or 0755 (executable bit kept) and a fixed
  modification time, so equal content gives an equal context.

Importing this module runs nothing.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from piceli.artifacts.plan import canonical, digest, public_path

CONTEXT_POLICY = "piceli.build-context.v1"

DEFAULT_MAX_FILES = 10_000
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
MAX_FILES_LIMIT = 1_000_000
MAX_BYTES_LIMIT = 16 * 1024 * 1024 * 1024
MAX_PATTERNS = 256

PRUNED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        ".direnv",
        "node_modules",
        "target",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".cargo",
        ".rustup",
    }
)

_WILDCARD = re.compile(r"[*?\[\]]")


class BuildContextError(ValueError):
    """A declared context cannot be staged within its policy.

    ``code`` is a fixed, path-free reason suitable for machine output.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _component_regex(part: str) -> str:
    out = []
    for character in part:
        if character == "*":
            out.append("[^/]*")
        elif character == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(character))
    return "".join(out)


def validate_pattern(pattern: Any) -> str:
    if (
        not isinstance(pattern, str)
        or not pattern
        or len(pattern) > 1024
        or pattern.startswith("/")
        or "\\" in pattern
        or "[" in pattern
        or "]" in pattern
        or any(ord(character) < 32 for character in pattern)
    ):
        raise BuildContextError("invalid-spec", "include patterns must be relative")
    parts = pattern.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise BuildContextError(
            "invalid-spec", "include patterns must be canonical relative globs"
        )
    if any("**" in part and part != "**" for part in parts):
        raise BuildContextError("invalid-spec", "'**' must be a whole path component")
    return pattern


@dataclass(frozen=True)
class Glob:
    """A compiled include/exclude pattern with directory pruning support."""

    pattern: str

    def __post_init__(self) -> None:
        validate_pattern(self.pattern)

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.pattern.split("/"))

    def regex(self) -> re.Pattern[str]:
        pieces: list[str] = []
        parts = self.parts
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            if part == "**":
                pieces.append(".*" if last else "(?:[^/]+/)*")
            else:
                pieces.append(_component_regex(part) + ("" if last else "/"))
        return re.compile("".join(pieces))

    def matches(self, path: str) -> bool:
        return self.regex().fullmatch(path) is not None

    def may_contain(self, directory: tuple[str, ...]) -> bool:
        """Whether a file below ``directory`` could match this pattern."""
        parts = self.parts
        for depth, name in enumerate(directory):
            if depth >= len(parts):
                return False
            part = parts[depth]
            if part == "**":
                return True
            if depth == len(parts) - 1:
                return False
            if re.fullmatch(_component_regex(part), name) is None:
                return False
        return True

    def names_literally(self, directory: tuple[str, ...]) -> bool:
        """Whether the pattern spells out ``directory`` without wildcards."""
        parts = self.parts
        if len(parts) <= len(directory):
            return False
        return all(
            not _WILDCARD.search(parts[depth]) and parts[depth] == name
            for depth, name in enumerate(directory)
        )


@dataclass(frozen=True)
class ContextFile:
    path: str
    size: int
    sha256: str
    executable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "executable": self.executable,
        }


@dataclass(frozen=True)
class ContextManifest:
    """Exactly what a staged context contains; its digest binds the build."""

    files: tuple[ContextFile, ...]
    pruned_directories: int = 0
    skipped_private: int = 0
    skipped_symlinks: int = 0

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)

    @property
    def sha256(self) -> str:
        return digest(
            canonical(
                {
                    "policy": CONTEXT_POLICY,
                    "files": [item.to_dict() for item in self.files],
                }
            )
        )

    def summary(self) -> dict[str, Any]:
        return {
            "files": len(self.files),
            "bytes": self.total_bytes,
            "sha256": self.sha256,
            "pruned_directories": self.pruned_directories,
            "skipped_private": self.skipped_private,
            "skipped_symlinks": self.skipped_symlinks,
        }


def _is_private(path: str) -> bool:
    try:
        public_path(path)
    except ValueError:
        return True
    return False


def _hash_regular(fd: int) -> tuple[str, int]:
    with os.fdopen(os.dup(fd), "rb") as stream:
        stream.seek(0)
        return (
            "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest(),
            os.fstat(fd).st_size,
        )


def _open_relative(root: Path, path: str) -> int:
    """Open ``root/path`` without following any symlink on the way."""
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in PurePosixPath(path).parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            os.close(parent)
            parent = child
        fd = os.open(
            PurePosixPath(path).name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent,
        )
    except OSError as error:
        raise BuildContextError(
            "context-changed", "context file is no longer a regular file"
        ) from error
    finally:
        os.close(parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise BuildContextError("context-changed", "context path is not a file")
    return fd


@dataclass(frozen=True)
class ContextSelection:
    """Declared selection rules for one context directory."""

    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    max_files: int = DEFAULT_MAX_FILES
    max_bytes: int = DEFAULT_MAX_BYTES

    def __post_init__(self) -> None:
        if (
            not isinstance(self.include, tuple)
            or not 0 < len(self.include) <= MAX_PATTERNS
            or not isinstance(self.exclude, tuple)
            or len(self.exclude) > MAX_PATTERNS
        ):
            raise BuildContextError(
                "invalid-spec", f"expected 1-{MAX_PATTERNS} include patterns"
            )
        for pattern in (*self.include, *self.exclude):
            validate_pattern(pattern)
        for value, limit in (
            (self.max_files, MAX_FILES_LIMIT),
            (self.max_bytes, MAX_BYTES_LIMIT),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 < value <= limit
            ):
                raise BuildContextError("invalid-spec", "invalid context budget")

    def to_dict(self) -> dict[str, Any]:
        return {
            "include": list(self.include),
            "exclude": list(self.exclude),
            "max_files": self.max_files,
            "max_bytes": self.max_bytes,
        }

    def scan(self, root: Path) -> ContextManifest:
        """Select and hash the declared files. Reads only; writes nothing."""
        if root.is_symlink() or not root.is_dir():
            raise BuildContextError(
                "context-missing", "context root must be a real directory"
            )
        includes = [Glob(item) for item in self.include]
        includes_re = [item.regex() for item in includes]
        excludes_re = [Glob(item).regex() for item in self.exclude]
        files: list[ContextFile] = []
        total = 0
        pruned = 0
        private = 0
        symlinks = 0
        pending: list[tuple[str, ...]] = [()]
        while pending:
            directory = pending.pop()
            local = root.joinpath(*directory)
            try:
                entries = sorted(os.scandir(local), key=lambda entry: entry.name)
            except OSError as error:
                raise BuildContextError(
                    "context-missing", "context directory is unreadable"
                ) from error
            for entry in entries:
                parts = (*directory, entry.name)
                path = "/".join(parts)
                if entry.is_symlink():
                    if any(item.fullmatch(path) for item in includes_re):
                        raise BuildContextError(
                            "context-symlink",
                            "context selection matches a symbolic link",
                        )
                    symlinks += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if not any(item.may_contain(parts) for item in includes):
                        continue
                    if entry.name in PRUNED_DIRECTORIES and not any(
                        item.names_literally(parts) for item in includes
                    ):
                        pruned += 1
                        continue
                    pending.append(parts)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if not any(item.fullmatch(path) for item in includes_re) or any(
                    item.fullmatch(path) for item in excludes_re
                ):
                    continue
                if _is_private(path):
                    private += 1
                    continue
                if len(files) >= self.max_files:
                    raise BuildContextError(
                        "context-budget-exceeded", "context exceeds its file budget"
                    )
                fd = _open_relative(root, path)
                try:
                    sha, size = _hash_regular(fd)
                    executable = bool(os.fstat(fd).st_mode & 0o111)
                finally:
                    os.close(fd)
                total += size
                if total > self.max_bytes:
                    raise BuildContextError(
                        "context-budget-exceeded", "context exceeds its byte budget"
                    )
                files.append(ContextFile(path, size, sha, executable))
        if not files:
            raise BuildContextError(
                "context-empty", "context include patterns matched no files"
            )
        files.sort(key=lambda item: item.path)
        return ContextManifest(tuple(files), pruned, private, symlinks)


def stage_context(
    root: Path,
    manifest: ContextManifest,
    destination: Path,
    *,
    mtime: int = 0,
) -> ContextManifest:
    """Copy exactly the manifest's files into a new ``destination`` directory.

    Each file is re-hashed while copied; any difference from ``manifest``
    (content, size, type or a new symlink) raises ``context-changed``.
    """
    destination.mkdir(mode=0o755)
    directories: set[Path] = set()
    for item in manifest.files:
        target = destination.joinpath(*PurePosixPath(item.path).parts)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        directories.update(target.parents)
        fd = _open_relative(root, item.path)
        hasher = hashlib.sha256()
        size = 0
        try:
            with (
                os.fdopen(os.dup(fd), "rb") as source,
                open(target, "xb") as sink,
            ):
                while block := source.read(1024 * 1024):
                    size += len(block)
                    if size > item.size:
                        break
                    hasher.update(block)
                    sink.write(block)
        finally:
            os.close(fd)
        if size != item.size or "sha256:" + hasher.hexdigest() != item.sha256:
            raise BuildContextError(
                "context-changed", "context file changed while staging"
            )
        os.chmod(target, 0o755 if item.executable else 0o644)
        os.utime(target, (mtime, mtime))
    for directory in sorted(directories, key=lambda path: len(path.parts)):
        if directory == destination or destination in directory.parents:
            os.utime(directory, (mtime, mtime))
    return manifest
