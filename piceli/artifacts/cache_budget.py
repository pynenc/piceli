"""The budget of a build cache claim (0.35.0).

A cluster build Job (``piceli build job-run``) keeps its cargo targets,
staged contexts and blobs on a cache claim. On a storage class without
quotas (k3s ``local-path``) nothing stops that claim from growing past its
size until the node's disk is full. So before each build the Job measures
the claim and evicts the least recently used **units** (each
``target/<arch>-<page size>``, ``target/shared``, each ``stage/<name>`` and
``blobs``) while the cache holds more than its budget (by default 80% of
the claim) or its filesystem has less than 15% free.

Host builds record the units they use in ``.piceli-used.json`` at the
cache's root (:func:`mark_used`); a unit never recorded counts as used when
its directory last changed. The report (``piceli.cache-budget.v1``) goes
into the build receipt as ``cache``.

Importing this module is side-effect free.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

#: Environment variables of a build Job: its cache's budget in bytes and
#: the share of the filesystem to keep free.
MAX_BYTES_ENV = "PICELI_CACHE_MAX_BYTES"
MIN_FREE_ENV = "PICELI_CACHE_MIN_FREE_RATIO"
USED_FILE = ".piceli-used.json"
#: Top-level directories whose children are units; other top-level
#: directories are one unit each.
_SPLIT = ("target", "stage")
_EVICTABLE = (*_SPLIT, "blobs")


def mark_used(cache: Path, units: Iterable[Path], now: float | None = None) -> None:
    """Record that a build uses ``units`` (directories under ``cache``) now."""
    now = time.time() if now is None else now
    names = []
    for unit in units:
        try:
            names.append(unit.relative_to(cache).as_posix())
        except ValueError:
            continue
    if not names:
        return
    try:
        cache.mkdir(parents=True, exist_ok=True)
        with (cache / f"{USED_FILE}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            known = _used(cache)
            known.update(dict.fromkeys(names, now))
            partial = cache / f".{USED_FILE}.partial"
            partial.write_text(json.dumps(known, sort_keys=True))
            os.replace(partial, cache / USED_FILE)
    except OSError:
        pass  # bookkeeping, never a reason to fail a build


def _used(cache: Path) -> dict[str, float]:
    try:
        value = json.loads((cache / USED_FILE).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(k): float(v) for k, v in value.items() if isinstance(v, int | float)}


def _units(cache: Path) -> list[Path]:
    found: list[Path] = []
    for name in _EVICTABLE:
        top = cache / name
        if not top.is_dir() or top.is_symlink():
            continue
        if name in _SPLIT:
            found += sorted(
                child
                for child in top.iterdir()
                if child.is_dir() and not child.is_symlink()
            )
        else:
            found.append(top)
    return found


def enforce(
    cache: Path,
    max_bytes: int,
    *,
    min_free_ratio: float = 0.0,
    now: float | None = None,
) -> dict[str, Any]:
    """Evict least recently used units while ``cache`` is over ``max_bytes``
    or its filesystem has less than ``min_free_ratio`` free; the report."""
    from piceli.dev.pod import _remove_tree, du, other_bytes, shrink

    now = time.time() if now is None else now
    used = _used(cache)
    units: list[tuple[float, int, str, Callable[[], bool]]] = []
    for path in _units(cache):
        name = path.relative_to(cache).as_posix()
        try:
            at = used.get(name, path.stat().st_mtime)
        except OSError:
            continue

        def remove(path: Path = path) -> bool:
            _remove_tree(path)
            return True

        units.append((at, du(path), name, remove))
    skip = (*_EVICTABLE, USED_FILE, f"{USED_FILE}.lock")
    other, _at = other_bytes(cache, skip, now)
    report = shrink(cache, units, 0, other, max_bytes, min_free_ratio)
    if report["evicted"]:
        left = _used(cache)
        for name in report["evicted"]:
            left.pop(name, None)
        try:
            (cache / USED_FILE).write_text(json.dumps(left, sort_keys=True))
        except OSError:
            pass
    report["at"] = now
    return report


def from_environment(
    cache: Path, environ: Any = None, say: Any = None
) -> dict[str, Any] | None:
    """:func:`enforce` with the Job's budget (:data:`MAX_BYTES_ENV`); ``None``
    when the Job declares none."""
    environ = os.environ if environ is None else environ
    try:
        max_bytes = int(environ.get(MAX_BYTES_ENV) or 0)
        min_free = float(environ.get(MIN_FREE_ENV) or 0)
    except ValueError:
        return None
    if max_bytes <= 0 or not cache.is_dir():
        return None
    report = enforce(cache, max_bytes, min_free_ratio=min_free)
    if say is not None:
        gib = 2**30
        if report["evicted"]:
            say(
                f"[build] cache budget: evicted {', '.join(report['evicted'])} "
                f"({report['freed_bytes'] / gib:.1f} GiB, {report.get('evicted_for')})"
            )
        if report["over_budget"] or report["fs_low"]:
            say(
                f"[build] cache over budget: {report['used_bytes'] / gib:.1f} GiB "
                f"of {max_bytes / gib:.1f} GiB"
                + (", its disk is nearly full" if report["fs_low"] else "")
            )
    return report


def job_env(storage: str, budget: float) -> list[dict[str, str]]:
    """The build Job's budget variables for a claim of ``storage``."""
    from piceli.dev.model import bytes_of
    from piceli.dev.pod import MIN_FREE_RATIO

    return [
        {"name": MAX_BYTES_ENV, "value": str(int(bytes_of(storage) * budget))},
        {"name": MIN_FREE_ENV, "value": str(MIN_FREE_RATIO)},
    ]
