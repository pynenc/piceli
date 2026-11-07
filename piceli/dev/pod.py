"""The wrapper a development-build run executes inside its pod (0.18.0).

Standard library only: the Job runs this file's source with ``python3 -I
-c``, so any image with ``python3`` works and the wrapper always matches
the client that created the run. Its input is the JSON spec in
``PICELI_DEV_SPEC``; its scratch is ``/work`` (the pod's emptyDir) and the
shared cache is ``/cache`` (the ``piceli-dev-cache`` claim).

1. Wait for the client's upload (``/work/upload/tree.tar.gz`` and
   ``ready``), check its SHA-256, extract it (no absolute or escaping
   paths).
2. Check the profile's tools, evict least recently used free lineages while
   the whole cache is over its budget or its disk is nearly full (0.35.0),
   and take a **lineage** of the run's key under an
   exclusive lock (the one this branch used last, else 0.53.0 the free one
   whose head is nearest this run's head in the mirror, else the most
   recently used, else a new one): a stable tree path and its
   ``CARGO_TARGET_DIR``. Never two runs on one lineage. 0.53.0: a new
   lineage is seeded with a copy of its nearest sibling (``tree/`` and
   ``target/``, reflinked where the filesystem can) when the copy fits the
   run's cap, so it starts warm.
3. Sync the tree into the lineage **keeping the mtime of unchanged files**,
   so cargo's fingerprints rebuild only what changed.
4. Optionally ``cargo fetch --locked``, then run the command with a deadline,
   streaming its output; parse cargo's output (crates compiled, build and
   test phases, test counts). 0.38.1: a lineage that grows past its cap
   while it runs stops the run (``dev-cache-over-cap``) and is evicted;
   0.48.0: only after evicting every free lineage did not make room.
   0.53.0: while other runs hold lineages, the youngest running run pauses
   instead (``SIGSTOP``) and resumes once room appears, and the others
   continue; only a run alone in the cache is stopped.
5. Evict to the budget again (0.38.1; 0.53.0: its report lists the
   lineages), print one ``PICELI-DEV-RESULT {json}`` line, copy requested artifacts to ``/work/out/artifacts.tar.gz``
   and wait for the client to take them.

Importing this module is side-effect free.
"""

from __future__ import annotations

import collections
import fcntl
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

try:  # in a pod: the wrapper bundles it under this name (jobs.wrapper_source)
    import piceli_dev_toolchains as toolchains  # type: ignore[import-not-found]
except ImportError:
    from piceli.dev import toolchains

RESULT_MARKER = "PICELI-DEV-RESULT "
SCHEMA = "piceli.dev-pod-result.v1"
_COMPILING = re.compile(r"^\s*Compiling \S+ v")
_FINISHED = re.compile(r"^\s*Finished `")
_TESTS = re.compile(
    r"test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored"
)
#: ``test db::slabs::merge ... FAILED`` (0.20: the failed tests by name).
_FAILED_TEST = re.compile(r"^test (\S+) \.\.\. FAILED\s*$")
#: At most this many failed test names in a result.
FAILED_NAMES = 100


class RunError(Exception):
    """The run ends here with a registered reason (no command ran, or it broke)."""

    def __init__(self, reason: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.reason = reason
        self.extra = extra


# ------------------------------------------------------------------ upload


#: An upload still writing parts keeps the run waiting this long past its
#: last part (a slow link), up to :data:`UPLOAD_HARD_SECONDS` in all.
UPLOAD_GRACE_SECONDS = 120
UPLOAD_HARD_SECONDS = 4 * 3600


def wait_upload(work: Path, spec: dict[str, Any], clock: Callable[[], float]) -> Path:
    """The uploaded archive once ``ready`` exists and its digest matches.

    While the client is still writing parts (``part-*``), the wait extends.
    """
    upload = work / "upload"
    ready = upload / "ready"
    began = clock()
    deadline = began + float(spec.get("upload_timeout_seconds") or 600)
    while not ready.exists():
        now = clock()
        newest = (
            max(
                (
                    item.stat().st_mtime
                    for item in [*upload.glob("part-*"), *upload.glob(".part-*")]
                ),
                default=None,
            )
            if upload.is_dir()
            else None
        )
        if newest is not None and time.time() - newest < UPLOAD_GRACE_SECONDS:
            deadline = max(
                deadline, min(now + UPLOAD_GRACE_SECONDS, began + UPLOAD_HARD_SECONDS)
            )
        if now >= deadline:
            raise RunError(
                "dev-upload-failed",
                "no upload arrived in time"
                if newest is None
                else "the upload stopped before it completed",
            )
        time.sleep(float(spec.get("poll_seconds") or 0.5))
    archive = work / "upload" / "tree.tar.gz"
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    expected = spec.get("archive_sha256")
    if expected is None and (upload / "tree.sha256").is_file():
        expected = (upload / "tree.sha256").read_text().strip()  # 0.19 uploads
    if digest.hexdigest() != expected:
        raise RunError("dev-upload-failed", "the upload does not match its SHA-256")
    return archive


def extract_plain(archive: Path, into: Path) -> None:
    """:func:`extract` of an uncompressed tar (the untracked files)."""
    extract(archive, into, mode="r:")


def extract(archive: Path, into: Path, mode: str = "r:gz") -> None:
    """Extract regular files, directories and in-tree symlinks only."""
    into.mkdir(parents=True, exist_ok=True)
    root = into.resolve()
    try:
        with tarfile.open(archive, mode) as tar:  # type: ignore[call-overload]
            for member in tar.getmembers():
                name = member.name
                shown = name[:300]
                if name.startswith("/") or ".." in Path(name).parts:
                    raise RunError(
                        "dev-upload-failed",
                        f"the archive holds an unsafe path: {shown}",
                        path=shown,
                    )
                if not (member.isfile() or member.isdir() or member.issym()):
                    raise RunError(
                        "dev-upload-failed",
                        f"the archive holds a special file: {shown}",
                        path=shown,
                    )
                if member.issym():
                    target = (root / Path(name).parent / member.linkname).resolve()
                    if root not in (target, *target.parents):
                        raise RunError(
                            "dev-upload-failed",
                            f"the archive holds a link out of the tree: {shown} -> "
                            f"{member.linkname[:300]} (ship sources link-free, or "
                            "pass the link's target as its own --source)",
                            path=shown,
                            link=member.linkname[:300],
                        )
            if sys.version_info >= (3, 12):  # noqa: UP036 - any image's python3
                tar.extractall(into, filter="tar")
            else:  # pragma: no cover - older interpreters in custom images
                tar.extractall(into)
    except (tarfile.TarError, OSError, EOFError) as error:
        raise RunError(
            "dev-upload-failed", f"the archive cannot be read ({type(error).__name__})"
        ) from None


# ------------------------------------------------------------------ git mirrors


def _mask(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text)[-400:]


def _git(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 600,
    input_: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, input=input_, capture_output=True,
        timeout=timeout, check=False,
    )  # fmt: skip


def _mirror(cache: Path, key: str) -> Path:
    if not re.fullmatch(r"[a-z0-9._-]{1,120}", key):
        raise RunError("dev-run-invalid", "a source key is not a safe name")
    path = cache / "git" / f"{key}.git"
    if not (path / "HEAD").exists():
        path.mkdir(parents=True, exist_ok=True)
        _git("init", "--bare", "--quiet", str(path))
    return path


class _Locked:
    """An exclusive lock on a mirror (one fetch or unbundle at a time)."""

    def __init__(self, path: Path) -> None:
        self.descriptor = os.open(path / "piceli.lock", os.O_RDWR | os.O_CREAT, 0o600)

    def __enter__(self) -> _Locked:
        fcntl.flock(self.descriptor, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc: Any) -> None:
        os.close(self.descriptor)


def _present(mirror: Path, shas: list[str]) -> list[str]:
    if not shas:
        return []
    found = _git(
        "--git-dir", str(mirror), "cat-file", "--batch-check=%(objectname) %(objecttype)",
        input_=("\n".join(f"{sha}^{{commit}}" for sha in shas) + "\n").encode(),
    )  # fmt: skip
    have = {
        line.split()[0]
        for line in found.stdout.decode().splitlines()
        if line.endswith(" commit")
    }
    return [sha for sha in shas if sha in have]


def fetch_main(
    spec: dict[str, Any], work: Path, cache: Path, secret: Path | None
) -> dict[str, Any]:
    """The init container: fetch each declared remote into its mirror, then
    write ``work/have.json``: which of each source's commits the mirror has.

    Runs before any code from the repository, with the Git credentials only
    it mounts; fetches only URLs the credentials' ``allowed`` list names (the
    composition's sources). Never fails the pod: errors are reported, masked.
    """
    work.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema": "piceli.dev-have.v1",
        "git": True,
        "sources": {},
    }
    if shutil.which("git") is None:
        report["git"] = False
    allowed: set[str] = set()
    secrets: list[str] = []
    if secret is not None and (secret / "allowed").is_file():
        allowed = {
            line.strip()
            for line in (secret / "allowed").read_text().splitlines()
            if line.strip()
        }
        secrets = [
            (secret / name).read_text().strip()
            for name in ("password",)
            if (secret / name).is_file()
        ]
    askpass = work / ".askpass"
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": str(askpass),
        "HOME": str(work / "home"),
    }
    try:
        if secret is not None and (secret / "password").is_file():
            askpass.write_text(
                "#!/bin/sh\n"
                f'case "$1" in Username*) cat "{secret}/username" ;; *) cat "{secret}/password" ;; esac\n'
            )
            os.chmod(askpass, 0o700)
        for source in spec.get("sources") or ():
            name = str(source.get("name"))
            entry: dict[str, Any] = {
                "fetched": False,
                "error": None,
                "note": None,
                "present": [],
            }
            report["sources"][name] = entry
            if not report["git"]:
                entry["note"] = "the image has no git"
                continue
            try:
                mirror = _mirror(cache, str(source.get("key")))
            except (RunError, OSError) as error:
                entry["error"] = str(error)
                continue
            url = source.get("url")
            with _Locked(mirror):
                if not url:
                    entry["note"] = "no remote the cluster may fetch"
                elif url not in allowed:
                    entry["note"] = (
                        "not a declared source of the composition: not fetched"
                    )
                else:
                    try:
                        done = _git(
                            "--git-dir", str(mirror), "-c", "credential.helper=",
                            "fetch", "--quiet", "--no-tags", "--prune", str(url),
                            "+refs/heads/*:refs/remotes/origin/*", env=env, timeout=900,
                        )  # fmt: skip
                    except subprocess.TimeoutExpired:
                        entry["error"] = "the fetch timed out"
                    else:
                        if done.returncode == 0:
                            entry["fetched"] = True
                        else:
                            entry["error"] = _mask(
                                done.stderr.decode(errors="replace").strip(), secrets
                            )
                wanted = [str(source.get("head"))] + [
                    str(c) for c in source.get("candidates") or ()
                ]
                entry["present"] = _present(mirror, wanted)
                _git("--git-dir", str(mirror), "gc", "--auto", "--quiet", timeout=600)
    finally:
        askpass.unlink(missing_ok=True)
        partial = work / ".have.json.partial"
        partial.write_text(json.dumps(report, sort_keys=True))
        os.replace(partial, work / "have.json")
    return report


def _unquote(raw: bytes) -> str:
    """A path as Git's diff headers print it (C-quoted when unusual)."""
    if not raw.startswith(b'"'):
        return raw.decode("utf-8", "surrogateescape")
    body = raw[1:-1] if raw.endswith(b'"') else raw[1:]
    escaped = body.decode("latin-1").encode("latin-1").decode("unicode_escape")
    return escaped.encode("latin-1").decode("utf-8", "surrogateescape")


class DeltaSource:
    """One source of a delta upload, its commit in the builder's mirror."""

    def __init__(self, name: str, mirror: Path, head: str, incoming: Path) -> None:
        self.name = name
        self.mirror = mirror
        self.head = head
        self.incoming = incoming

    def overlay(self) -> list[str]:
        """The paths this run's uncommitted diff and untracked files touch."""
        found: set[str] = set()
        diff = self.incoming / "diff"
        if diff.is_file():
            listed = _git("apply", "--numstat", "-z", str(diff), cwd=self.incoming)
            # "added\tdeleted\tpath\0", or for a rename "a\td\t\0old\0new\0".
            parts = (
                (listed.stdout or b"").decode("utf-8", "surrogateescape").split("\0")
            )
            index = 0
            while index < len(parts):
                record = parts[index]
                if not record:
                    index += 1
                    continue
                fields = record.split("\t", 2)
                if len(fields) == 3 and fields[2]:
                    found.add(fields[2])
                    index += 1
                elif len(fields) == 3:  # a rename: the next two are the paths
                    found.update(p for p in parts[index + 1 : index + 3] if p)
                    index += 3
                else:
                    index += 1
            # 0.50.0: --numstat names only a rename's or copy's new path; an
            # upload from an older client still carries renames.
            for line in diff.read_bytes().splitlines():
                for prefix in (b"rename from ", b"copy from "):
                    if line.startswith(prefix):
                        found.add(_unquote(line[len(prefix) :]))
        untracked = self.incoming / "untracked.tar"
        if untracked.is_file():
            with tarfile.open(untracked, "r:") as tar:
                found.update(m.name for m in tar.getmembers() if not m.isdir())
        return sorted(found)


def unbundle(inbox: Path, cache: Path, spec: dict[str, Any]) -> list[DeltaSource]:
    """Bring each source's bundle into its mirror; the sources, checked."""
    manifest = json.loads((inbox / "manifest.json").read_text())
    keys = {str(s.get("name")): str(s.get("key")) for s in spec.get("sources") or ()}
    found = []
    for name, entry in sorted((manifest.get("sources") or {}).items()):
        if name not in keys or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", name
        ):
            raise RunError(
                "dev-upload-failed", f"the upload names an unknown source {name}"
            )
        mirror = _mirror(cache, keys[name])
        head = str(entry.get("head"))
        incoming = inbox / "sources" / name
        with _Locked(mirror):
            if (incoming / "bundle").is_file():
                done = _git(
                    "--git-dir",
                    str(mirror),
                    "bundle",
                    "unbundle",
                    str(incoming / "bundle"),
                )
                if done.returncode != 0:
                    raise RunError(
                        "dev-upload-failed",
                        f"the bundle of {name} does not apply to the builder's mirror",
                    )
            if not _present(mirror, [head]):
                raise RunError(
                    "dev-upload-failed",
                    f"the builder's mirror lacks commit {head[:12]} of {name}",
                )
            _git("--git-dir", str(mirror), "update-ref", f"refs/piceli/h/{head}", head)
        found.append(DeltaSource(name, mirror, head, incoming))
    return found


def _archive_into(source: DeltaSource, target: Path, paths: list[str] | None) -> None:
    """``git archive`` of the head (only ``paths``, when given) into ``target``."""
    target.mkdir(parents=True, exist_ok=True)
    if paths is not None and not paths:
        return
    archive = subprocess.Popen(
        ["git", "--git-dir", str(source.mirror), "archive", "--format=tar",
         source.head, *(["--", *paths] if paths else [])],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    assert archive.stdout is not None
    with tarfile.open(fileobj=archive.stdout, mode="r|") as stream:
        if sys.version_info >= (3, 12):  # noqa: UP036 - any image's python3
            stream.extractall(target, filter="tar")
        else:  # pragma: no cover
            stream.extractall(target)
    if archive.wait() != 0:
        raise RunError("dev-upload-failed", f"git archive of {source.name} failed")


def _overlay_into(source: DeltaSource, target: Path) -> None:
    """Apply the run's uncommitted diff and untracked files in ``target``."""
    if (source.incoming / "diff").is_file():
        done = _git(
            "apply",
            "--binary",
            "--whitespace=nowarn",
            str(source.incoming / "diff"),
            cwd=target,
        )
        if done.returncode != 0:
            raise RunError(
                "dev-upload-failed",
                f"the uncommitted changes of {source.name} do not apply",
            )
    if (source.incoming / "untracked.tar").is_file():
        extract_plain(source.incoming / "untracked.tar", target)


def _in_head(source: DeltaSource, paths: list[str]) -> list[str]:
    """Which of ``paths`` exist in the head's tree."""
    if not paths:
        return []
    listed = _git(
        "--git-dir", str(source.mirror), "ls-tree", "-r", "-z", "--name-only",
        source.head, "--", *paths,
    )  # fmt: skip
    text = (listed.stdout or b"").decode("utf-8", "surrogateescape")
    return [item for item in text.split("\0") if item]


def apply_delta(
    inbox: Path, tree: Path, cache: Path, spec: dict[str, Any]
) -> dict[str, Any]:
    """Materialize each source of a delta upload into ``tree/<name>``."""
    return materialize(unbundle(inbox, cache, spec), tree)


def materialize(sources: list[DeltaSource], tree: Path) -> dict[str, Any]:
    applied: dict[str, Any] = {}
    for source in sources:
        target = tree / source.name
        _archive_into(source, target, None)
        _overlay_into(source, target)
        applied[source.name] = {
            "mode": "delta",
            "bundle": (source.incoming / "bundle").is_file(),
            "diff": (source.incoming / "diff").is_file(),
        }
    return applied


#: A lineage refreshed in place this many times gets a full sync (what a
#: build script wrote into its tree goes).
FULL_SYNC_EVERY = 50


def refresh_in_place(
    sources: list[DeltaSource],
    lineage_tree: Path,
    previous: dict[str, Any] | None,
    stage: Path,
) -> dict[str, int] | None:
    """Bring the lineage's tree to this run's sources by touching only what
    can differ (0.20): the lineage holds the same heads as last time, so
    only the paths last run's overlay and this run's overlay touch are
    re-read from the head, overlaid again, and copied when their content
    changed (unchanged files keep their mtime). ``None``: not possible (other
    heads or sources, or due for a full sync)."""
    if not previous or int(previous.get("refreshed") or 0) >= FULL_SYNC_EVERY:
        return None
    heads = previous.get("sources") or {}
    if sorted(heads) != sorted(s.name for s in sources):
        return None
    if any((heads.get(s.name) or {}).get("head") != s.head for s in sources):
        return None
    counts = {"written": 0, "removed": 0, "kept": 0, "removed_dirs": 0}
    for source in sources:
        paths = sorted(
            set((heads.get(source.name) or {}).get("overlay") or ())
            | set(source.overlay())
        )
        here = stage / source.name
        _archive_into(source, here, _in_head(source, paths))
        _overlay_into(source, here)
        dest_root = lineage_tree / source.name
        for path in paths:
            item, dest = here / path, dest_root / path
            if item.is_file() or item.is_symlink():
                if _same(item, dest):
                    counts["kept"] += 1
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.is_dir() and not dest.is_symlink():
                    shutil.rmtree(dest)
                elif dest.exists() or dest.is_symlink():
                    dest.unlink()
                if item.is_symlink():
                    os.symlink(os.readlink(item), dest)
                else:
                    partial = dest.parent / f".{dest.name}.piceli-sync"
                    shutil.copyfile(item, partial)
                    os.chmod(partial, item.stat().st_mode & 0o777)
                    os.replace(partial, dest)
                counts["written"] += 1
            elif dest.exists() or dest.is_symlink():
                if dest.is_dir() and not dest.is_symlink():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
                counts["removed"] += 1
        counts["removed_dirs"] += prune_empty_dirs(dest_root)
    return counts


#: Folders a sync never prunes inside: build output and git's own state.
PRUNE_SKIP = frozenset({"target", ".git"})


def prune_empty_dirs(root: Path) -> int:
    """Remove the folders under ``root`` that hold nothing, bottom-up (0.36.0).

    A file removed from the tree can leave its folder empty, and a cargo glob
    member (``crates/app/*``) then fails on it. ``root`` itself and anything
    under ``target/`` or ``.git/`` stay. Also heals folders older versions
    left empty. Returns how many folders went."""
    removed = 0
    if not root.is_dir() or root.is_symlink():
        return 0
    for directory, dirs, files in os.walk(root, topdown=False):
        here = Path(directory)
        if here == root or PRUNE_SKIP.intersection(here.relative_to(root).parts):
            continue
        if not files and not any((here / d).exists() for d in dirs):
            try:
                here.rmdir()
            except OSError:
                continue
            removed += 1
    return removed


def materialized(sources: list[DeltaSource], refreshed: int) -> dict[str, Any]:
    """What a lineage's tree now holds (its ``meta.json`` ``materialized``)."""
    return {
        "refreshed": refreshed,
        "sources": {s.name: {"head": s.head, "overlay": s.overlay()} for s in sources},
    }


def delta_pin(sources: list[DeltaSource], cwd: str | None, scratch: Path) -> Any:
    """The Rust pin the run's tree will hold, read from the mirror (no full
    tree yet); ``...`` (Ellipsis) when this run's overlay changes a pin file."""
    parts = Path(str(cwd or ".")).parts
    by_name = {s.name: s for s in sources}
    source = by_name.get(parts[0]) if parts else None
    if source is None:
        return None
    inner = list(parts[1:])
    overlay = set(source.overlay())
    while True:
        for name in ("rust-toolchain.toml", "rust-toolchain"):
            path = "/".join([*inner, name])
            if path in overlay:
                return ...
            shown = _git(
                "--git-dir", str(source.mirror), "show", f"{source.head}:{path}"
            )
            if shown.returncode == 0:
                where = scratch / source.name / path
                where.parent.mkdir(parents=True, exist_ok=True)
                where.write_bytes(shown.stdout)
                return _parse_toolchain(where, scratch)
        if not inner:
            return None
        inner.pop()


# ------------------------------------------------------------------ isolation


def wait_isolated(
    spec: dict[str, Any], clock: Callable[[], float], emit: Callable[[str], None]
) -> None:
    """Return once the API server is unreachable from this pod.

    A CNI programs a new pod's NetworkPolicy a moment after its network is
    up (or never, without policy support): the repository's code must not
    run before. Fails closed after ``isolation_timeout_seconds``.
    """
    if not spec.get("isolation_check"):
        return
    import socket

    host = os.environ.get("KUBERNETES_SERVICE_HOST") or "kubernetes.default.svc"
    port = int(os.environ.get("KUBERNETES_SERVICE_PORT") or 443)
    deadline = clock() + float(spec.get("isolation_timeout_seconds") or 60)
    said = False
    while True:
        try:
            socket.create_connection((host, port), timeout=1).close()
        except OSError:
            return
        if clock() >= deadline:
            raise RunError(
                "dev-isolation-not-enforced",
                "the API server stays reachable from the run's pod: the "
                "cluster does not enforce the NetworkPolicy of piceli-dev",
            )
        if not said:
            emit("[piceli-dev] waiting for the network policy to take effect")
            said = True
        time.sleep(0.5)


# ------------------------------------------------------------------ lineages


def _read_meta(path: Path) -> dict[str, Any]:
    try:
        value = json.loads((path / "meta.json").read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_meta(path: Path, meta: dict[str, Any]) -> None:
    partial = path / ".meta.json.partial"
    partial.write_text(json.dumps(meta))
    os.replace(partial, path / "meta.json")


def _try_lock(path: Path) -> int | None:
    path.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        return None
    return descriptor


def du(path: Path) -> int:
    """Bytes on disk under ``path`` (allocated blocks, links not followed)."""
    total = 0
    for directory, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_blocks * 512
            except OSError:
                continue
    return total


#: 0.35.0: the share of a cache claim its contents may use before the least
#: recently used lineages (or build targets) are evicted.
CACHE_BUDGET = 0.8
#: 0.35.0: the share of the cache's filesystem kept free. On a storage class
#: without quotas (k3s ``local-path``) a claim is a directory on the node's
#: disk: its size is not enforced and a full cache is a full node.
MIN_FREE_RATIO = 0.15
#: 0.35.0: the last budget check of a cache, kept at its root.
BUDGET_FILE = ".piceli-budget.json"
BUDGET_SCHEMA = "piceli.cache-budget.v1"
#: How long the measured size of what is not evictable (toolchains, cargo's
#: home, Git mirrors) is reused before it is measured again.
OTHER_TTL_SECONDS = 600


def filesystem(path: Path) -> tuple[int, int]:
    """``(free, total)`` bytes of the filesystem holding ``path`` (0, 0 unknown)."""
    try:
        found = os.statvfs(path)
    except OSError:
        return 0, 0
    return found.f_bavail * found.f_frsize, found.f_blocks * found.f_frsize


def _remove_tree(path: Path) -> None:
    for child in list(path.iterdir()) if path.is_dir() else ():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    shutil.rmtree(path, ignore_errors=True)


def _read_budget(cache: Path) -> dict[str, Any]:
    try:
        value = json.loads((cache / BUDGET_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_budget(cache: Path, report: dict[str, Any]) -> None:
    try:
        partial = cache / f".{BUDGET_FILE}.partial"
        partial.write_text(json.dumps(report, sort_keys=True))
        os.replace(partial, cache / BUDGET_FILE)
    except OSError:
        pass  # a report, never a reason to fail the run


def other_bytes(cache: Path, skip: Iterable[str], now: float) -> tuple[int, float]:
    """Bytes under ``cache`` outside ``skip`` and when they were measured
    (at most every :data:`OTHER_TTL_SECONDS`)."""
    known = _read_budget(cache)
    at, value = known.get("other_at"), known.get("other_bytes")
    if (
        isinstance(at, int | float)
        and isinstance(value, int)
        and 0 <= now - at < OTHER_TTL_SECONDS
    ):
        return value, float(at)
    skipped = {*skip, BUDGET_FILE}
    total = 0
    for child in cache.iterdir() if cache.is_dir() else ():
        if child.name in skipped:
            continue
        if child.is_dir() and not child.is_symlink():
            total += du(child)
        else:
            try:
                total += child.lstat().st_blocks * 512
            except OSError:
                continue
    return total, now


def shrink(
    cache: Path,
    units: list[tuple[float, int, str, Callable[[], bool]]],
    held: int,
    other: int,
    max_bytes: int,
    min_free_ratio: float,
) -> dict[str, Any]:
    """Evict ``units`` (``(last_used, bytes, name, remove)``), least recently
    used first, while the cache is over ``max_bytes`` or its filesystem has
    less than ``min_free_ratio`` free; the budget report.

    ``held`` is what busy units hold (counted, never evicted) and ``other``
    what is not evictable. ``remove()`` returns whether the unit went.
    """
    used = held + other + sum(size for _at, size, _name, _remove in units)
    evicted: list[str] = []
    freed = 0

    def low() -> bool:
        if min_free_ratio <= 0:
            return False
        free, total = filesystem(cache)
        return total > 0 and free < total * min_free_ratio

    why = None
    for _at, size, name, remove in sorted(units, key=lambda item: item[0]):
        over = used > max_bytes
        if not over and not low():
            break
        why = why or ("over-budget" if over else "filesystem-low")
        if remove():
            used -= size
            freed += size
            evicted.append(name)
    free, total = filesystem(cache)
    report = {
        "schema": BUDGET_SCHEMA,
        "used_bytes": used,
        "max_bytes": max_bytes,
        "other_bytes": other,
        "held_bytes": held,
        "evicted": evicted,
        "freed_bytes": freed,
        "over_budget": used > max_bytes,
        "fs_free_bytes": free,
        "fs_total_bytes": total,
        "fs_low": bool(min_free_ratio > 0 and total and free < total * min_free_ratio),
        "min_free_ratio": min_free_ratio,
    }
    if why:
        report["evicted_for"] = why
    return report


def _listed(path: Path, meta: dict[str, Any], *, busy: bool) -> dict[str, Any]:
    """0.53.0: one lineage as a budget report lists it."""
    return {
        "name": f"{path.parent.name}/{path.name}",
        "bytes": int(meta.get("bytes") or 0),
        "last_used": meta.get("last_used"),
        "busy": busy,
        "run": meta.get("run") or meta.get("last_run"),
        "hint": meta.get("hint"),
        "taken_at": meta.get("taken_at") if busy else None,
        "paused": meta.get("paused") if busy else None,
    }


def evict(
    cache: Path,
    limit: int,
    *,
    min_free_ratio: float = 0.0,
    now: float | None = None,
) -> dict[str, Any]:
    """Keep the development cache within its budget (0.35.0: everything in
    it counts, not only the lineages' recorded bytes).

    Removes least recently used free lineages while the cache holds more
    than ``limit`` bytes, or its filesystem has less than ``min_free_ratio``
    free. A lineage a run holds is counted, never removed. A free lineage
    without a size, or one a run took and never released (a killed pod), is
    measured first. Writes the report to :data:`BUDGET_FILE` and returns it.

    0.53.0: the report's ``lineages`` lists the lineages left, free or busy
    (``name``, ``bytes``, ``last_used``, ``busy``, ``run``, ``hint``,
    ``taken_at``, ``paused``).
    """
    now = time.time() if now is None else now
    cache.mkdir(parents=True, exist_ok=True)
    held = 0
    units: list[tuple[float, int, str, Callable[[], bool]]] = []
    locks: list[int] = []
    listed: list[dict[str, Any]] = []
    lineages = cache / "lineages"
    try:
        for path in sorted(lineages.glob("*/*")):
            if not path.name.isdigit() or not path.is_dir():
                continue
            meta = _read_meta(path)
            changed = path.stat().st_mtime  # before the lock file touches it
            descriptor = _try_lock(path)
            if descriptor is None:
                held += int(meta.get("bytes") or 0)
                listed.append(_listed(path, meta, busy=True))
                continue
            locks.append(descriptor)
            if "bytes" not in meta or meta.get("taken_at"):
                meta["bytes"] = du(path)
                meta["last_used"] = float(
                    meta.get("last_used") or meta.get("taken_at") or changed
                )
                meta.pop("taken_at", None)
                meta.pop("paused", None)
                if meta.get("run"):  # a killed run's mark
                    meta["last_run"] = meta.pop("run")
                _write_meta(path, meta)
            listed.append(_listed(path, meta, busy=False))

            def remove(path: Path = path) -> bool:
                _remove_tree(path)
                return True

            units.append(
                (
                    float(meta.get("last_used") or 0),
                    int(meta.get("bytes") or 0),
                    f"{path.parent.name}/{path.name}",
                    remove,
                )
            )
        other, other_at = other_bytes(cache, ("lineages",), now)
        report = shrink(cache, units, held, other, limit, min_free_ratio)
    finally:
        for descriptor in locks:
            os.close(descriptor)
    gone = set(report["evicted"])
    report.update(
        at=now,
        other_at=other_at,
        lineages=[item for item in listed if item["name"] not in gone],
    )
    _write_budget(cache, report)
    return report


#: 0.38.1: how often a running command's cache disk is checked (statvfs),
#: and the least time between two measurements of its lineage (``du``).
WATCH_SECONDS = 15.0
MEASURE_SECONDS = 60.0
#: A lineage on a disk that looks fine is still measured this many
#: :data:`MEASURE_SECONDS` apart (a filesystem statvfs cannot see).
REMEASURE_FACTOR = 10
#: Under the disk floor a run stops once its lineage grew this much.
GROWTH_SLACK_BYTES = 1 << 30
#: 0.56.0: a run paused this long that still does not fit stops, so its
#: lineage is freed for the runs that wait (paused runs held their
#: lineages while the oldest run kept the cache over its budget).
PAUSE_LIMIT_SECONDS = 600.0


class CacheWatch:
    """0.38.1: holds a run's lineage to its cap while the command runs.

    ``cap`` is the budget minus what the rest of the cache held when the run
    took its lineage. Every :data:`WATCH_SECONDS` it reads the cache's disk
    (statvfs, cheap): the lineage's size is its last measurement plus what
    the disk lost since. Only when that estimate passes the cap, or the disk
    is under its floor, is the lineage measured (``du``, at most every
    :data:`MEASURE_SECONDS`). Calling it says whether the run must stop:
    ``stopped`` is then ``"over-cap"`` (the lineage passed its cap) or
    ``"filesystem-low"`` (the disk is under its floor and the lineage grew
    by :data:`GROWTH_SLACK_BYTES` in this run).

    0.53.0: with ``lineages`` (the last budget report's list) it stops a run
    only when no other lineage is busy. While others are, the youngest
    running run pauses (``paused``, for :func:`run_command` to ``SIGSTOP``
    its group) and resumes once its bytes fit the cap again, or once no
    older run is busy; every other run continues (the budget is soft while
    runs share it, and the oldest always proceeds). ``pauses`` lists each
    pause (``reason``, ``seconds``).

    0.56.0: a run paused for ``pause_limit`` seconds that still does not
    fit stops (``stopped``), so its lineage can be evicted.
    """

    def __init__(
        self,
        lineage: Path,
        cap: int,
        start_bytes: int,
        *,
        min_free_ratio: float = 0.0,
        every: float = WATCH_SECONDS,
        measure_every: float = MEASURE_SECONDS,
        slack: int = GROWTH_SLACK_BYTES,
        clock: Callable[[], float] = time.monotonic,
        make_room: Callable[[int], int] | None = None,
        lineages: Callable[[], list[dict[str, Any]]] | None = None,
        taken_at: float = 0.0,
        emit: Callable[[str], None] = lambda _line: None,
        pause_limit: float = PAUSE_LIMIT_SECONDS,
    ) -> None:
        self.lineage = lineage
        self.pause_limit = pause_limit
        self.name = f"{lineage.parent.name}/{lineage.name}"
        self.cap = cap
        self.make_room = make_room
        self.lineages = lineages
        self.taken_at = taken_at
        self.emit = emit
        self.start_bytes = self.bytes = start_bytes
        self.min_free_ratio = min_free_ratio
        self.every = every
        self.measure_every = measure_every
        self.slack = slack
        self.clock = clock
        self.stopped: str | None = None
        self.paused: dict[str, Any] | None = None
        self.pauses: list[dict[str, Any]] = []
        self.measured = 0
        self._paused_at = 0.0
        self._continued = False
        now = clock()
        self.checked_at = self.measured_at = now
        free, total = filesystem(lineage)
        self.disk_used = total - free if total else None

    def _low(self) -> bool:
        free, total = filesystem(self.lineage)
        return bool(
            self.min_free_ratio > 0 and total and free < total * self.min_free_ratio
        )

    def __call__(self) -> bool:
        if self.stopped:
            return True
        now = self.clock()
        if now - self.checked_at < self.every:
            return False
        self.checked_at = now
        grew = self.bytes > self.start_bytes + self.slack
        if self.paused is not None:
            # 0.53.0: paused, the lineage does not grow; make room and see.
            if self.make_room is not None:
                self.cap = self.make_room(self.bytes)
            self._decide(now, self._low() and grew)
            return self.stopped is not None
        free, total = filesystem(self.lineage)
        used = total - free if total else None
        low = bool(
            self.min_free_ratio > 0 and total and free < total * self.min_free_ratio
        )
        grown = (
            max(0, used - self.disk_used)
            if used is not None and self.disk_used is not None
            else None
        )
        suspect = low or grown is None or self.bytes + grown > self.cap
        wait = self.measure_every * (1 if suspect else REMEASURE_FACTOR)
        if now - self.measured_at < wait:
            return False
        self.bytes, self.measured_at, self.disk_used = du(self.lineage), now, used
        self.measured += 1
        try:  # so a start-time eviction elsewhere counts its real size
            _write_meta(self.lineage, {**_read_meta(self.lineage), "bytes": self.bytes})
        except OSError:
            pass
        grew = self.bytes > self.start_bytes + self.slack
        if (self.bytes > self.cap or (low and grew)) and self.make_room is not None:
            # 0.48.0: free lineages go before a running one is stopped.
            self.cap = self.make_room(self.bytes)
            low = self._low()
        self._decide(now, low and grew)
        return self.stopped is not None

    def _decide(self, now: float, low: bool) -> None:
        """0.53.0: stop, pause, continue or resume, from the cap and the
        other busy lineages."""
        why = "over-cap" if self.bytes > self.cap else "filesystem-low" if low else None
        if why is None:
            if self.paused is not None:
                self._resume(now, "room in the cache")
            return
        others = [
            item
            for item in (self.lineages() if self.lineages is not None else ())
            if item.get("busy") and item.get("name") != self.name
        ]
        if not others:  # this lineage alone is over the budget
            if self.paused is not None:
                self._resume(now, "no other run holds the cache")
            self.stopped = why
            return

        def age(item: dict[str, Any]) -> float:
            return float(item.get("taken_at") or 0)

        running = [item for item in others if not item.get("paused")]
        held = sum(int(item.get("bytes") or 0) for item in others) / 2**30
        if self.paused is not None:
            older = [item for item in others if age(item) < self.taken_at]
            if not older:
                self._resume(now, "the oldest run in the cache")
            elif now - self._paused_at >= self.pause_limit:
                waited = now - self._paused_at
                self._resume(now, "it stops instead")
                self.stopped = why
                self.emit(
                    f"[piceli-dev] run stopped ({why}): paused {waited:.0f} s while "
                    f"{len(older)} older run(s) still ran; its lineage "
                    "is freed for the runs that wait"
                )
            return
        older = any(age(item) < self.taken_at for item in running)
        if older and all(age(item) <= self.taken_at for item in running):
            self.paused = {"since": time.time(), "reason": why}
            self._paused_at = now
            self._mark()
            self.emit(
                f"[piceli-dev] run paused ({why}): its lineage holds "
                f"{self.bytes / 2**30:.1f} GiB over its cap of "
                f"{self.cap / 2**30:.1f} GiB while {len(others)} other run(s) "
                f"hold {held:.1f} GiB; it resumes when room appears"
            )
        elif not self._continued:
            self._continued = True
            self.emit(
                f"[piceli-dev] cache over budget ({why}): {len(others)} other "
                f"run(s) hold {held:.1f} GiB; this run continues (the budget is "
                "soft while runs share it)"
            )

    def _mark(self) -> None:
        try:
            meta = _read_meta(self.lineage)
            meta.pop("paused", None)
            if self.paused is not None:
                meta["paused"] = self.paused
            _write_meta(self.lineage, meta)
        except OSError:
            pass

    def _resume(self, now: float, because: str) -> None:
        assert self.paused is not None
        seconds = round(now - self._paused_at, 3)
        self.pauses.append({"reason": self.paused["reason"], "seconds": seconds})
        self.paused = None
        self._mark()
        self.emit(f"[piceli-dev] run resumed after {seconds:.0f} s ({because})")

    @property
    def paused_seconds(self) -> float:
        """0.53.0: the time this run spent paused (the deadline skips it)."""
        current = self.clock() - self._paused_at if self.paused is not None else 0
        return sum(float(item["seconds"]) for item in self.pauses) + current

    def close(self) -> None:
        """0.53.0: end a pause the command outlived (it was killed)."""
        if self.paused is not None:
            self._resume(self.clock(), "the command ended")


class Lineage:
    """A taken lineage: its directory, lock and whether it was built before.

    0.53.0: ``seed_from`` is the nearest sibling to copy into a new one."""

    def __init__(
        self,
        path: Path,
        descriptor: int,
        warm: bool,
        seed_from: Path | None = None,
        *,
        command_warm: bool | None = None,
        waited: float = 0.0,
    ) -> None:
        self.path = path
        self.descriptor = descriptor
        #: Built before, by any command (the tree and some of target/).
        self.warm = warm
        self.seed_from = seed_from
        #: 0.61.0: this lineage last finished the run's own command key
        #: (``None``: the run has no key).
        self.command_warm = command_warm
        #: 0.61.0: seconds the run waited for a busy lineage warm for it.
        self.waited = waited

    def release(self, hint: str | None, run: str, command: str | None = None) -> None:
        meta = _read_meta(self.path)
        for key in ("taken_at", "run", "paused"):
            meta.pop(key, None)
        now = time.time()
        meta.update(
            last_used=now,
            bytes=du(self.path),
            hint=hint,
            runs=int(meta.get("runs") or 0) + 1,
            last_run=run,
        )
        if command:
            # 0.61.0: the command keys this lineage finished, newest kept.
            done = dict(meta.get("commands") or {})
            done[command] = now
            meta["commands"] = dict(
                sorted(done.items(), key=lambda item: -float(item[1]))[:COMMAND_KEYS]
            )
        partial = self.path / ".meta.json.partial"
        partial.write_text(json.dumps(meta))
        os.replace(partial, self.path / "meta.json")
        os.close(self.descriptor)

    def drop(self) -> int:
        """0.38.1: remove the lineage (it went over its cap) while still
        holding it; the bytes it held."""
        size = du(self.path)
        try:
            _remove_tree(self.path)
        finally:
            os.close(self.descriptor)
        return size


_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def distance(mirror: Path, one: str, two: str) -> int | None:
    """0.53.0: the commits between two heads (``git rev-list --count
    one...two``); ``None`` when either is not in the mirror or Git fails."""
    if not (_SHA.fullmatch(one) and _SHA.fullmatch(two)):
        return None
    if one == two:
        return 0
    try:
        done = _git(
            "--git-dir", str(mirror), "rev-list", "--count", f"{one}...{two}",
            timeout=60,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return int(done.stdout.strip()) if done.returncode == 0 else None
    except ValueError:
        return None


#: 0.61.0: how many command keys a lineage remembers.
COMMAND_KEYS = 16
#: 0.61.0: the environment that changes what cargo builds (part of the key).
_BUILD_ENV = re.compile(r"(CARGO_.*|RUST.*|CC|CXX|CFLAGS|CXXFLAGS|LDFLAGS|TARGET_.*)")
#: 0.61.0: how long a run waits for a busy lineage warm for its command
#: before it takes a cold one (``warm_wait_seconds`` in the spec).
WARM_WAIT_SECONDS = 120.0


def command_key(spec: dict[str, Any]) -> str | None:
    """0.61.0: what a lineage must have built for a run to be warm: the
    command up to ``--`` (test filters and binary arguments do not change
    what cargo compiles), the working directory and the build environment
    (``RUSTFLAGS``, ``CARGO_*``...). ``None`` without a command."""
    command = [str(part) for part in spec.get("command") or ()]
    if not command:
        return None
    if "--" in command:
        command = command[: command.index("--")]
    env = {
        str(key): str(value)
        for key, value in (spec.get("env") or {}).items()
        if _BUILD_ENV.fullmatch(str(key))
    }
    document = {"command": command, "cwd": str(spec.get("cwd") or "."), "env": env}
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()[
        :16
    ]


def _finished(meta: dict[str, Any], key: str | None) -> bool:
    return bool(key) and key in (meta.get("commands") or {})


def nearest(
    items: list[tuple[dict[str, Any], Path]],
    hint: str | None,
    near: DeltaSource | None,
    command: str | None = None,
) -> tuple[dict[str, Any], Path] | None:
    """0.53.0: the lineage a run prefers among ``items`` (``(meta, path)``):
    the same hint (branch); else the smallest distance from its head to
    ``near``'s head in ``near``'s mirror; else the most recently used.

    0.61.0: only among the lineages that finished the run's ``command`` key,
    when any did: one warm for other commands only still compiles
    everything for a new target, profile or ``RUSTFLAGS``."""
    if not items:
        return None
    warm = [item for item in items if _finished(item[0], command)]
    if warm:
        items = warm

    def recent(item: tuple[dict[str, Any], Path]) -> float:
        return float(item[0].get("last_used") or 0)

    same = [item for item in items if hint and item[0].get("hint") == hint]
    if same:
        return max(same, key=recent)
    if near is not None:
        ranked = []
        for item in items:
            held = (item[0].get("materialized") or {}).get("sources") or {}
            head = (held.get(near.name) or {}) if isinstance(held, dict) else {}
            found = (
                distance(near.mirror, str(head.get("head")), near.head)
                if isinstance(head, dict) and head.get("head")
                else None
            )
            if found is not None:
                ranked.append((found, -recent(item), item))
        if ranked:
            return min(ranked, key=lambda entry: entry[:2])[2]
    return max(items, key=recent)


def _warmth(lineage: Lineage) -> str:
    """0.61.0: ``warm`` only when the lineage finished this very command."""
    if not lineage.warm:
        return "cold"
    if lineage.command_warm is False:
        return "cold for this command, built before by others"
    waited = f", waited {lineage.waited:.0f} s for it" if lineage.waited >= 1 else ""
    return "warm" + waited


def take_lineage(
    cache: Path,
    spec: dict[str, Any],
    clock: Callable[[], float],
    near: DeltaSource | None = None,
) -> Lineage:
    """The free lineage this run builds in (waits while all are busy).

    0.53.0: ``near`` (the run's first source) ranks free lineages by how
    close their head is to the run's (:func:`nearest`); a new lineage names
    the nearest sibling to seed from."""
    key = str(spec["lineage_key"])
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", key):
        raise RunError("dev-run-invalid", "the lineage key is not a safe name")
    root = cache / "lineages" / key
    root.mkdir(parents=True, exist_ok=True)
    most = max(1, int(spec.get("max_lineages") or 1))
    hint = spec.get("lineage_hint")
    command = command_key(spec)
    started = clock()
    deadline = started + float(spec.get("timeout_seconds") or 3600)
    patience = float(spec.get("warm_wait_seconds", WARM_WAIT_SECONDS) or 0)
    while True:
        select = os.open(root / ".select.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(select, fcntl.LOCK_EX)
            free: list[tuple[int, dict[str, Any], Path, int]] = []
            existing = sorted(
                int(path.name) for path in root.iterdir() if path.name.isdigit()
            )
            for index in existing:
                path = root / str(index)
                descriptor = _try_lock(path)
                if descriptor is not None:
                    free.append((index, _read_meta(path), path, descriptor))
            chosen = None
            seed = None
            # 0.61.0: no free lineage finished this command but a busy one
            # did (a cold build of a new target can take ten times as long
            # as a warm one): start a new lineage seeded from it when there
            # is room, else wait for it a while.
            busy = [index for index in existing if all(i[0] != index for i in free)]
            warm_elsewhere = (
                command is not None
                and not any(_finished(item[1], command) for item in free)
                and any(_finished(_read_meta(root / str(i)), command) for i in busy)
            )
            grow = len(existing) < most and (not free or warm_elsewhere)
            if (
                free
                and not grow
                and not (warm_elsewhere and clock() - started < patience)
            ):
                best = nearest(
                    [(item[1], item[2]) for item in free], hint, near, command
                )
                chosen = next(item for item in free if best and item[2] == best[1])
            elif grow:
                index = next(i for i in range(len(existing) + 1) if i not in existing)
                path = root / str(index)
                descriptor = _try_lock(path)
                if descriptor is not None:
                    chosen = (index, {}, path, descriptor)
                    # 0.53.0: the nearest sibling with a build to copy.
                    siblings = [
                        (_read_meta(root / str(i)), root / str(i))
                        for i in existing
                        if (root / str(i) / "target").is_dir()
                    ]
                    found = nearest(siblings, hint, near, command)
                    seed = found[1] if found else None
            for item in free:
                if item is not chosen:
                    os.close(item[3])
            if chosen is not None:
                _index, meta, path, descriptor = chosen
                # 0.35.0: a lineage whose run never releases it (a killed
                # pod) keeps this mark; the next eviction measures it again.
                # 0.53.0: with its run, for the budget report.
                meta = {k: v for k, v in meta.items() if k != "paused"}
                _write_meta(
                    path,
                    {**meta, "taken_at": time.time(), "run": spec.get("run")},
                )
                return Lineage(
                    path,
                    descriptor,
                    bool(meta.get("runs")),
                    seed,
                    command_warm=_finished(meta, command) if command else None,
                    waited=round(clock() - started, 3),
                )
        finally:
            os.close(select)
        if clock() >= deadline:
            raise RunError("dev-run-timed-out", "no free lineage in time")
        time.sleep(float(spec.get("poll_seconds") or 0.5))


#: 0.53.0: the directories of a cargo profile whose entries are named after
#: a build unit (``<name>-<hash>``, a library with a ``lib`` prefix).
UNIT_DIRS = ("deps", ".fingerprint", "build", "incremental")


def _stems(name: str) -> set[str]:
    stem = name.split(".", 1)[0]
    return {stem, stem.removeprefix("lib")} - {""}


def _copy_dir(source: Path, dest: Path) -> str:
    """Copy a directory, reflinked where the filesystem can; the method."""
    try:
        done = subprocess.run(
            ["cp", "-a", "--reflink=auto", str(source), str(dest)],
            capture_output=True, check=False,
        )  # fmt: skip
        if done.returncode == 0 or dest.exists():
            return "cp"  # files that vanished meanwhile: the fix-up handles them
    except OSError:
        pass
    shutil.rmtree(dest, ignore_errors=True)  # cp rejects the flag (BSD, busybox)
    try:
        shutil.copytree(source, dest, symlinks=True, copy_function=shutil.copy2)
    except shutil.Error:
        pass  # files that vanished meanwhile: the fix-up handles them
    return "copytree"


def fix_copy(source: Path, copy: Path, *, units: bool) -> int:
    """0.53.0: make a copy of a directory that changed while it was copied
    safe to build on; the build units dropped.

    A copied file whose size or mtime differs from the source's now, or
    that is gone from the source, is removed. With ``units`` (a cargo
    target), every entry of its unit (the same ``<name>-<hash>`` under the
    profile's :data:`UNIT_DIRS`) goes with it, so cargo rebuilds that unit;
    cargo's ``.cargo-lock`` files go too."""
    stale: dict[Path, list[set[str]]] = {}
    for directory, dirs, files in os.walk(copy):
        here = Path(directory)
        relative = here.relative_to(copy)
        links = [name for name in dirs if (here / name).is_symlink()]
        for name in [*files, *links]:
            copied = here / name
            if units and name == ".cargo-lock":
                copied.unlink(missing_ok=True)
                continue
            original = source / relative / name
            try:
                one, two = copied.lstat(), original.lstat()
                if copied.is_symlink() or original.is_symlink():
                    same = (
                        copied.is_symlink()
                        and original.is_symlink()
                        and os.readlink(copied) == os.readlink(original)
                    )
                else:
                    same = (one.st_size, one.st_mtime_ns) == (
                        two.st_size,
                        two.st_mtime_ns,
                    )
            except OSError:
                same = False
            if same:
                continue
            copied.unlink(missing_ok=True)
            parts = (*relative.parts, name)
            hit = next(
                (i for i, part in enumerate(parts[:-1]) if i and part in UNIT_DIRS),
                None,
            )
            if units and hit is not None:
                stale.setdefault(copy.joinpath(*parts[:hit]), []).append(
                    _stems(parts[hit + 1])
                )
    dropped = 0
    for profile, groups in stale.items():
        merged: list[set[str]] = []  # libfoo-1a.rlib and foo-1a.d: one unit
        for group in groups:
            joined = [known for known in merged if known & group]
            merged = [known for known in merged if not known & group]
            merged.append(group.union(*joined))
        dropped += len(merged)
        stems = set().union(*merged)
        for folder in UNIT_DIRS:
            found = profile / folder
            for entry in list(found.iterdir()) if found.is_dir() else ():
                if not _stems(entry.name) & stems:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
    return dropped


def seed_lineage(lineage: Path, source: Path, cap: int) -> dict[str, Any] | None:
    """0.53.0: copy ``source`` (a sibling lineage, free or busy) into the new
    ``lineage``: its ``tree/`` and ``target/``, then :func:`fix_copy`. The
    seed report (``from``, ``bytes``, ``seconds``, ``method``,
    ``dropped_units``), or ``None`` when the source does not fit ``cap``.

    The new lineage's meta records ``seeded_from`` and the source's
    ``fetched``, and no ``materialized``: its first sync is full (unchanged
    files keep their mtime)."""
    known = _read_meta(source)
    size = int(known.get("bytes") or 0) or du(source)
    if size > cap:
        return None
    began = time.monotonic()
    method = "copytree"
    dropped = 0
    try:
        for part in ("tree", "target"):
            if (source / part).is_dir():
                method = _copy_dir(source / part, lineage / part)
                dropped += fix_copy(
                    source / part, lineage / part, units=part == "target"
                )
    except OSError:
        for part in ("tree", "target"):
            shutil.rmtree(lineage / part, ignore_errors=True)
        raise
    name = f"{source.parent.name}/{source.name}"
    held = du(lineage)
    meta = _read_meta(lineage)
    meta.pop("materialized", None)
    meta.update(seeded_from=name, bytes=held)
    if known.get("fetched"):
        meta["fetched"] = known["fetched"]
    _write_meta(lineage, meta)
    return {
        "from": name,
        "bytes": held,
        "seconds": round(time.monotonic() - began, 3),
        "method": method,
        "dropped_units": dropped,
    }


def _same(source: Path, dest: Path) -> bool:
    try:
        left, right = source.lstat(), dest.lstat()
    except OSError:
        return False
    if source.is_symlink() or dest.is_symlink():
        return (
            source.is_symlink()
            and dest.is_symlink()
            and os.readlink(source) == os.readlink(dest)
        )
    if not dest.is_file() or left.st_size != right.st_size:
        return False
    if (left.st_mode & 0o111) != (right.st_mode & 0o111):
        return False
    with source.open("rb") as one, dest.open("rb") as two:
        while True:
            block = one.read(1 << 20)
            if block != two.read(1 << 20):
                return False
            if not block:
                return True


def sync_tree(source: Path, dest: Path) -> dict[str, int]:
    """Make ``dest`` hold ``source``; unchanged files keep their mtime."""
    counts = {"written": 0, "removed": 0, "kept": 0, "removed_dirs": 0}
    dest.mkdir(parents=True, exist_ok=True)
    wanted: set[str] = set()
    for directory, dirs, files in os.walk(source):
        relative = os.path.relpath(directory, source)
        base = dest if relative == "." else dest / relative
        if relative != ".":
            wanted.add(relative)
        if base.is_symlink() or (base.exists() and not base.is_dir()):
            base.unlink()
        base.mkdir(exist_ok=True)
        for name in [*files, *[d for d in dirs if (Path(directory) / d).is_symlink()]]:
            item = Path(directory) / name
            target = base / name
            key = os.path.normpath(os.path.join(relative, name))
            wanted.add(key)
            if _same(item, target):
                counts["kept"] += 1
                continue
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
            if item.is_symlink():
                os.symlink(os.readlink(item), target)
            else:
                partial = base / f".{name}.piceli-sync"
                shutil.copyfile(item, partial)
                os.chmod(partial, item.stat().st_mode & 0o777)
                os.replace(partial, target)
            counts["written"] += 1
        dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
    for directory, dirs, files in os.walk(dest, topdown=False):
        relative = os.path.relpath(directory, dest)
        for name in [*files, *[d for d in dirs if (Path(directory) / d).is_symlink()]]:
            key = os.path.normpath(os.path.join(relative, name))
            if key not in wanted:
                os.unlink(os.path.join(directory, name))
                counts["removed"] += 1
        if relative != "." and relative not in wanted:
            shutil.rmtree(directory, ignore_errors=True)
            counts["removed_dirs"] += 1
    return counts


# ------------------------------------------------------------------ command


# ------------------------------------------------- corrupted cargo caches (0.19)
#: A rustc crash (an ICE: internal compiler error), in every rustc's words.
_ICE = re.compile(
    r"internal compiler error|thread 'rustc'(?: \(\d+\))? panicked"
    r"|the compiler unexpectedly panicked"
)
#: Output that means a cached artifact is damaged, not the code: the loader
#: refusing a proc-macro ``.so``, invalid crate metadata, a linker reading a
#: truncated or garbled object (a run killed mid-write, a faulty disk).
_CORRUPT = (
    # 0.20: rustc's incremental state broke the compiler itself (an ICE in
    # dep_graph/serialized.rs on a faulty node): drop incremental/, retry.
    # 0.64.0: rustc 1.97 says "thread 'rustc' (1169) panicked" and "the
    # compiler unexpectedly panicked", without "internal compiler error".
    ("incremental", _ICE),
    (
        "loader",
        re.compile(
            r"error while loading shared libraries|cannot open shared object"
            r"|invalid ELF header|R_X86_64_[A-Z0-9_]+|\bld\.so\b"
            r"|(?:failed|cannot) to load proc[- ]macro|proc[- ]macro .* could not be loaded"
        ),
    ),
    (
        "metadata",
        re.compile(r"E0786|found invalid metadata files|invalid metadata version"),
    ),
    (
        "link",
        re.compile(
            r"undefined (?:hidden )?symbol|file truncated|file format not recognized"
            r"|malformed archive|archive has no index"
        ),
    ),
)
_ARTIFACT = re.compile(
    r"(?:/deps/(?:lib)?|/incremental/|/build/|/\.fingerprint/)"
    r"([A-Za-z0-9_-]+)-([0-9a-z]{1,24})(?=[./\s'\"`]|$)"
)
_TICKED = re.compile(r"for crate `([A-Za-z0-9_-]+)`")
_COULD_NOT_COMPILE = re.compile(r"could not compile `([A-Za-z0-9_-]+)`")
_ENTRY = re.compile(r"^(?:lib)?([A-Za-z0-9_-]+)-[0-9a-z]{1,24}(?:\..*)?$")
_ARTIFACT_DIRS = frozenset({"deps", ".fingerprint", "build", "incremental"})


def _crate(name: str) -> str:
    return name.replace("-", "_")


def corrupt_crates(lines: list[str]) -> tuple[list[str], str | None]:
    """The crates whose cached artifacts a failed build's output blames, and
    the kind of damage (``loader``, ``metadata``, ``link``); ``([], None)``
    for an ordinary failure."""
    for kind, signature in _CORRUPT:
        hits = [line for line in lines if signature.search(line)]
        if not hits:
            continue
        found: set[str] = set()
        for line in hits:
            found |= {_crate(match[0]) for match in _ARTIFACT.findall(line)}
            found |= {_crate(name) for name in _TICKED.findall(line)}
        if kind == "link":  # the objects are the failing crate's own
            for line in lines:
                found |= {_crate(match[0]) for match in _ARTIFACT.findall(line)}
                found |= {_crate(name) for name in _COULD_NOT_COMPILE.findall(line)}
        return sorted(found), kind
    return [], None


# ------------------------------------------------- machine health (0.22)
#: Output that points at the machine rather than the code: a compiler that
#: crashed, a process killed by a hardware fault, a link that lost symbols, a
#: cached blob whose bytes no longer match their digest. A build host that
#: shows these repeatedly is quarantined (:mod:`piceli.dev.health`).
HEALTH_SIGNATURES = (
    ("internal-compiler-error", _ICE),
    (
        "link-symbol",
        re.compile(
            r"undefined (?:hidden )?symbol|hidden symbol `[^`']*' is(?:n't)? "
            r"(?:referenced|defined)"
        ),
    ),
    ("segfault", re.compile(r"Segmentation fault|SIGSEGV|signal: 11\b")),
    ("illegal-instruction", re.compile(r"Illegal instruction|SIGILL|signal: 4\b")),
    ("bus-error", re.compile(r"\bBus error\b|SIGBUS|signal: 7\b")),
    (
        "machine-check",
        re.compile(r"\bmce: |[Mm]achine [Cc]heck|Hardware Error"),
    ),
    ("cache-corrupt", re.compile(r"\[piceli\] cache-corrupt: ")),
    # A host build step repaired its cargo cache (piceli.artifacts.host_execution).
    ("cache-repaired", re.compile(r"\[piceli\] .*: corrupted cache \(")),
)


def health_signal(lines: Iterable[str]) -> dict[str, str] | None:
    """The first line of ``lines`` with a machine-health signature, as
    ``{"kind", "line"}``; ``None`` when there is none."""
    for line in lines:
        for kind, signature in HEALTH_SIGNATURES:
            if signature.search(line):
                return {"kind": kind, "line": line.strip()[:300]}
    return None


def clear_incremental(target: Path) -> int:
    """Remove every ``incremental/`` directory of a cargo target (every
    profile and triple); how many went."""
    removed = 0
    for root, dirs, _files in os.walk(target):
        if "incremental" in dirs:
            shutil.rmtree(Path(root) / "incremental", ignore_errors=True)
            dirs.remove("incremental")
            removed += 1
        dirs[:] = [d for d in dirs if d not in _ARTIFACT_DIRS]
    return removed


def clear_crates(target: Path, crates: list[str]) -> int:
    """Remove the named crates' artifacts (deps, fingerprints, build script
    outputs, incremental state) from a cargo target directory; how many
    entries went."""
    wanted = {_crate(name) for name in crates}
    removed = 0
    for root, dirs, _files in os.walk(target):
        here = Path(root)
        if here.name in _ARTIFACT_DIRS:
            for entry in list(here.iterdir()):
                found = _ENTRY.match(entry.name)
                if found and _crate(found.group(1)) in wanted:
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
                    removed += 1
            dirs[:] = []  # never below an artifact directory
    return removed


CLEAR_SCHEMA = "piceli.dev-cache-clear.v1"


def _selected(cache: Path, lineage: str | None) -> list[Path]:
    """Lineage directories matching ``KEY/N``, ``KEY`` or a root source name
    (every key ``NAME-...``); all without ``lineage``."""
    roots = cache / "lineages"
    found = []
    for key in sorted(roots.iterdir()) if roots.is_dir() else ():
        for path in sorted(key.iterdir(), key=lambda item: item.name):
            if not path.name.isdigit():
                continue
            name = f"{key.name}/{path.name}"
            if (
                lineage is None
                or lineage in (name, key.name)
                or key.name.startswith(f"{lineage}-")
            ):
                found.append(path)
    return found


def clear_cache(
    cache: Path, lineage: str | None = None, crates: list[str] | tuple[str, ...] = ()
) -> dict[str, Any]:
    """Empty the cargo target of free lineages (0.19.0), or only ``crates``'
    artifacts; a lineage a run holds is skipped (``busy``)."""
    cleared: list[dict[str, Any]] = []
    busy: list[str] = []
    for path in _selected(cache, lineage):
        name = f"{path.parent.name}/{path.name}"
        descriptor = _try_lock(path)
        if descriptor is None:
            busy.append(name)
            continue
        try:
            target = path / "target"
            if crates:
                cleared.append(
                    {"lineage": name, "entries": clear_crates(target, list(crates))}
                )
                continue
            size = du(target)
            shutil.rmtree(target, ignore_errors=True)
            meta = {**_read_meta(path), "runs": 0, "bytes": du(path)}
            (path / "meta.json").write_text(json.dumps(meta))
            cleared.append({"lineage": name, "bytes": size})
        finally:
            os.close(descriptor)
    return {"schema": CLEAR_SCHEMA, "cleared": cleared, "busy": busy}


def clear_main(
    spec: dict[str, Any], cache: Path, emit: Callable[[str], None] = print
) -> dict[str, Any]:
    """The cache-clear Job's entry (``PICELI_DEV_ROLE=clear``); with
    ``evict`` (0.38.0) the budget eviction a run does, its report as ``budget``."""
    if spec.get("evict"):
        budget = evict(
            cache,
            int(spec.get("cache_max_bytes") or 1 << 62),
            min_free_ratio=float(spec.get("cache_min_free_ratio") or 0),
        )
        result = {"schema": CLEAR_SCHEMA, "cleared": [], "busy": [], "budget": budget}
        emit(RESULT_MARKER + json.dumps(result, sort_keys=True))
        return result
    result = clear_cache(
        cache, spec.get("lineage"), [str(c) for c in spec.get("crates") or ()]
    )
    emit(RESULT_MARKER + json.dumps(result, sort_keys=True))
    return result


class FirstError:
    """The first ``error`` line of a build and the lines after it, so a long
    log's cause survives a short tail."""

    _START = re.compile(r"^\s*error(?:\[E\d+\])?:")

    def __init__(self, lines: int = 20) -> None:
        self.limit = lines
        self.lines: list[str] = []

    def feed(self, line: str) -> None:
        if (self.lines and len(self.lines) < self.limit) or (
            not self.lines and self._START.match(line)
        ):
            self.lines.append(line)


class CargoOutput:
    """What cargo's output tells: crates compiled, build/test split, tests."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.compiled = 0
        self.finished_at: float | None = None
        self.tests: dict[str, Any] = {
            "passed": 0, "failed": 0, "ignored": 0, "suites": 0,
        }  # fmt: skip
        self.failed_names: list[str] = []

    def feed(self, line: str) -> None:
        failed = _FAILED_TEST.match(line)
        if failed and len(self.failed_names) < FAILED_NAMES:
            if failed.group(1) not in self.failed_names:
                self.failed_names.append(failed.group(1))
        if _COMPILING.match(line):
            self.compiled += 1
        elif self.finished_at is None and _FINISHED.match(line):
            self.finished_at = self.clock()
        found = _TESTS.search(line)
        if found:
            self.tests["passed"] += int(found.group(1))
            self.tests["failed"] += int(found.group(2))
            self.tests["ignored"] += int(found.group(3))
            self.tests["suites"] += 1


def _locked_crates(cwd: Path, top: Path) -> int | None:
    for directory in (cwd, *cwd.parents):
        lock = directory / "Cargo.lock"
        if lock.is_file():
            return lock.read_text(errors="replace").count("[[package]]")
        if directory == top:
            break
    return None


def run_command(
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    emit: Callable[[str], None],
    on_line: Callable[[str], None],
    stop: Callable[[], bool] | None = None,
    paused: Callable[[], bool] | None = None,
) -> tuple[int | None, bool]:
    """Run ``command``; ``(exit code, timed out)``. Its whole group is killed
    at the deadline, or (0.38.1) once ``stop()`` says so. 0.53.0: while
    ``paused()`` says so the group is stopped (``SIGSTOP``, then
    ``SIGCONT``), and the paused time extends the deadline, by at most
    ``timeout`` in all (a run's Job allows twice its timeout)."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def read() -> None:
        assert process.stdout is not None
        for raw in iter(process.stdout.readline, b""):
            lines.put(raw.decode("utf-8", "replace").rstrip("\r\n"))
        lines.put(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    stopped_at: float | None = None  # 0.53.0: while the group is paused
    granted = 0.0  # paused time given back so far (at most ``timeout``)

    def signal_group(sig: int) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    while True:
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            line = ""
        if line is None:
            break
        if line or line == "":
            if line:
                emit(line)
                on_line(line)
        halt = stop is not None and stop()
        if paused is not None and not halt and process.poll() is None:
            want, now = paused(), time.monotonic()
            if want and stopped_at is None:
                signal_group(signal.SIGSTOP)
                stopped_at = now
            elif not want and stopped_at is not None:
                signal_group(signal.SIGCONT)
                extra = min(now - stopped_at, timeout - granted)
                deadline, granted = deadline + extra, granted + extra
                stopped_at = None
        late = time.monotonic() >= deadline + (
            0.0 if stopped_at is None else timeout - granted
        )
        if (late or halt) and process.poll() is None:
            timed_out = late
            if stopped_at is not None:  # a stopped process gets no SIGTERM
                signal_group(signal.SIGCONT)
                stopped_at = None
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    process.wait(5)
                    break
                except subprocess.TimeoutExpired:
                    continue
    code = process.wait()
    reader.join(5)
    return code, timed_out


# ------------------------------------------------------------------ artifacts


def collect(
    patterns: list[str], cwd: Path, target: Path, out: Path, limit: int
) -> list[str]:
    """Pack the files matching ``patterns`` (``target/...`` is the target dir)."""
    found: dict[str, Path] = {}
    for pattern in patterns:
        if pattern.startswith("/") or ".." in Path(pattern).parts:
            continue
        if pattern == "target" or pattern.startswith("target/"):
            base, rest, prefix = target, pattern[len("target/") :], "target/"
        else:
            base, rest, prefix = cwd, pattern, ""
        for path in sorted(base.glob(rest)):
            if path.is_file() and not path.is_symlink():
                found[prefix + str(path.relative_to(base))] = path
    if not found:
        return []
    out.mkdir(parents=True, exist_ok=True)
    total = 0
    names = []
    with tarfile.open(out / "artifacts.tar.gz", "w:gz") as tar:
        for name, path in sorted(found.items()):
            total += path.stat().st_size
            if total > limit:
                break
            tar.add(path, arcname=name)
            names.append(name)
    return names


# ------------------------------------------------------------------ main


def _toolchain(spec: dict[str, Any], env: dict[str, str] | None = None) -> str:
    probe = spec.get("toolchain_probe")
    if not probe or shutil.which(probe[0]) is None:
        return "none"
    try:
        output = subprocess.run(
            probe, capture_output=True, timeout=120, check=False, env=env
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "none"
    return hashlib.sha256(output).hexdigest()[:10]


# ------------------------------------------------- the source's toolchain (0.20)
# 0.22: parsing and installing live in piceli.dev.toolchains (shared with host
# builds); the wrapper ships it next to this file (jobs.wrapper_source).


def _toolchain_error(error: Any) -> RunError:
    if error.kind == "checksum":
        return RunError("dev-toolchain-checksum", str(error))
    if error.kind == "failed":
        return RunError("dev-toolchain-failed", str(error))
    return RunError("dev-toolchain-invalid", str(error))


def rust_toolchain(tree: Path, cwd: str | None) -> dict[str, Any] | None:
    """The Rust toolchain the source pins, as rustup reads it: the nearest
    ``rust-toolchain.toml`` (or legacy ``rust-toolchain``) from the run's
    directory up to the tree's root; ``None`` when there is none.

    :raises RunError: ``dev-toolchain-invalid`` (an unreadable pin).
    """
    root = tree.resolve()
    here = (root / str(cwd or ".")).resolve()
    while True:
        for name in ("rust-toolchain.toml", "rust-toolchain"):
            path = here / name
            if path.is_file():
                return _parse_toolchain(path, root)
        if here == root or root not in here.parents:
            return None
        here = here.parent


def _parse_toolchain(path: Path, root: Path) -> dict[str, Any]:
    try:
        return toolchains.parse_rust(
            path.read_text(errors="replace"), path.name, str(path.relative_to(root))
        )
    except toolchains.ToolchainError as error:
        raise _toolchain_error(error) from None


def provision_rust(
    cache: Path,
    pin: dict[str, Any],
    emit: Callable[[str], None],
    clock: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Install the pinned toolchain once into the cache (``rustup``, under a
    lock, keyed by channel, targets and components) and return the
    environment that selects it, and what was done."""
    began = clock()
    try:
        installed, env = toolchains.provision_rust(
            cache, pin, lambda line: emit(f"[piceli-dev] {line}")
        )
    except toolchains.ToolchainError as error:
        raise _toolchain_error(error) from None
    report = {**pin, "installed": installed, "seconds": 0.0}
    if installed:
        report["seconds"] = round(clock() - began, 3)
    return env, report


def provision_tools(
    cache: Path,
    declared: dict[str, str],
    tree: Path,
    env: dict[str, str],
    emit: Callable[[str], None],
) -> tuple[list[str], dict[str, Any]]:
    """0.22: the profile's node, binaryen and wasm-bindgen pins (paths
    relative to the tree's root, or versions), installed once into the
    cache; returns the ``bin`` directories for ``PATH`` and the report."""
    root = tree.resolve()

    def read(path: str) -> bytes:
        where = (root / path).resolve()
        if root not in where.parents or not where.is_file():
            raise toolchains.ToolchainError("invalid", f"{path}: no such pin file")
        return where.read_bytes()

    try:
        wanted = toolchains.check_declared(declared, "toolchains")
        wanted.pop("rust", None)  # the source's own rust-toolchain.toml
        pins = toolchains.resolve(wanted, read)
        _, paths, report = toolchains.provision(
            cache, pins, lambda line: emit(f"[piceli-dev] {line}"), env=env
        )
    except toolchains.ToolchainError as error:
        raise _toolchain_error(error) from None
    return paths, report


class Timeline:
    """Contiguous phases with wall-clock times (0.20): every second of a run
    belongs to one phase, so a trace has no unattributed gap."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.items: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None

    def phase(self, name: str, at: float | None = None) -> None:
        now = self.clock() if at is None else at
        if self.current is not None:
            self.current["end"] = round(now, 3)
            self.items.append(self.current)
        self.current = {"phase": name, "start": round(now, 3)}

    def close(self) -> list[dict[str, Any]]:
        if self.current is not None:
            self.current["end"] = round(self.clock(), 3)
            self.items.append(self.current)
            self.current = None
        return list(self.items)


def main(
    spec: dict[str, Any],
    work: Path,
    cache: Path,
    *,
    emit: Callable[[str], None] = lambda line: print(line, flush=True),
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run one development build; returns (and prints) its result."""
    began = clock()
    timeline = Timeline()
    timeline.phase("upload")
    durations: dict[str, float] = {}
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "run": spec.get("run"),
        "state": "error",
        "exit_code": None,
        "reason": None,
        "durations": durations,
        "tests": None,
        "cache": {},
        "log_tail": [],
        "artifacts": [],
    }
    tail: collections.deque[str] = collections.deque(
        maxlen=int(spec.get("log_tail_lines") or 80)
    )
    lineage: Lineage | None = None
    first: FirstError | None = None
    limit: int | None = None
    watch: CacheWatch | None = None
    min_free = float(spec.get("cache_min_free_ratio") or 0)
    tree = work / "tree"
    collected = False
    for scratch in ("home", "tmp"):
        (work / scratch).mkdir(parents=True, exist_ok=True)
    try:
        archive = wait_upload(work, spec, clock)
        timeline.phase("extract")
        step = clock()
        inbox = work / "in"
        extract(archive, inbox)
        archive.unlink(missing_ok=True)
        sources: list[DeltaSource] | None = None
        full = False
        if (inbox / "manifest.json").is_file():  # 0.19: a Git delta
            sources = unbundle(inbox, cache, spec)
        else:  # a whole tree (0.18 clients, images without git)
            os.replace(inbox, tree)
        timeline.phase("isolation")
        wait_isolated(spec, clock, emit)
        timeline.phase("toolchain")
        missing = [
            tool for tool in spec.get("tools") or () if shutil.which(tool) is None
        ]
        if missing:
            raise RunError(
                "dev-tool-missing",
                "the profile's image lacks " + ", ".join(missing),
                missing_tools=missing,
            )
        # 0.20: the toolchain the source pins (rust-toolchain.toml), installed
        # once into the cache, not the image's.
        selected: dict[str, str] = {}
        if sources is not None:
            # 0.20: read from the mirror; the tree is built after the lineage
            # is known (in place when it can be).
            pin = delta_pin(sources, spec.get("cwd"), work / "pin")
            if pin is ...:  # this run changes a pin file: build the tree now
                result["applied"] = materialize(sources, tree)
                full = True
                pin = rust_toolchain(tree, spec.get("cwd"))
        else:
            pin = rust_toolchain(tree, spec.get("cwd"))
        if pin is not None:
            if spec.get("provision_toolchains", True) and shutil.which("rustup"):
                selected, done_report = provision_rust(cache, pin, emit, clock)
                result["toolchain"] = {"rust": done_report}
            else:  # a native host: its own rustup reads the file itself
                result["toolchain"] = {"rust": {**pin, "installed": False}}
        toolchain = _toolchain(spec, {**os.environ, **selected} if selected else None)
        timeline.phase("lineage")
        limit = int(spec.get("cache_max_bytes") or 1 << 62)
        budget = evict(cache, limit, min_free_ratio=min_free)
        if budget["evicted"]:
            emit(
                f"[piceli-dev] cache budget: evicted {len(budget['evicted'])} "
                f"lineage(s), {budget['freed_bytes'] / 2**30:.1f} GiB "
                f"({budget.get('evicted_for')})"
            )
        if budget["over_budget"] or budget["fs_low"]:
            emit(
                f"[piceli-dev] cache over budget: {budget['used_bytes'] / 2**30:.1f} "
                f"GiB of {limit / 2**30:.1f} GiB"
                + (", its disk is nearly full" if budget["fs_low"] else "")
                + "; piceli dev cache clear"
            )
        if toolchain != "none":
            spec = {**spec, "lineage_key": f"{spec['lineage_key']}-{toolchain}"}
        lineage = take_lineage(cache, spec, clock, sources[0] if sources else None)
        # 0.38.1: the lineage's cap is the budget less what the rest of the
        # cache held (other lineages, toolchains, cargo's home, mirrors).
        taken = _read_meta(lineage.path)
        taken_bytes = int(taken.get("bytes") or 0)
        cap = max(0, limit - (int(budget["used_bytes"]) - taken_bytes))
        seeded = None
        if lineage.seed_from is not None:
            # 0.53.0: a new lineage starts from a copy of its nearest sibling.
            try:
                seeded = seed_lineage(lineage.path, lineage.seed_from, cap)
            except OSError as error:
                emit(
                    f"[piceli-dev] seeding the lineage failed, it starts cold: {error}"
                )
            if seeded is not None:
                taken_bytes = int(seeded["bytes"])
        result["cache"] = {
            "lineage": f"{lineage.path.parent.name}/{lineage.path.name}",
            "lineage_dir": str(lineage.path),
            "warm": "seeded"
            if seeded
            else (
                lineage.warm
                if lineage.command_warm is None
                else lineage.warm and lineage.command_warm
            ),
            **(
                {"tree_warm": lineage.warm, "command_key": command_key(spec)}
                if lineage.command_warm is not None
                else {}
            ),
            **({"warm_wait_seconds": lineage.waited} if lineage.waited >= 1 else {}),
            **({"seed": seeded} if seeded else {}),
            "evicted": budget["evicted"],
            "used_bytes": budget["used_bytes"],
            "max_bytes": limit,
            "cap_bytes": cap,
            "lineage_bytes": taken_bytes,
            # 0.35.0: the whole cache counts; what eviction freed and
            # whether the cache or its disk is still over its limit.
            **{
                key: budget[key]
                for key in (
                    "other_bytes",
                    "freed_bytes",
                    "over_budget",
                    "fs_free_bytes",
                    "fs_total_bytes",
                    "fs_low",
                )
            },
        }
        emit(
            f"[piceli-dev] lineage {result['cache']['lineage']} "
            + (
                f"(seeded from {seeded['from']}: {seeded['bytes'] / 2**30:.1f} GiB "
                f"in {seeded['seconds']:.1f} s by {seeded['method']}, "
                f"{seeded['dropped_units']} unit(s) to rebuild)"
                if seeded
                else f"({_warmth(lineage)})"
            )
        )
        timeline.phase("sync")
        lineage_tree = lineage.path / "tree"
        if sources is not None:
            meta = _read_meta(lineage.path)
            previous = meta.pop("materialized", None)
            if previous is not None:
                # 0.50.0: a run killed mid-sync leaves no record, so the next
                # run syncs fully instead of trusting a stale overlay list.
                _write_meta(lineage.path, meta)
            counts = (
                refresh_in_place(sources, lineage_tree, previous, work / "stage")
                if not full and lineage_tree.is_dir()
                else None
            )
            if counts is not None:
                result["sync"] = {**counts, "mode": "in-place"}
                refreshed = int((previous or {}).get("refreshed") or 0) + 1
                result["applied"] = {
                    item.name: {
                        "mode": "delta",
                        "bundle": (item.incoming / "bundle").is_file(),
                        "diff": (item.incoming / "diff").is_file(),
                    }
                    for item in sources
                }
            else:
                if not full:
                    result["applied"] = materialize(sources, tree)
                result["sync"] = sync_tree(tree, lineage_tree)
                refreshed = 0
            _write_meta(
                lineage.path,
                {
                    **_read_meta(lineage.path),
                    "materialized": materialized(sources, refreshed),
                },
            )
        else:
            result["sync"] = sync_tree(tree, lineage_tree)
            meta = _read_meta(lineage.path)
            if meta.pop("materialized", None) is not None:
                _write_meta(lineage.path, meta)
        shutil.rmtree(tree, ignore_errors=True)
        shutil.rmtree(work / "stage", ignore_errors=True)
        shutil.rmtree(inbox, ignore_errors=True)
        durations["sync"] = round(clock() - step, 3)
        cwd = (lineage.path / "tree" / str(spec.get("cwd") or ".")).resolve()
        if lineage.path.resolve() not in cwd.parents and cwd != lineage.path.resolve():
            raise RunError(
                "dev-run-invalid",
                f"the working directory leaves the tree: {str(spec.get('cwd'))[:300]}",
            )
        target = lineage.path / "target"
        target.mkdir(exist_ok=True)
        env = {
            **os.environ,
            **{str(k): str(v) for k, v in (spec.get("env") or {}).items()},
        }
        env.update(selected)
        env["CARGO_TARGET_DIR"] = str(target)
        if not spec.get("own_cargo_home"):
            # A native host keeps the user's own ~/.cargo (its crates, and
            # --offline works); everywhere else the cache's, per toolchain.
            env["CARGO_HOME"] = str(cache / "cargo-home" / toolchain)
        env["PICELI_DEV_RUN"] = str(spec.get("run"))
        declared = dict(spec.get("toolchains") or {})
        if declared and spec.get("provision_toolchains", True):
            # 0.22: the profile's node, binaryen and wasm-bindgen pins.
            timeline.phase("toolchain")
            paths, tools = provision_tools(
                cache, declared, lineage.path / "tree", env, emit
            )
            env["PATH"] = os.pathsep.join([*paths, env.get("PATH") or os.defpath])
            result["toolchain"] = {**(result.get("toolchain") or {}), **tools}
        if spec.get("prefetch") == "cargo" and (cwd / "Cargo.lock").is_file():
            timeline.phase("fetch")
            step = clock()
            # 0.20: this lineage already fetched this exact Cargo.lock into
            # this CARGO_HOME: nothing to fetch, skip cargo's 2 s check.
            lock = hashlib.sha256((cwd / "Cargo.lock").read_bytes()).hexdigest()
            home = env.get("CARGO_HOME") or str(Path.home() / ".cargo")
            key = f"{lock}:{home}:{cwd.relative_to(lineage.path)}"
            meta = _read_meta(lineage.path)
            if meta.get("fetched") == key and Path(home).is_dir():
                result["prefetch"] = "cached"
            else:
                emit("[piceli-dev] cargo fetch --locked")
                code, _ = run_command(
                    ["cargo", "fetch", "--locked"], cwd, env, 600, emit, tail.append
                )
                result["prefetch"] = "ok" if code == 0 else "failed"
                if code == 0:
                    _write_meta(lineage.path, {**meta, "fetched": key})
            durations["fetch"] = round(clock() - step, 3)
        cargo = CargoOutput(clock)
        errors = first = FirstError(int(spec.get("first_error_lines") or 20))
        building: collections.deque[str] = collections.deque(maxlen=2000)

        health: list[dict[str, str]] = []

        def seen(line: str) -> None:
            found = health_signal((line,)) if len(health) < 8 else None
            if found is not None and all(h["kind"] != found["kind"] for h in health):
                health.append(found)
            tail.append(line)
            cargo.feed(line)
            errors.feed(line)
            if cargo.finished_at is None:  # the build, not the tests' output
                building.append(line)

        timeline.phase("command")
        step = clock()
        attempt_step = step
        command = [str(part) for part in spec["command"]]
        emit("[piceli-dev] running " + " ".join(command))
        timeout = float(spec.get("timeout_seconds") or 3600)

        room: dict[str, Any] = {}

        def make_room(lineage_bytes: int) -> int:
            """Evict free lineages; the run's new cap."""
            report = evict(cache, limit, min_free_ratio=min_free)
            room.update(report, lineage_bytes=lineage_bytes)
            if report["evicted"]:
                emit(
                    f"[piceli-dev] cache budget: evicted {len(report['evicted'])} "
                    f"free lineage(s), {report['freed_bytes'] / 2**30:.1f} GiB, "
                    "to make room for this run"
                )
            return max(0, limit - (int(report["used_bytes"]) - lineage_bytes))

        watch = CacheWatch(
            lineage.path,
            cap,
            taken_bytes,
            make_room=make_room,
            min_free_ratio=min_free,
            every=float(spec.get("cache_watch_seconds") or WATCH_SECONDS),
            measure_every=float(spec.get("cache_measure_seconds") or MEASURE_SECONDS),
            slack=int(spec.get("cache_growth_slack_bytes") or GROWTH_SLACK_BYTES),
            # 0.53.0: pause instead of stop while other runs hold lineages.
            lineages=lambda: list(room.get("lineages") or ()),
            taken_at=float(taken.get("taken_at") or time.time()),
            emit=emit,
            pause_limit=float(spec.get("cache_pause_seconds") or PAUSE_LIMIT_SECONDS),
        )

        def paused() -> bool:
            return watch is not None and watch.paused is not None

        code, timed_out = run_command(
            command, cwd, env, timeout, emit, seen, watch, paused
        )
        watch.close()
        crates, damage = (
            corrupt_crates(list(building))
            if code and not timed_out and not watch.stopped
            else ([], None)
        )
        if crates or damage == "incremental":
            # 0.19: a damaged cached artifact, not the code: clear those
            # crates and run once more (a run killed mid-write, a bad disk).
            # 0.20: an ICE in the incremental state: drop incremental/.
            timeline.phase("repair")
            removed = (
                clear_incremental(target)
                if damage == "incremental"
                else clear_crates(target, crates)
            )
            emit(
                f"[piceli-dev] corrupted cache ({damage}): cleared "
                f"{', '.join(crates) or 'incremental/'} ({removed} entries); "
                "running once more"
            )
            result["cache"]["repaired"] = {
                "kind": damage,
                "crates": crates,
                "entries": removed,
                "first_error": list(errors.lines),
            }
            cargo = CargoOutput(clock)
            errors.lines.clear()
            building.clear()
            tail.clear()
            left = max(1.0, timeout - (clock() - step) + watch.paused_seconds)
            timeline.phase("command")
            attempt_step = clock()
            code, timed_out = run_command(
                command, cwd, env, left, emit, seen, watch, paused
            )
            watch.close()
        elif damage:
            result["cache"]["suspect"] = {"kind": damage}
        ended = clock()
        current = timeline.current
        if current is not None and cargo.finished_at is not None:
            # Split the command at cargo's Finished line: build, then tests.
            current["phase"] = "build"
            timeline.phase(
                "test" if cargo.tests["suites"] else "after-build",
                at=float(current["start"]) + (cargo.finished_at - attempt_step),
            )
        durations["command"] = round(ended - step, 3)
        if cargo.finished_at is not None:
            durations["build"] = round(cargo.finished_at - step, 3)
            if cargo.tests["suites"]:
                durations["test"] = round(ended - cargo.finished_at, 3)
        if cargo.tests["suites"] or cargo.failed_names:
            result["tests"] = dict(cargo.tests)
            if cargo.failed_names:
                result["tests"]["failed_names"] = list(cargo.failed_names)
        locked = _locked_crates(cwd, lineage.path / "tree")
        result["cache"].update(
            crates_compiled=cargo.compiled,
            crates_locked=locked,
            hit_ratio=(
                round(max(0.0, 1 - cargo.compiled / locked), 3) if locked else None
            ),
        )
        result["exit_code"] = code
        repaired = result["cache"].get("repaired") or {}
        if (code and not timed_out) or repaired:
            # 0.22: what the build host's health record counts. A link error
            # alone is the code's own unless a cache repair blamed the cache.
            counted = [h for h in health if repaired or h["kind"] != "link-symbol"]
            if counted:
                result["health"] = {**counted[0], "repaired": bool(repaired)}
            elif repaired:
                result["health"] = {
                    "kind": f"cache-{repaired.get('kind')}",
                    "line": next(iter(repaired.get("first_error") or ()), ""),
                    "repaired": True,
                }
        result["cache"]["lineage_bytes"] = watch.bytes
        if watch.pauses:
            result["cache"]["paused"] = list(watch.pauses)
        if watch.stopped:
            gib = 2**30
            result["cache"].update(over_cap=True, stopped_for=watch.stopped)
            result["cache"]["cap_bytes_at_stop"] = watch.cap
            rest = (
                int(room["used_bytes"]) - int(room["lineage_bytes"]) if room else None
            )
            others = (
                max(0, int(room.get("held_bytes") or 0) - int(room["lineage_bytes"]))
                if room
                else 0
            )
            # 0.50.0: the cap that stopped the run, which shrinks while it
            # runs when other runs' lineages grow (it was the start's cap).
            # 0.53.0: other runs' lineages pause it instead; a stop is a
            # lineage alone over the budget.
            why = (
                f"its lineage held {watch.bytes / gib:.1f} GiB, over its cap of "
                f"{watch.cap / gib:.1f} GiB: the cache's budget of "
                f"{limit / gib:.1f} GiB less "
                + (
                    f"{rest / gib:.1f} GiB the rest of the cache holds"
                    + (
                        f", {others / gib:.1f} GiB of it in lineages other runs hold"
                        if others
                        else ""
                    )
                    + f" (its cap at the start was {cap / gib:.1f} GiB)"
                    if rest is not None
                    else "the rest of the cache"
                )
                if watch.stopped == "over-cap"
                else f"the cache's disk fell under {min_free:.0%} free while its "
                f"lineage grew to {watch.bytes / gib:.1f} GiB"
            )
            emit(f"[piceli-dev] run stopped: {why}; the lineage is evicted")
            result.update(
                {
                    "state": "error",
                    "reason": "dev-cache-over-cap",
                    "message": f"run stopped: {why}",
                }
            )
        elif timed_out:
            result.update(state="timed-out", reason="dev-run-timed-out")
        elif code == 0:
            result["state"] = "passed"
        else:
            result.update(state="failed", reason="dev-command-failed")
        timeline.phase("artifacts")
        result["artifacts"] = collect(
            list(spec.get("artifacts") or ()),
            cwd,
            target,
            work / "out",
            int(spec.get("artifact_max_bytes") or 512 << 20),
        )
        collected = bool(result["artifacts"])
    except RunError as error:
        result.update(
            state="error", reason=error.reason, message=str(error), **error.extra
        )
    finally:
        shutil.rmtree(tree, ignore_errors=True)
        shutil.rmtree(work / "in", ignore_errors=True)
        (work / "upload" / "tree.tar.gz").unlink(missing_ok=True)
        dropped = None
        if lineage is not None:
            try:
                if watch is not None and watch.stopped:
                    dropped = lineage.drop()
                else:
                    lineage.release(
                        spec.get("lineage_hint"),
                        str(spec.get("run")),
                        command_key(spec),
                    )
            except OSError:
                pass
        if limit is not None and lineage is not None:
            # 0.38.1: trim at the end of every run too, so the cache never
            # sits over its budget until the next run starts.
            try:
                trim = evict(cache, limit, min_free_ratio=min_free)
            except OSError:
                trim = None
            if trim is not None:
                result["cache"]["trim"] = {
                    key: trim[key]
                    for key in (
                        "evicted",
                        "freed_bytes",
                        "used_bytes",
                        "over_budget",
                        "fs_free_bytes",
                        "fs_low",
                        "lineages",  # 0.53.0: for the status
                    )
                }
                if dropped is not None:
                    result["cache"]["trim"]["dropped_bytes"] = dropped
                if trim["evicted"]:
                    emit(
                        f"[piceli-dev] cache budget after the run: evicted "
                        f"{len(trim['evicted'])} lineage(s), "
                        f"{trim['freed_bytes'] / 2**30:.1f} GiB "
                        f"({trim.get('evicted_for')})"
                    )
    if result["state"] != "passed":
        result["log_tail"] = list(tail)
        if first is not None and first.lines:
            result["first_error"] = list(first.lines)
    durations["total"] = round(clock() - began, 3)
    result["timeline"] = timeline.close()
    emit(RESULT_MARKER + json.dumps(result, sort_keys=True))
    if collected:
        done = work / "out" / "collected"
        deadline = clock() + float(spec.get("collect_timeout_seconds") or 300)
        while not done.exists() and clock() < deadline:
            time.sleep(float(spec.get("poll_seconds") or 0.5))
    return result


if __name__ == "__main__":  # pragma: no cover - runs inside the pod
    # /work and /cache in a pod or container; a native build host (0.20.0)
    # names its own directories.
    WORK = Path(os.environ.get("PICELI_DEV_WORK") or "/work")
    CACHE = Path(os.environ.get("PICELI_DEV_CACHE") or "/cache")
    if os.environ.get("PICELI_DEV_ROLE") == "fetch":
        found = Path("/etc/piceli-git")
        fetch_main(
            json.loads(os.environ["PICELI_DEV_SPEC"]),
            WORK,
            CACHE,
            found if found.is_dir() else None,
        )
    elif os.environ.get("PICELI_DEV_ROLE") == "clear":
        clear_main(json.loads(os.environ["PICELI_DEV_SPEC"]), CACHE)
    else:
        main(json.loads(os.environ["PICELI_DEV_SPEC"]), WORK, CACHE)
