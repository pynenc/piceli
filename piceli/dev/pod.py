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
   exclusive lock (the one this branch used last, else the warmest free
   one, else a new one): a stable tree path and its ``CARGO_TARGET_DIR``.
   Never two runs on one lineage.
3. Sync the tree into the lineage **keeping the mtime of unchanged files**,
   so cargo's fingerprints rebuild only what changed.
4. Optionally ``cargo fetch --locked``, then run the command with a deadline,
   streaming its output; parse cargo's output (crates compiled, build and
   test phases, test counts). 0.38.1: a lineage that grows past its cap
   while it runs stops the run (``dev-cache-over-cap``) and is evicted.
5. Evict to the budget again (0.38.1), print one ``PICELI-DEV-RESULT
   {json}`` line, copy requested artifacts to ``/work/out/artifacts.tar.gz``
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
    """
    now = time.time() if now is None else now
    cache.mkdir(parents=True, exist_ok=True)
    held = 0
    units: list[tuple[float, int, str, Callable[[], bool]]] = []
    locks: list[int] = []
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
                continue
            locks.append(descriptor)
            if "bytes" not in meta or meta.get("taken_at"):
                meta["bytes"] = du(path)
                meta["last_used"] = float(
                    meta.get("last_used") or meta.get("taken_at") or changed
                )
                meta.pop("taken_at", None)
                _write_meta(path, meta)

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
    report.update(at=now, other_at=other_at)
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
    ) -> None:
        self.lineage = lineage
        self.cap = cap
        self.start_bytes = self.bytes = start_bytes
        self.min_free_ratio = min_free_ratio
        self.every = every
        self.measure_every = measure_every
        self.slack = slack
        self.clock = clock
        self.stopped: str | None = None
        self.measured = 0
        now = clock()
        self.checked_at = self.measured_at = now
        free, total = filesystem(lineage)
        self.disk_used = total - free if total else None

    def __call__(self) -> bool:
        if self.stopped:
            return True
        now = self.clock()
        if now - self.checked_at < self.every:
            return False
        self.checked_at = now
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
        if self.bytes > self.cap:
            self.stopped = "over-cap"
        elif low and self.bytes > self.start_bytes + self.slack:
            self.stopped = "filesystem-low"
        return self.stopped is not None


class Lineage:
    """A taken lineage: its directory, lock and whether it was built before."""

    def __init__(self, path: Path, descriptor: int, warm: bool) -> None:
        self.path = path
        self.descriptor = descriptor
        self.warm = warm

    def release(self, hint: str | None, run: str) -> None:
        meta = _read_meta(self.path)
        meta.pop("taken_at", None)
        meta.update(
            last_used=time.time(),
            bytes=du(self.path),
            hint=hint,
            runs=int(meta.get("runs") or 0) + 1,
            last_run=run,
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


def take_lineage(
    cache: Path, spec: dict[str, Any], clock: Callable[[], float]
) -> Lineage:
    """The free lineage this run builds in (waits while all are busy)."""
    key = str(spec["lineage_key"])
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", key):
        raise RunError("dev-run-invalid", "the lineage key is not a safe name")
    root = cache / "lineages" / key
    root.mkdir(parents=True, exist_ok=True)
    most = max(1, int(spec.get("max_lineages") or 1))
    hint = spec.get("lineage_hint")
    deadline = clock() + float(spec.get("timeout_seconds") or 3600)
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
            if free:
                same = [item for item in free if hint and item[1].get("hint") == hint]
                chosen = (
                    same
                    or sorted(
                        free, key=lambda item: float(item[1].get("last_used") or 0)
                    )
                )[-1]
            elif len(existing) < most:
                index = next(i for i in range(len(existing) + 1) if i not in existing)
                path = root / str(index)
                descriptor = _try_lock(path)
                if descriptor is not None:
                    chosen = (index, {}, path, descriptor)
            for item in free:
                if item is not chosen:
                    os.close(item[3])
            if chosen is not None:
                _index, meta, path, descriptor = chosen
                # 0.35.0: a lineage whose run never releases it (a killed
                # pod) keeps this mark; the next eviction measures it again.
                _write_meta(path, {**meta, "taken_at": time.time()})
                return Lineage(path, descriptor, bool(meta.get("runs")))
        finally:
            os.close(select)
        if clock() >= deadline:
            raise RunError("dev-run-timed-out", "no free lineage in time")
        time.sleep(float(spec.get("poll_seconds") or 0.5))


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
#: Output that means a cached artifact is damaged, not the code: the loader
#: refusing a proc-macro ``.so``, invalid crate metadata, a linker reading a
#: truncated or garbled object (a run killed mid-write, a faulty disk).
_CORRUPT = (
    # 0.20: rustc's incremental state broke the compiler itself (an ICE in
    # dep_graph/serialized.rs on a faulty node): drop incremental/, retry.
    (
        "incremental",
        re.compile(r"internal compiler error|thread 'rustc' panicked"),
    ),
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
    (
        "internal-compiler-error",
        re.compile(r"internal compiler error|thread 'rustc' panicked"),
    ),
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
) -> tuple[int | None, bool]:
    """Run ``command``; ``(exit code, timed out)``. Its whole group is killed
    at the deadline, or (0.38.1) once ``stop()`` says so."""
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
        late = time.monotonic() >= deadline
        if (late or (stop is not None and stop())) and process.poll() is None:
            timed_out = late
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
        lineage = take_lineage(cache, spec, clock)
        # 0.38.1: the lineage's cap is the budget less what the rest of the
        # cache held (other lineages, toolchains, cargo's home, mirrors).
        taken_bytes = int(_read_meta(lineage.path).get("bytes") or 0)
        cap = max(0, limit - (int(budget["used_bytes"]) - taken_bytes))
        result["cache"] = {
            "lineage": f"{lineage.path.parent.name}/{lineage.path.name}",
            "lineage_dir": str(lineage.path),
            "warm": lineage.warm,
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
            f"({'warm' if lineage.warm else 'cold'})"
        )
        timeline.phase("sync")
        lineage_tree = lineage.path / "tree"
        if sources is not None:
            previous = _read_meta(lineage.path).get("materialized")
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
        watch = CacheWatch(
            lineage.path,
            cap,
            taken_bytes,
            min_free_ratio=min_free,
            every=float(spec.get("cache_watch_seconds") or WATCH_SECONDS),
            measure_every=float(spec.get("cache_measure_seconds") or MEASURE_SECONDS),
            slack=int(spec.get("cache_growth_slack_bytes") or GROWTH_SLACK_BYTES),
        )
        code, timed_out = run_command(command, cwd, env, timeout, emit, seen, watch)
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
            left = max(1.0, timeout - (clock() - step))
            timeline.phase("command")
            attempt_step = clock()
            code, timed_out = run_command(command, cwd, env, left, emit, seen, watch)
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
        if watch.stopped:
            gib = 2**30
            result["cache"].update(over_cap=True, stopped_for=watch.stopped)
            why = (
                f"its lineage held {watch.bytes / gib:.1f} GiB, over its cap of "
                f"{cap / gib:.1f} GiB (the cache's budget less the rest of the cache)"
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
                    lineage.release(spec.get("lineage_hint"), str(spec.get("run")))
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
