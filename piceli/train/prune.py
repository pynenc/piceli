"""Bounding what a train keeps on this machine.

Whatever a train creates it removes, and what it keeps is bounded
(:class:`~piceli.train.model.Retention`, defaults in brackets). At the end
of every run, and with ``piceli train-prune NAME [--dry-run]``:

- **trial worktrees** (``<cache>/checkouts/<train>/<repo>/<n>/``): one a
  crashed run left behind goes (a trial removes its own); never one a
  running trial holds;
- **cargo target directories** (``<cache>/targets/<train>/<repo>/*/``):
  one unused for ``target_days`` [14] goes, with its checkout path's times
  record; then the least recently used while their total passes
  ``targets_size`` [100 GiB]; never one a running trial or command holds;
- **the content store** (``<cache>/store/``, shared by every train of the
  machine): records unused for ``store_days`` [30] go, then the least
  recently used while the bytes they keep pass ``store_size`` [20 GiB];
  bytes no record keeps go once an hour old (a step being stored writes
  its bytes before its record), and so do interrupted partial files;
- **run records, events and logs** (``<state>/runs/<run>/``): runs older
  than ``log_days`` [30] go, but never the newest ``keep_runs`` [20], the
  run in progress (the lock names it), a run serving a queued request or
  named by a queued ``--promote-run``, nor the newest ``keep_runs`` runs
  that promoted (what ``piceli trains`` matches deployed targets to). The flaky registry
  (``flaky.jsonl``), the queue (``queue/``) and the lock stay.

The result (``piceli.train-prune.v1``) lists what went (path, kind, bytes,
why) and the sizes kept per kind, with their limits.

Importing this module is side-effect free.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from piceli.train.model import Retention

SCHEMA = "piceli.train-prune.v1"
DAY = 86400.0
#: Store bytes no record keeps go once they are this old.
ORPHAN_SECONDS = 3600.0


def parse_size(text: str) -> int:
    from piceli.maintenance.cache import parse_size as parse

    return parse(text.strip())


def disk_usage(path: Path) -> int:
    """Bytes ``path`` uses on disk (blocks; links are not followed)."""
    total = 0
    try:
        info = path.lstat()
    except OSError:
        return 0
    total += getattr(info, "st_blocks", 0) * 512 or info.st_size
    if path.is_dir() and not path.is_symlink():
        try:
            entries = list(os.scandir(path))
        except OSError:
            return total
        for entry in entries:
            total += disk_usage(Path(entry.path))
    return total


@contextmanager
def _held(lock: Path) -> Iterator[bool]:
    """Try to take ``lock`` (no wait): ``True`` when taken (nobody uses what
    it guards), held until the block exits."""
    if not lock.parent.is_dir():
        yield True
        return
    with lock.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass
class Pruning:
    """One prune: what went and what is kept."""

    dry_run: bool
    removed: list[dict[str, Any]] = field(default_factory=list)
    sizes: dict[str, int] = field(default_factory=dict)

    def drop(self, path: Path, kind: str, size: int, why: str) -> None:
        self.removed.append(
            {"path": str(path), "kind": kind, "bytes": size, "why": why}
        )
        if self.dry_run:
            return
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def _used(directory: Path) -> float:
    from piceli.train.local import USED_MARKER

    for candidate in (directory / USED_MARKER, directory):
        try:
            return candidate.stat().st_mtime
        except OSError:
            continue
    return 0.0


def _lock_of(target: Path, cache: Path, train: str) -> Path:
    """The lock that guards a target directory while it is used."""
    if target.name.startswith("slot-"):
        return cache / "slots" / f"local-{target.name[len('slot-') :]}.lock"
    repo = target.parent.name
    return cache / "checkouts" / train / repo / f"lock-{target.name}.lock"


def _checkouts(job: Pruning, cache: Path, train: str) -> int:
    kept = 0
    for repo in sorted((cache / "checkouts" / train).glob("*")):
        for path in sorted(repo.iterdir()) if repo.is_dir() else ():
            if not path.is_dir():
                kept += disk_usage(path)
                continue
            with _held(repo / f"lock-{path.name}.lock") as free:
                if free:
                    job.drop(path, "checkout", disk_usage(path), "left by a run")
                else:
                    kept += disk_usage(path)
    return kept


def _targets(
    job: Pruning, cache: Path, train: str, retention: Retention, now: float
) -> int:
    found = []
    for repo in sorted((cache / "targets" / train).glob("*")):
        for path in sorted(repo.iterdir()) if repo.is_dir() else ():
            if path.is_dir():
                found.append((_used(path), path, disk_usage(path)))
    found.sort()
    total = sum(size for _used_at, _path, size in found)
    limit = parse_size(retention.targets_size)
    for used, path, size in found:
        old = now - used > retention.target_days * DAY
        if not old and total <= limit:
            continue
        with _held(_lock_of(path, cache, train)) as free:
            if not free:
                continue
            why = (
                f"unused for {retention.target_days}+ days"
                if old
                else f"over {retention.targets_size} (least recently used)"
            )
            job.drop(path, "target", size, why)
            times = (
                cache
                / "checkouts"
                / train
                / path.parent.name
                / (f"{path.name}.times.json")
            )
            if times.exists():
                job.drop(times, "times", disk_usage(times), "its target went")
            total -= size
    return total


def _store(job: Pruning, store: Path, retention: Retention, now: float) -> int:
    records = []
    for path in sorted((store / "keys").glob("*.json")):
        try:
            body = json.loads(path.read_text())
            used = path.stat().st_mtime
        except (OSError, ValueError):
            if now - _mtime(path) > ORPHAN_SECONDS:
                job.drop(path, "store-record", disk_usage(path), "unreadable")
            continue
        outputs = [
            (str(item.get("sha256")), int(item.get("size") or 0))
            for item in body.get("outputs") or ()
            if isinstance(item, dict)
        ]
        records.append((used, path, outputs))
    records.sort()
    refs: dict[str, int] = {}
    for _used_at, _path, outputs in records:
        for digest, _size in outputs:
            refs[digest] = refs.get(digest, 0) + 1
    sizes = {digest: _blob_size(store, digest) for digest in refs}
    total = sum(sizes.values())
    limit = parse_size(retention.store_size)
    for used, path, outputs in records:
        old = now - used > retention.store_days * DAY
        if not old and total <= limit:
            continue
        why = (
            f"unused for {retention.store_days}+ days"
            if old
            else f"over {retention.store_size} (least recently used)"
        )
        job.drop(path, "store-record", disk_usage(path), why)
        for digest, _size in outputs:
            refs[digest] -= 1
            if refs[digest] == 0:
                total -= sizes.get(digest, 0)
    kept = 0
    for blob in sorted((store / "blobs").glob("*/*")):
        name = blob.name
        digest = "sha256:" + name if not name.startswith(".") else ""
        if refs.get(digest, 0) > 0:
            kept += disk_usage(blob)
            continue
        if now - _mtime(blob) > ORPHAN_SECONDS:
            job.drop(blob, "store-blob", disk_usage(blob), "no record keeps it")
        else:
            kept += disk_usage(blob)
    for partial in sorted((store / "blobs").glob(".partial-*")):
        if now - _mtime(partial) > ORPHAN_SECONDS:
            job.drop(partial, "store-partial", disk_usage(partial), "interrupted")
    return kept + sum(
        disk_usage(path) for path in (store / "keys").glob("*.json") if path.exists()
    )


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _blob_size(store: Path, digest: str) -> int:
    hexa = digest.split(":", 1)[-1]
    return disk_usage(store / "blobs" / hexa[:2] / hexa)


def _needed(state_root: Path, found: list[Path], retention: Retention) -> set[str]:
    """Runs still read, whatever their age: the run holding the lock, the
    run serving a queued request and the run a queued ``--promote-run``
    names, and the newest ``keep_runs`` runs that promoted (what
    ``piceli trains`` matches deployed targets to)."""
    from piceli.train.queue import TrainQueue
    from piceli.train.state import TrainState

    state = TrainState(state_root)
    needed: set[str] = set()
    owner = state.owner()
    if owner and isinstance(owner.get("run"), str):
        needed.add(owner["run"])
    for request in TrainQueue(state).pending():
        for key in ("run", "promote_run"):
            if request.get(key):
                needed.add(str(request[key]))
    promoted = 0
    for path in found:
        if promoted >= retention.keep_runs:
            break
        record = state.record(path.name) or {}
        if (record.get("promote") or {}).get("state") in {
            "promoted",
            "publish-failed",
        }:
            needed.add(path.name)
            promoted += 1
    return needed


def _runs(
    job: Pruning,
    state_root: Path,
    retention: Retention,
    now: float,
    current: str | None,
) -> int:
    runs = state_root / "runs"
    found = sorted(
        (path for path in runs.glob("*") if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )
    needed = _needed(state_root, found, retention) if found else set()
    kept = 0
    for index, path in enumerate(found):
        record = path / "run.json"
        age = now - _mtime(record if record.exists() else path)
        if (
            index >= retention.keep_runs
            and age > retention.log_days * DAY
            and path.name != current
            and path.name not in needed
        ):
            job.drop(
                path, "run", disk_usage(path), f"older than {retention.log_days} days"
            )
        else:
            kept += disk_usage(path)
    return kept


def prune(
    train: str,
    *,
    cache: Path,
    state_root: Path,
    retention: Retention | None = None,
    dry_run: bool = False,
    current: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Prune one train's worktrees, targets and runs, and the machine's
    content store (see the module doc); the ``piceli.train-prune.v1`` result.

    :param state_root: The train's state directory (``.../train/<name>``).
    :param current: A run that is never pruned (the one running).
    """
    retention = retention or Retention()
    moment = time.time() if now is None else now
    job = Pruning(dry_run)
    job.sizes["checkouts"] = _checkouts(job, cache, train)
    job.sizes["targets"] = _targets(job, cache, train, retention, moment)
    job.sizes["store"] = _store(job, cache / "store", retention, moment)
    job.sizes["runs"] = _runs(job, state_root, retention, moment, current)
    return {
        "schema": SCHEMA,
        "train": train,
        "dry_run": dry_run,
        "cache": str(cache),
        "state": str(state_root),
        "removed": job.removed,
        "freed": sum(int(item["bytes"]) for item in job.removed),
        "sizes": job.sizes,
        "limits": retention.to_dict(),
    }
