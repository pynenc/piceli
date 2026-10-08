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


def wait_upload(work: Path, spec: dict[str, Any], clock: Callable[[], float]) -> Path:
    """The uploaded archive once ``ready`` exists and its digest matches."""
    ready = work / "upload" / "ready"
    deadline = clock() + float(spec.get("upload_timeout_seconds") or 600)
    while not ready.exists():
        if clock() >= deadline:
            raise RunError("dev-upload-failed", "no upload arrived in time")
        time.sleep(float(spec.get("poll_seconds") or 0.5))
    archive = work / "upload" / "tree.tar.gz"
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != spec.get("archive_sha256"):
        raise RunError("dev-upload-failed", "the upload does not match its SHA-256")
    return archive


def extract(archive: Path, into: Path) -> None:
    """Extract regular files, directories and in-tree symlinks only."""
    into.mkdir(parents=True, exist_ok=True)
    root = into.resolve()
    try:
        with tarfile.open(archive, "r:gz") as tar:
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
    tree = work / "tree"
    collected = False
    for scratch in ("home", "tmp"):
        (work / scratch).mkdir(parents=True, exist_ok=True)
    try:
        archive = wait_upload(work, spec, clock)
        step = clock()
        extract(archive, tree)
        archive.unlink(missing_ok=True)
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

        def seen(line: str) -> None:
            tail.append(line)
            cargo.feed(line)

        step = clock()
        command = [str(part) for part in spec["command"]]
        emit("[piceli-dev] running " + " ".join(command))
        code, timed_out = run_command(
            command, cwd, env, float(spec.get("timeout_seconds") or 3600), emit, seen
        )
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
        (work / "upload" / "tree.tar.gz").unlink(missing_ok=True)
        if lineage is not None:
            try:
                lineage.release(spec.get("lineage_hint"), str(spec.get("run")))
            except OSError:
                pass
    if result["state"] != "passed":
        result["log_tail"] = list(tail)
    durations["total"] = round(clock() - began, 3)
    emit(RESULT_MARKER + json.dumps(result, sort_keys=True))
    if collected:
        done = work / "out" / "collected"
        deadline = clock() + float(spec.get("collect_timeout_seconds") or 300)
        while not done.exists() and clock() < deadline:
            time.sleep(float(spec.get("poll_seconds") or 0.5))
    return result


if __name__ == "__main__":  # pragma: no cover - runs inside the pod
    main(json.loads(os.environ["PICELI_DEV_SPEC"]), Path("/work"), Path("/cache"))
