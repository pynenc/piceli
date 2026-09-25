"""Git-derived identity of the source checkouts that feed a build.

A `SourceIdentity` is captured from git only: the HEAD commit, whether the
checkout differs from it, and a SHA-256 over the exact working-tree state of
every path that differs (tracked changes and untracked, non-ignored files).
Nothing depends on unversioned evidence files, so deleting a local receipt can
never break or fake verification.

Dirty-state digest policy (``piceli.source-dirty.v1``):

* The changed-path set is ``git status --porcelain -z --untracked-files=all
  --no-renames`` limited to the optional subpath. Ignored files never count.
* Each path contributes its *working-tree* state, independent of the index:
  a regular file contributes its content SHA-256, size and executable bit; a
  symlink contributes its target text; a missing path contributes ``deleted``;
  a nested repository or submodule contributes its HEAD commit and a digest of
  its own status listing (declare it as a separate source to hash its content).
* The digest is SHA-256 over the canonical JSON list of those entries, sorted
  by path. It does not depend on diff settings, colour, pager, locale or
  staging, so the same bytes on disk always give the same identity.

Git runs with an explicit argv, no shell, a bounded time, ``GIT_OPTIONAL_LOCKS=0``
(read-only status) and ``core.fsmonitor=false`` (no repository-configured hook).
Importing this module runs nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from piceli.artifacts.plan import canonical, digest, relative, validate_digest
from piceli.bounds import object_keys, seconds, text

SOURCE_IDENTITY_REVISION = "piceli.source-identity.v1"
INPUTS_LOCK_REVISION = "piceli.inputs-lock.v1"
DIRTY_POLICY = "piceli.source-dirty.v1"

DEFAULT_GIT_TIMEOUT = 30.0
MAX_GIT_OUTPUT = 64 * 1024 * 1024
MAX_CHANGED_PATHS = 100_000
MAX_SOURCES = 256

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_COMMIT = re.compile(r"[a-f0-9]{40}|[a-f0-9]{64}")
# Variables that would silently point git at another repository or index.
_GIT_REDIRECTS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
)


class SourceIdentityError(ValueError):
    """A source cannot be identified or violates its declared policy.

    ``code`` is the registered error code when the failure has a specific one
    (``piceli explain``); ``None`` leaves the choice to the caller.
    """

    code: str | None = None

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class UnknownSourceError(SourceIdentityError):
    """A selection names a source the spec does not declare.

    ``code`` is the fixed, machine-readable reason ``unknown-source``.
    """

    code = "unknown-source"

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown source {name!r}")
        self.name = name


class SourceDriftError(SourceIdentityError):
    """Recorded and current source identities differ."""

    def __init__(self, message: str, verification: InputsVerification) -> None:
        super().__init__(message)
        self.verification = verification


def _name(value: Any) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise SourceIdentityError(
            "source name must be 1-128 characters of [A-Za-z0-9._-]"
        )
    return value


def _subpath(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return relative(value)
    except ValueError as error:
        raise SourceIdentityError(
            f"subpath must be a canonical relative POSIX path: {value!r}"
        ) from error


def _optional_digest(value: Any) -> str | None:
    return None if value is None else validate_digest(value)


def redact_remote(url: str) -> str:
    """Drop credentials from a remote URL; keep scp-style ``user@host:path``."""
    parts = urlsplit(url)
    if not parts.scheme or "@" not in parts.netloc:
        return url
    userinfo, host = parts.netloc.rsplit("@", 1)
    user = userinfo.split(":", 1)[0]
    # Tokens are often sent as the HTTP user name, so only ssh keeps a user.
    netloc = f"{user}@{host}" if parts.scheme == "ssh" and user else host
    return urlunsplit(parts._replace(netloc=netloc))


@dataclass(frozen=True)
class SourceSpec:
    """One declared source checkout.

    ``path`` is resolved against the spec directory and must be the top level
    of a git work tree; use ``subpath`` to restrict identity to a directory.
    ``ref`` is an optional required commit, tag or branch that HEAD must match.
    """

    name: str
    path: str
    ref: str | None = None
    allow_dirty: bool = False
    subpath: str | None = None

    def __post_init__(self) -> None:
        _name(self.name)
        text(self.path, "source path")
        if self.ref is not None:
            text(self.ref, "source ref")
            if self.ref.startswith("-"):
                raise SourceIdentityError("source ref cannot start with '-'")
        if not isinstance(self.allow_dirty, bool):
            raise SourceIdentityError("allow_dirty must be a boolean")
        _subpath(self.subpath)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "ref": self.ref,
            "allow_dirty": self.allow_dirty,
            "subpath": self.subpath,
        }


@dataclass(frozen=True)
class InputsSpec:
    """Declared sources plus the directory their relative paths resolve from."""

    sources: tuple[SourceSpec, ...]
    base: Path = field(default=Path("."), compare=False)
    roots: Mapping[str, Path] = field(default_factory=dict, compare=False)
    """Checkout directories that replace a source's declared path (by name).

    ``piceli deploy --ref`` materialises a source at a commit in a temporary
    worktree and reads it from there; the declaration (and its digest) is
    unchanged, so identities still record the declared path.
    """

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sources, tuple)
            or not 0 < len(self.sources) <= MAX_SOURCES
            or not all(isinstance(item, SourceSpec) for item in self.sources)
        ):
            raise SourceIdentityError(f"expected 1-{MAX_SOURCES} declared sources")
        names = [item.name for item in self.sources]
        if len(set(names)) != len(names):
            raise SourceIdentityError("source names must be unique")

    @property
    def spec_sha256(self) -> str:
        """Digest of the normalised declaration (not of the file's bytes)."""
        return digest(canonical([item.to_dict() for item in self.sources]))

    def select(self, only: Iterable[str] | None) -> tuple[SourceSpec, ...]:
        """The declared sources named in ``only`` (all when ``None``).

        Order follows the declaration; an unknown name raises
        `UnknownSourceError`.
        """
        if only is None:
            return self.sources
        wanted = set(only)
        known = {item.name for item in self.sources}
        for name in sorted(wanted - known):
            raise UnknownSourceError(name)
        if not wanted:
            raise SourceIdentityError("select at least one source")
        return tuple(item for item in self.sources if item.name in wanted)

    def resolve(self, source: SourceSpec) -> Path:
        """Where ``source`` is read: its checkout root when one is set, else its path."""
        root = self.roots.get(source.name)
        return root if root is not None else self.declared(source)

    def declared(self, source: SourceSpec) -> Path:
        """The declared location of ``source`` (ignores ``roots``)."""
        path = Path(source.path).expanduser()
        return path if path.is_absolute() else self.base / path

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], base: Path) -> InputsSpec:
        """Parse ``{"source": [{name, path, ref?, allow_dirty?, subpath?}, ...]}``."""
        try:
            object_keys(dict(value), {"source"})
            items = value["source"]
            if not isinstance(items, list):
                raise ValueError
            for item in items:
                object_keys(
                    item,
                    {"name", "path"},
                    frozenset({"ref", "allow_dirty", "subpath"}),
                )
        except ValueError:
            raise SourceIdentityError(
                "inputs spec must contain only [[source]] tables with name, path "
                "and optional ref, allow_dirty, subpath"
            ) from None
        return cls(tuple(SourceSpec(**item) for item in items), base)

    @classmethod
    def from_toml(cls, path: Path) -> InputsSpec:
        """Load a spec file; relative source paths resolve from its directory."""
        try:
            with path.open("rb") as stream:
                document = tomllib.load(stream)
        except tomllib.TOMLDecodeError as error:
            raise SourceIdentityError(f"invalid inputs spec TOML: {error}") from None
        return cls.from_dict(document, path.resolve().parent)


@dataclass(frozen=True)
class SourceIdentity:
    """What a build consumed from one source checkout, derived only from git."""

    name: str
    path: str
    repository: str
    commit: str
    dirty: bool
    diff_sha256: str | None
    changed_paths: int = 0
    remote_url: str | None = None
    subpath: str | None = None

    def __post_init__(self) -> None:
        _name(self.name)
        text(self.path, "source path")
        text(self.repository, "repository")
        if not isinstance(self.commit, str) or not _COMMIT.fullmatch(self.commit):
            raise SourceIdentityError("commit must be a full git object id")
        if not isinstance(self.dirty, bool):
            raise SourceIdentityError("dirty must be a boolean")
        _optional_digest(self.diff_sha256)
        if self.dirty != (self.diff_sha256 is not None):
            raise SourceIdentityError("a dirty identity needs a diff digest")
        if (
            not isinstance(self.changed_paths, int)
            or isinstance(self.changed_paths, bool)
            or not 0 <= self.changed_paths <= MAX_CHANGED_PATHS
            or (self.changed_paths > 0) != self.dirty
        ):
            raise SourceIdentityError("invalid changed path count")
        if self.remote_url is not None:
            text(self.remote_url, "remote url")
        _subpath(self.subpath)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "repository": self.repository,
            "remote_url": self.remote_url,
            "commit": self.commit,
            "dirty": self.dirty,
            "diff_sha256": self.diff_sha256,
            "changed_paths": self.changed_paths,
            "subpath": self.subpath,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceIdentity:
        try:
            object_keys(
                dict(value),
                {"name", "path", "repository", "commit", "dirty", "diff_sha256"},
                frozenset({"changed_paths", "remote_url", "subpath"}),
            )
        except ValueError:
            raise SourceIdentityError("invalid source identity fields") from None
        return cls(**value)


# Fields that define "the same source". Location and remote are informational.
IDENTITY_FIELDS = ("commit", "dirty", "diff_sha256", "subpath")


@dataclass(frozen=True)
class SourceDrift:
    name: str
    field: str
    expected: Any
    actual: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass(frozen=True)
class InputsVerification:
    expected: InputsLock
    actual: InputsLock
    drifts: tuple[SourceDrift, ...]

    @property
    def ok(self) -> bool:
        return not self.drifts

    def summary(self) -> str:
        if self.ok:
            return "all sources match"
        return "; ".join(
            f"{item.name}: {item.field} {item.expected!r} -> {item.actual!r}"
            for item in self.drifts
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": "piceli.inputs-verification.v1",
            "state": "verified" if self.ok else "drift",
            "spec_sha256": self.actual.spec_sha256,
            "drifts": [item.to_dict() for item in self.drifts],
            "sources": [item.to_dict() for item in self.actual.sources],
            **(
                {"only": list(self.actual.only)} if self.actual.only is not None else {}
            ),
        }

    def raise_for_drift(self, context: str = "sources changed") -> None:
        if not self.ok:
            raise SourceDriftError(f"{context}: {self.summary()}", self)


@dataclass(frozen=True)
class InputsLock:
    """Identities of every declared source, bound to the declaration digest."""

    spec_sha256: str
    sources: tuple[SourceIdentity, ...]
    only: tuple[str, ...] | None = None
    """Set when the lock covers a selection (``--only``) of the declaration."""

    def __post_init__(self) -> None:
        validate_digest(self.spec_sha256)
        if self.only is not None and (
            not isinstance(self.only, tuple)
            or tuple(item.name for item in self.sources) != self.only
        ):
            raise SourceIdentityError("a partial lock lists exactly its sources")
        if (
            not isinstance(self.sources, tuple)
            or not 0 < len(self.sources) <= MAX_SOURCES
            or not all(isinstance(item, SourceIdentity) for item in self.sources)
        ):
            raise SourceIdentityError("expected declared source identities")
        names = [item.name for item in self.sources]
        if len(set(names)) != len(names):
            raise SourceIdentityError("source identity names must be unique")

    def source(self, name: str) -> SourceIdentity:
        for item in self.sources:
            if item.name == name:
                return item
        raise KeyError(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": INPUTS_LOCK_REVISION,
            "identity_revision": SOURCE_IDENTITY_REVISION,
            "dirty_policy": DIRTY_POLICY,
            "spec_sha256": self.spec_sha256,
            "sources": [item.to_dict() for item in self.sources],
            **({"only": list(self.only)} if self.only is not None else {}),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> InputsLock:
        try:
            object_keys(
                dict(value),
                {"revision", "spec_sha256", "sources"},
                frozenset({"identity_revision", "dirty_policy", "only"}),
            )
        except ValueError:
            raise SourceIdentityError("invalid inputs lock fields") from None
        only = value.get("only")
        if only is not None and (
            not isinstance(only, list) or not all(isinstance(i, str) for i in only)
        ):
            raise SourceIdentityError("inputs lock 'only' must list source names")
        if (
            value["revision"] != INPUTS_LOCK_REVISION
            or value.get("identity_revision", SOURCE_IDENTITY_REVISION)
            != SOURCE_IDENTITY_REVISION
            or value.get("dirty_policy", DIRTY_POLICY) != DIRTY_POLICY
        ):
            raise SourceIdentityError("unsupported inputs lock revision")
        if not isinstance(value["sources"], list):
            raise SourceIdentityError("inputs lock sources must be a list")
        return cls(
            value["spec_sha256"],
            tuple(SourceIdentity.from_dict(item) for item in value["sources"]),
            tuple(only) if only is not None else None,
        )

    @classmethod
    def from_json(cls, raw: str) -> InputsLock:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise SourceIdentityError(f"invalid inputs lock JSON: {error}") from None
        if not isinstance(value, dict):
            raise SourceIdentityError("inputs lock must be a JSON object")
        return cls.from_dict(value)


# --- git ---------------------------------------------------------------------


def _git(repo: Path, *args: str, timeout: float, check: bool = True) -> bytes:
    executable = shutil.which("git")
    if executable is None:
        raise SourceIdentityError(
            "git executable not found on PATH", code="git-unavailable"
        )
    env = {key: value for key, value in os.environ.items() if key not in _GIT_REDIRECTS}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"})
    argv = [
        executable,
        "-C",
        str(repo),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.quotePath=false",
        *args,
    ]
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SourceIdentityError(
            f"git {args[0]} timed out after {timeout:g}s in {repo}",
            code="git-timed-out",
        ) from None
    if len(result.stdout) > MAX_GIT_OUTPUT:
        raise SourceIdentityError(f"git {args[0]} output exceeds budget in {repo}")
    if check and result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip().splitlines()
        reason = detail[-1][:300] if detail else f"exit {result.returncode}"
        raise SourceIdentityError(f"git {args[0]} failed in {repo}: {reason}")
    return result.stdout if result.returncode == 0 else b""


def _changed_paths(repo: Path, subpath: str | None, timeout: float) -> list[str]:
    raw = _git(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
        "--no-renames",
        *(("--", f":(top,literal){subpath}") if subpath else ()),
        timeout=timeout,
    )
    paths = sorted(
        {entry[3:].decode("utf-8", "surrogateescape") for entry in raw.split(b"\0")}
        - {""}
    )
    if len(paths) > MAX_CHANGED_PATHS:
        raise SourceIdentityError(f"too many changed paths in {repo}")
    return paths


def _file_sha256(path: Path) -> tuple[str, int]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise SourceIdentityError(f"changed path is not a regular file: {path}")
        return (
            "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest(),
            metadata.st_size,
        )


def _path_state(repo: Path, path: str, timeout: float) -> dict[str, Any]:
    """Working-tree state of one changed path, following the documented policy."""
    local = repo / PurePosixPath(path.rstrip("/"))
    try:
        metadata = os.lstat(local)
    except FileNotFoundError:
        return {"path": path, "state": "deleted"}
    if stat.S_ISLNK(metadata.st_mode):
        target = os.readlink(local).encode("utf-8", "surrogateescape")
        return {"path": path, "state": "symlink", "sha256": digest(target)}
    if stat.S_ISREG(metadata.st_mode):
        sha, size = _file_sha256(local)
        return {
            "path": path,
            "state": "file",
            "sha256": sha,
            "size": size,
            "executable": bool(metadata.st_mode & 0o111),
        }
    if stat.S_ISDIR(metadata.st_mode):
        head = _git(
            local, "rev-parse", "--verify", "-q", "HEAD", timeout=timeout, check=False
        )
        listing = _git(
            local,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            timeout=timeout,
            check=False,
        )
        return {
            "path": path,
            "state": "repository",
            "commit": head.decode().strip() or None,
            "status_sha256": digest(listing),
        }
    raise SourceIdentityError(f"unsupported changed path type: {local}")


def capture_source_identity(
    path: Path,
    *,
    name: str,
    declared_path: str | None = None,
    subpath: str | None = None,
    timeout: float = DEFAULT_GIT_TIMEOUT,
    repository: str | None = None,
) -> SourceIdentity:
    """Capture the git identity of the work tree whose top level is ``path``.

    ``repository`` overrides the recorded repository name (a temporary
    checkout of the repository records the original's name).
    """
    seconds(timeout, "git", 600)
    _subpath(subpath)
    if not path.is_dir():
        raise SourceIdentityError(
            f"source {name!r}: {path} is not a directory", code="source-not-git"
        )
    top = _git(path, "rev-parse", "--show-toplevel", timeout=timeout).decode().strip()
    if not top or Path(top).resolve() != path.resolve():
        raise SourceIdentityError(
            f"source {name!r}: {path} is not the top level of a git work tree"
            " (declare the repository root and use subpath)",
            code="source-not-git",
        )
    repo = Path(top)
    commit = (
        _git(
            repo,
            "rev-parse",
            "--verify",
            "-q",
            "HEAD^{commit}",
            timeout=timeout,
            check=False,
        )
        .decode()
        .strip()
    )
    if not commit:
        raise SourceIdentityError(
            f"source {name!r}: {repo} has no commit at HEAD",
            code="source-has-no-commit",
        )
    if subpath and not (repo / subpath).exists():
        raise SourceIdentityError(
            f"source {name!r}: subpath {subpath!r} is missing",
            code="source-subpath-missing",
        )
    remote = _git(
        repo, "config", "--get", "remote.origin.url", timeout=timeout, check=False
    ).decode(errors="replace")
    remote = remote.strip()
    entries = [
        _path_state(repo, item, timeout)
        for item in _changed_paths(repo, subpath, timeout)
    ]
    return SourceIdentity(
        name=name,
        path=declared_path if declared_path is not None else str(path),
        repository=repository or repo.name,
        commit=commit,
        dirty=bool(entries),
        diff_sha256=digest(canonical(entries)) if entries else None,
        changed_paths=len(entries),
        remote_url=redact_remote(remote) if remote else None,
        subpath=subpath,
    )


def _resolve_ref(repo: Path, ref: str, timeout: float) -> str:
    resolved = _git(
        repo,
        "rev-parse",
        "--verify",
        "-q",
        "--end-of-options",
        f"{ref}^{{commit}}",
        timeout=timeout,
        check=False,
    )
    return resolved.decode().strip()


def _capture(
    spec: InputsSpec, source: SourceSpec, timeout: float, enforce: bool
) -> SourceIdentity:
    path = spec.resolve(source)
    identity = capture_source_identity(
        path,
        name=source.name,
        declared_path=source.path,
        subpath=source.subpath,
        timeout=timeout,
        repository=(
            spec.declared(source).resolve().name if source.name in spec.roots else None
        ),
    )
    if not enforce:
        return identity
    if identity.dirty and not source.allow_dirty:
        raise SourceIdentityError(
            f"source {source.name!r} has {identity.changed_paths} uncommitted "
            "change(s); commit them or set allow_dirty = true",
            code="source-dirty",
        )
    if source.ref is not None:
        expected = _resolve_ref(Path(path), source.ref, timeout)
        if not expected:
            raise SourceIdentityError(
                f"source {source.name!r}: required ref {source.ref!r} not found",
                code="source-ref-not-found",
            )
        if expected != identity.commit:
            raise SourceIdentityError(
                f"source {source.name!r} is at {identity.commit[:12]}, "
                f"but ref {source.ref!r} requires {expected[:12]}",
                code="source-ref-mismatch",
            )
    return identity


def record_inputs(
    spec: InputsSpec,
    *,
    timeout: float = DEFAULT_GIT_TIMEOUT,
    only: Iterable[str] | None = None,
) -> InputsLock:
    """Capture every declared source, enforcing ``allow_dirty`` and ``ref``.

    ``only`` limits the capture to the named sources; the lock is still bound
    to the digest of the whole declaration and records the selection.
    """
    selected = spec.select(only)
    return InputsLock(
        spec.spec_sha256,
        tuple(_capture(spec, item, timeout, True) for item in selected),
        None if only is None else tuple(item.name for item in selected),
    )


def compare_inputs(
    expected: InputsLock,
    actual: InputsLock,
    *,
    only: Iterable[str] | None = None,
) -> InputsVerification:
    """Compare identities field by field; location and remote are not compared.

    With ``only``, just the named sources are compared on both sides; a named
    source that one side lacks is reported with field ``present``.
    """
    wanted = None if only is None else set(only)

    def chosen(lock: InputsLock) -> list[SourceIdentity]:
        return [item for item in lock.sources if wanted is None or item.name in wanted]

    drifts: list[SourceDrift] = []
    if expected.spec_sha256 != actual.spec_sha256:
        drifts.append(
            SourceDrift("*", "spec_sha256", expected.spec_sha256, actual.spec_sha256)
        )
    current = {item.name: item for item in chosen(actual)}
    for before in chosen(expected):
        after = current.pop(before.name, None)
        if after is None:
            drifts.append(SourceDrift(before.name, "present", True, False))
            continue
        for key in IDENTITY_FIELDS:
            if getattr(before, key) != getattr(after, key):
                drifts.append(
                    SourceDrift(
                        before.name, key, getattr(before, key), getattr(after, key)
                    )
                )
    drifts.extend(SourceDrift(name, "present", False, True) for name in current)
    return InputsVerification(expected, actual, tuple(drifts))


def verify_inputs(
    spec: InputsSpec,
    lock: InputsLock,
    *,
    timeout: float = DEFAULT_GIT_TIMEOUT,
    only: Iterable[str] | None = None,
) -> InputsVerification:
    """Recapture the declared sources and compare them with a recorded lock.

    Policy (``allow_dirty``/``ref``) was enforced at record time and the lock is
    bound to the declaration digest, so a matching identity satisfies it too.
    ``only`` limits the check to the named sources; without it, a partial lock
    (recorded with ``only``) is checked for its own selection.
    """
    selection = tuple(only) if only is not None else lock.only
    selected = spec.select(selection)
    names = tuple(item.name for item in selected)
    actual = InputsLock(
        spec.spec_sha256,
        tuple(_capture(spec, item, timeout, False) for item in selected),
        None if selection is None else names,
    )
    return compare_inputs(lock, actual, only=None if selection is None else names)


def open_sources(
    spec: InputsSpec,
    lock: InputsLock | None = None,
    *,
    timeout: float = DEFAULT_GIT_TIMEOUT,
) -> InputsLock:
    """The entry half of `pinned_sources`: record, or verify against ``lock``.

    Every declared source must be covered: a partial lock (recorded with
    ``only``) is drift for the sources it does not hold.
    """
    if lock is None:
        return record_inputs(spec, timeout=timeout)
    verify_inputs(
        spec, lock, timeout=timeout, only=[item.name for item in spec.sources]
    ).raise_for_drift("sources differ from the lock")
    return lock


@contextmanager
def pinned_sources(
    spec: InputsSpec,
    lock: InputsLock | None = None,
    *,
    timeout: float = DEFAULT_GIT_TIMEOUT,
) -> Iterator[InputsLock]:
    """Bracket a build with source identity checks.

    At entry, record the sources (or verify them against ``lock``). Yield the
    identities for the build receipt. On normal exit, recapture and raise
    `SourceDriftError` if any checkout changed while the build ran. An error
    raised by the build itself propagates unchanged.
    """
    start = open_sources(spec, lock, timeout=timeout)
    yield start
    verify_inputs(spec, start, timeout=timeout).raise_for_drift(
        "source changed during the build"
    )
