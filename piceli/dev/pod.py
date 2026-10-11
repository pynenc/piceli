"""The wrapper a development-build run executes inside its pod (0.18.0).

Standard library only: the Job runs this file's source with ``python3 -I
-c``, so any image with ``python3`` works and the wrapper always matches
the client that created the run. Its input is the JSON spec in
``PICELI_DEV_SPEC``; its scratch is ``/work`` (the pod's emptyDir) and the
shared cache is ``/cache`` (the ``piceli-dev-cache`` claim).

1. Wait for the client's upload (``/work/upload/tree.tar.gz`` and
   ``ready``), check its SHA-256, extract it (no absolute or escaping
   paths).
2. Check the profile's tools, evict least recently used free lineages over
   the cache size, and take a **lineage** of the run's key under an
   exclusive lock (the one this branch used last, else the warmest free
   one, else a new one): a stable tree path and its ``CARGO_TARGET_DIR``.
   Never two runs on one lineage.
3. Sync the tree into the lineage **keeping the mtime of unchanged files**,
   so cargo's fingerprints rebuild only what changed.
4. Optionally ``cargo fetch --locked``, then run the command with a deadline,
   streaming its output; parse cargo's output (crates compiled, build and
   test phases, test counts).
5. Print one ``PICELI-DEV-RESULT {json}`` line, copy requested artifacts
   to ``/work/out/artifacts.tar.gz`` and wait for the client to take them.

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
from collections.abc import Callable
from pathlib import Path
from typing import Any

RESULT_MARKER = "PICELI-DEV-RESULT "
SCHEMA = "piceli.dev-pod-result.v1"
_COMPILING = re.compile(r"^\s*Compiling \S+ v")
_FINISHED = re.compile(r"^\s*Finished `")
_TESTS = re.compile(
    r"test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored"
)


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
                if name.startswith("/") or ".." in Path(name).parts:
                    raise RunError(
                        "dev-upload-failed", "the archive holds an unsafe path"
                    )
                if not (member.isfile() or member.isdir() or member.issym()):
                    raise RunError(
                        "dev-upload-failed", "the archive holds a special file"
                    )
                if member.issym():
                    target = (root / Path(name).parent / member.linkname).resolve()
                    if root not in (target, *target.parents):
                        raise RunError(
                            "dev-upload-failed",
                            "the archive holds a link out of the tree",
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


def apply_delta(
    inbox: Path, tree: Path, cache: Path, spec: dict[str, Any]
) -> dict[str, Any]:
    """Materialize each source of a delta upload into ``tree/<name>``."""
    manifest = json.loads((inbox / "manifest.json").read_text())
    keys = {str(s.get("name")): str(s.get("key")) for s in spec.get("sources") or ()}
    applied: dict[str, Any] = {}
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
        target = tree / name
        target.mkdir(parents=True, exist_ok=True)
        archive = subprocess.Popen(
            ["git", "--git-dir", str(mirror), "archive", "--format=tar", head],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        assert archive.stdout is not None
        with tarfile.open(fileobj=archive.stdout, mode="r|") as stream:
            if sys.version_info >= (3, 12):  # noqa: UP036 - any image's python3
                stream.extractall(target, filter="tar")
            else:  # pragma: no cover
                stream.extractall(target)
        if archive.wait() != 0:
            raise RunError("dev-upload-failed", f"git archive of {name} failed")
        if (incoming / "diff").is_file():
            done = _git(
                "apply",
                "--binary",
                "--whitespace=nowarn",
                str(incoming / "diff"),
                cwd=target,
            )
            if done.returncode != 0:
                raise RunError(
                    "dev-upload-failed",
                    f"the uncommitted changes of {name} do not apply",
                )
        if (incoming / "untracked.tar").is_file():
            extract_plain(incoming / "untracked.tar", target)
        applied[name] = {
            "mode": "delta",
            "bundle": (incoming / "bundle").is_file(),
            "diff": (incoming / "diff").is_file(),
        }
    return applied


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


def evict(cache: Path, limit: int) -> tuple[list[str], int]:
    """Remove least recently used free lineages until the recorded bytes fit;
    the removed lineages and the bytes still recorded."""
    lineages = cache / "lineages"
    found = []
    for meta in lineages.glob("*/*/meta.json"):
        data = _read_meta(meta.parent)
        found.append(
            (
                float(data.get("last_used") or 0),
                int(data.get("bytes") or 0),
                meta.parent,
            )
        )
    total = sum(size for _used, size, _path in found)
    removed: list[str] = []
    for _used, size, path in sorted(found, key=lambda item: item[0]):
        if total <= limit:
            break
        descriptor = _try_lock(path)
        if descriptor is None:
            continue  # in use: never removed
        try:
            for child in list(path.iterdir()):
                if child.name != "lock":
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
        finally:
            os.close(descriptor)
        shutil.rmtree(path, ignore_errors=True)
        total -= size
        removed.append(f"{path.parent.name}/{path.name}")
    return removed, total


class Lineage:
    """A taken lineage: its directory, lock and whether it was built before."""

    def __init__(self, path: Path, descriptor: int, warm: bool) -> None:
        self.path = path
        self.descriptor = descriptor
        self.warm = warm

    def release(self, hint: str | None, run: str) -> None:
        meta = _read_meta(self.path)
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
    counts = {"written": 0, "removed": 0, "kept": 0}
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
    return counts


# ------------------------------------------------------------------ command


# ------------------------------------------------- corrupted cargo caches (0.19)
#: Output that means a cached artifact is damaged, not the code: the loader
#: refusing a proc-macro ``.so``, invalid crate metadata, a linker reading a
#: truncated or garbled object (a run killed mid-write, a faulty disk).
_CORRUPT = (
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
            r"undefined hidden symbol|file truncated|file format not recognized"
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
    """The cache-clear Job's entry (``PICELI_DEV_ROLE=clear``)."""
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
        self.tests = {"passed": 0, "failed": 0, "ignored": 0, "suites": 0}

    def feed(self, line: str) -> None:
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
) -> tuple[int | None, bool]:
    """Run ``command``; ``(exit code, timed out)``. Its whole group is killed
    at the deadline."""
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
        if time.monotonic() >= deadline and process.poll() is None:
            timed_out = True
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


def _toolchain(spec: dict[str, Any]) -> str:
    probe = spec.get("toolchain_probe")
    if not probe or shutil.which(probe[0]) is None:
        return "none"
    try:
        output = subprocess.run(
            probe, capture_output=True, timeout=30, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "none"
    return hashlib.sha256(output).hexdigest()[:10]


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
    tree = work / "tree"
    collected = False
    for scratch in ("home", "tmp"):
        (work / scratch).mkdir(parents=True, exist_ok=True)
    try:
        archive = wait_upload(work, spec, clock)
        step = clock()
        inbox = work / "in"
        extract(archive, inbox)
        archive.unlink(missing_ok=True)
        if (inbox / "manifest.json").is_file():  # 0.19: a Git delta
            result["applied"] = apply_delta(inbox, tree, cache, spec)
            shutil.rmtree(inbox, ignore_errors=True)
        else:  # a whole tree (0.18 clients, images without git)
            os.replace(inbox, tree)
        wait_isolated(spec, clock, emit)
        missing = [
            tool for tool in spec.get("tools") or () if shutil.which(tool) is None
        ]
        if missing:
            raise RunError(
                "dev-tool-missing",
                "the profile's image lacks " + ", ".join(missing),
                missing_tools=missing,
            )
        toolchain = _toolchain(spec)
        limit = int(spec.get("cache_max_bytes") or 1 << 62)
        evicted, used = evict(cache, limit)
        if toolchain != "none":
            spec = {**spec, "lineage_key": f"{spec['lineage_key']}-{toolchain}"}
        lineage = take_lineage(cache, spec, clock)
        result["cache"] = {
            "lineage": f"{lineage.path.parent.name}/{lineage.path.name}",
            "lineage_dir": str(lineage.path),
            "warm": lineage.warm,
            "evicted": evicted,
            "used_bytes": used,
            "max_bytes": limit,
        }
        emit(
            f"[piceli-dev] lineage {result['cache']['lineage']} "
            f"({'warm' if lineage.warm else 'cold'})"
        )
        result["sync"] = sync_tree(tree, lineage.path / "tree")
        shutil.rmtree(tree, ignore_errors=True)
        durations["sync"] = round(clock() - step, 3)
        cwd = (lineage.path / "tree" / str(spec.get("cwd") or ".")).resolve()
        if lineage.path.resolve() not in cwd.parents and cwd != lineage.path.resolve():
            raise RunError("dev-run-invalid", "the working directory leaves the tree")
        target = lineage.path / "target"
        target.mkdir(exist_ok=True)
        env = {
            **os.environ,
            **{str(k): str(v) for k, v in (spec.get("env") or {}).items()},
        }
        env["CARGO_TARGET_DIR"] = str(target)
        env["CARGO_HOME"] = str(cache / "cargo-home" / toolchain)
        env["PICELI_DEV_RUN"] = str(spec.get("run"))
        if spec.get("prefetch") == "cargo" and (cwd / "Cargo.lock").is_file():
            step = clock()
            emit("[piceli-dev] cargo fetch --locked")
            code, _ = run_command(
                ["cargo", "fetch", "--locked"], cwd, env, 600, emit, tail.append
            )
            durations["fetch"] = round(clock() - step, 3)
            result["prefetch"] = "ok" if code == 0 else "failed"
        cargo = CargoOutput(clock)
        errors = first = FirstError(int(spec.get("first_error_lines") or 20))
        building: collections.deque[str] = collections.deque(maxlen=2000)

        def seen(line: str) -> None:
            tail.append(line)
            cargo.feed(line)
            errors.feed(line)
            if cargo.finished_at is None:  # the build, not the tests' output
                building.append(line)

        step = clock()
        command = [str(part) for part in spec["command"]]
        emit("[piceli-dev] running " + " ".join(command))
        timeout = float(spec.get("timeout_seconds") or 3600)
        code, timed_out = run_command(command, cwd, env, timeout, emit, seen)
        crates, damage = (
            corrupt_crates(list(building)) if code and not timed_out else ([], None)
        )
        if crates:
            # 0.19: a damaged cached artifact, not the code: clear those
            # crates and run once more (a run killed mid-write, a bad disk).
            removed = clear_crates(target, crates)
            emit(
                f"[piceli-dev] corrupted cache ({damage}): cleared "
                f"{', '.join(crates)} ({removed} entries); running once more"
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
            code, timed_out = run_command(command, cwd, env, left, emit, seen)
        elif damage:
            result["cache"]["suspect"] = {"kind": damage}
        ended = clock()
        durations["command"] = round(ended - step, 3)
        if cargo.finished_at is not None:
            durations["build"] = round(cargo.finished_at - step, 3)
            if cargo.tests["suites"]:
                durations["test"] = round(ended - cargo.finished_at, 3)
        if cargo.tests["suites"]:
            result["tests"] = dict(cargo.tests)
        locked = _locked_crates(cwd, lineage.path / "tree")
        result["cache"].update(
            crates_compiled=cargo.compiled,
            crates_locked=locked,
            hit_ratio=(
                round(max(0.0, 1 - cargo.compiled / locked), 3) if locked else None
            ),
        )
        result["exit_code"] = code
        if timed_out:
            result.update(state="timed-out", reason="dev-run-timed-out")
        elif code == 0:
            result["state"] = "passed"
        else:
            result.update(state="failed", reason="dev-command-failed")
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
        if lineage is not None:
            try:
                lineage.release(spec.get("lineage_hint"), str(spec.get("run")))
            except OSError:
                pass
    if result["state"] != "passed":
        result["log_tail"] = list(tail)
        if first is not None and first.lines:
            result["first_error"] = list(first.lines)
    durations["total"] = round(clock() - began, 3)
    emit(RESULT_MARKER + json.dumps(result, sort_keys=True))
    if collected:
        done = work / "out" / "collected"
        deadline = clock() + float(spec.get("collect_timeout_seconds") or 300)
        while not done.exists() and clock() < deadline:
            time.sleep(float(spec.get("poll_seconds") or 0.5))
    return result


if __name__ == "__main__":  # pragma: no cover - runs inside the pod
    if os.environ.get("PICELI_DEV_ROLE") == "fetch":
        found = Path("/etc/piceli-git")
        fetch_main(
            json.loads(os.environ["PICELI_DEV_SPEC"]),
            Path("/work"),
            Path("/cache"),
            found if found.is_dir() else None,
        )
    elif os.environ.get("PICELI_DEV_ROLE") == "clear":
        clear_main(json.loads(os.environ["PICELI_DEV_SPEC"]), Path("/cache"))
    else:
        main(json.loads(os.environ["PICELI_DEV_SPEC"]), Path("/work"), Path("/cache"))
