"""What Piceli keeps on a runner's disk, and a prune that stays safe.

A pipeline's state directory holds, by category:

``builds``
    Build outputs (``builds/<name>/outputs/``) and build logs: the local
    build cache. Not needed by any later stage or by a rollback.
``toolchains``, ``blobs``
    Toolchain targets and base or mirror blobs, when a builder keeps them in
    the state directory (``toolchains/``, ``blobs/``).
``receipts``
    Build, delivery and mirror receipts (``builds/<name>/receipt.json``,
    ``deliveries/``, ``mirrors/``).
``runs``
    Run journals and run summaries (``runs/``).
``release``
    The release state (``release/``, ``registry/``): catalog, execution
    journal, secret store, history, approved plans, backups and checks.
    **Never pruned.**
``other``, ``temp``
    Locks, keys and markers; partial files an interrupted write left behind.

Temporary directories (``piceli-<purpose>-*`` in the system temporary
directory) are reported apart: they belong to no state directory.

:func:`prune` removes, oldest first, and never anything in ``release/``,
``registry/`` or ``mirrors/``, a build receipt, the latest run, a run that
can still be resumed, or the latest run of each of the last ``keep_last``
applied releases (what a rollback of them may need):

1. temporary directories older than :data:`~piceli.tempfiles.STALE_SECONDS`
   (a killed process left them) and stale partial files;
2. runs beyond the last ``keep_last`` and the delivery receipts no kept run
   or current build uses (the next deploy delivers such an image again);
3. only when the state directory is still over ``budget``: build outputs and
   logs, then further old runs.

With shared state (``state="cluster"``) the state directory is a working
copy: only machine-local files (step 1, build outputs and logs) are pruned.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from piceli.pipeline.model import Pipeline

STATUS_SCHEMA = "piceli.cache-status.v1"
PRUNE_SCHEMA = "piceli.cache-prune.v1"
CATEGORIES = (
    "builds",
    "toolchains",
    "blobs",
    "receipts",
    "runs",
    "release",
    "other",
    "temp",
)
#: Runs kept by default (``--keep-last``).
DEFAULT_KEEP_LAST = 10
#: A partial file (``.<name>.<random>``) older than this was left by a
#: killed write.
STALE_PARTIAL_SECONDS = 3600
_PARTIAL = re.compile(r"\..+\.([a-z0-9_]{8}|\d+\.partial|piceli-partial)")
_RUN_ID = re.compile(r"\d{8}T\d{6}\d*Z-[0-9a-f]{8}")
_UNITS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1000,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1000**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1000**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1000**4,
    "tib": 1024**4,
}
_SIZE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*")


class CacheError(ValueError):
    """A cache command's input is invalid (``code`` is a registered error code)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def parse_size(value: str | int) -> int:
    """Bytes from ``20GiB``, ``500MB``, ``1.5G`` or an integer (binary for
    ``K``/``M``/``G``/``T`` and ``KiB``…, decimal for ``KB``/``MB``…).

    :raises CacheError: ``cache-budget-invalid``.
    """
    if isinstance(value, bool):
        raise CacheError("cache-budget-invalid", "a size is bytes or text like 20GiB")
    if isinstance(value, int):
        number, unit = float(value), ""
    else:
        match = _SIZE.fullmatch(str(value))
        if match is None or match[2].lower() not in _UNITS:
            raise CacheError(
                "cache-budget-invalid",
                f"{value!r} is not a size (examples: 20GiB, 500MB, 1073741824)",
            )
        number, unit = float(match[1]), match[2].lower()
    size = int(number * _UNITS[unit])
    if size <= 0:
        raise CacheError("cache-budget-invalid", "a cache budget must be positive")
    return size


def format_size(size: int) -> str:
    """``1.5 GiB``-style text for human output."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


# ------------------------------------------------------------ state dirs


@dataclass(frozen=True)
class StateDir:
    """One state directory to report on or prune.

    :param path: The directory.
    :param pipeline: The app name, when known.
    :param environment: The environment it belongs to (``--env``).
    :param backend: ``local``, ``cluster`` or ``unknown`` (a bare
        ``--state-dir``: treated as ``local``).
    :param budget: The pipeline's ``cache_budget`` in bytes.
    """

    path: Path
    pipeline: str | None = None
    environment: str | None = None
    backend: str = "unknown"
    budget: int | None = None

    @property
    def shared(self) -> bool:
        return self.backend == "cluster"

    def describe(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "pipeline": self.pipeline,
            "environment": self.environment,
            "backend": self.backend,
        }


def state_dirs(
    pipeline: Pipeline | None = None, *, state_dir: Path | None = None
) -> list[StateDir]:
    """The state directories of a pipeline (each environment's too), or of
    ``state_dir`` and the environment directories below it."""
    if pipeline is not None:
        budget = getattr(pipeline, "cache_budget", None)
        backend = pipeline.state.backend
        name = pipeline.app.name
        if pipeline.environment is not None:
            return [
                StateDir(
                    pipeline.state_dir, name, pipeline.environment.name, backend, budget
                )
            ]
        base = [StateDir(pipeline.state_dir, name, None, backend, budget)]
        return base + [
            StateDir(
                pipeline.state_dir / "environments" / env, name, env, backend, budget
            )
            for env in sorted(pipeline.targets)
        ]
    assert state_dir is not None
    found = [StateDir(state_dir)]
    environments = state_dir / "environments"
    if environments.is_dir() and not environments.is_symlink():
        found += [
            StateDir(item, environment=item.name)
            for item in sorted(environments.iterdir())
            if item.is_dir() and not item.is_symlink()
        ]
    return found


# -------------------------------------------------------------- scanning


def category(relative: PurePosixPath) -> str:
    """The category of a file inside a state directory (see the module doc)."""
    parts = relative.parts
    if any(part.startswith(".") for part in parts) and _PARTIAL.fullmatch(parts[-1]):
        return "temp"
    head = parts[0]
    if head == "builds":
        if len(parts) >= 3 and (parts[2] == "outputs" or parts[2].endswith(".log")):
            return "builds"
        return "receipts"
    if head in {"deliveries", "mirrors"}:
        return "receipts"
    if head == "runs":
        return "runs"
    if head in {"release", "registry"}:
        return "release"
    if head in {"toolchains", "blobs"}:
        return head
    return "other"


def _files(directory: Path) -> Iterator[tuple[PurePosixPath, os.stat_result]]:
    """Every regular file below ``directory`` (symlinks are not followed),
    without nested environment state directories."""
    if not directory.is_dir():
        return
    for root, folders, files in os.walk(directory):
        base = Path(root)
        relative_root = PurePosixPath(base.relative_to(directory).as_posix())
        if relative_root == PurePosixPath("."):
            folders[:] = [name for name in folders if name != "environments"]
        for name in files:
            path = base / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if path.is_symlink():
                continue
            yield PurePosixPath(path.relative_to(directory).as_posix()), info


def _size(path: Path) -> int:
    """Bytes under ``path`` (a file or a directory; symlinks not followed)."""
    try:
        if path.is_symlink() or path.is_file():
            return path.lstat().st_size
    except OSError:
        return 0
    total = 0
    for root, _folders, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                continue
    return total


def _usage(directory: Path) -> dict[str, dict[str, int]]:
    usage = {name: {"bytes": 0, "files": 0} for name in CATEGORIES}
    for relative, info in _files(directory):
        entry = usage[category(relative)]
        entry["bytes"] += info.st_size
        entry["files"] += 1
    return usage


@dataclass
class _Run:
    run_id: str
    path: Path
    data: dict[str, Any]

    @property
    def directory(self) -> Path:
        return self.path.with_suffix("")

    @property
    def finished(self) -> bool:
        from piceli.pipeline.journal import FINISHED

        return self.data.get("state") in FINISHED

    def output(self, stage: str) -> dict[str, Any]:
        value = (self.data.get("stages") or {}).get(stage) or {}
        output = value.get("output") if isinstance(value, dict) else None
        return output if isinstance(output, dict) else {}

    def stage_state(self, stage: str) -> str | None:
        value = (self.data.get("stages") or {}).get(stage) or {}
        return value.get("state") if isinstance(value, dict) else None

    def configs(self) -> set[str]:
        """Config digests (hex) of the images this run built or delivered."""
        found: set[str] = set()
        for build in self.output("build").values():
            if isinstance(build, dict):
                for image in (build.get("images") or {}).values():
                    if isinstance(image, dict) and isinstance(
                        image.get("image_id"), str
                    ):
                        found.add(image["image_id"].removeprefix("sha256:"))
        for image in (self.output("deliver").get("images") or {}).values():
            if isinstance(image, dict) and isinstance(image.get("config_digest"), str):
                found.add(image["config_digest"].removeprefix("sha256:"))
        planned = (self.data.get("plan") or {}).get("stages", {}).get("deliver", {})
        for image in (planned.get("images") or {}).values():
            if isinstance(image, dict) and isinstance(image.get("config_digest"), str):
                found.add(image["config_digest"].removeprefix("sha256:"))
        return found


def _runs(directory: Path) -> list[_Run]:
    """The journaled runs of a state directory, oldest first."""
    from piceli.pipeline.journal import RUN_SCHEMA

    runs_dir = directory / "runs"
    if not runs_dir.is_dir():
        return []
    found = []
    for path in sorted(runs_dir.glob("*.json")):
        if not _RUN_ID.fullmatch(path.stem) or path.is_symlink():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("schema") == RUN_SCHEMA:
            found.append(_Run(path.stem, path, data))
    return found


def _protected(runs: list[_Run], keep_last: int) -> dict[str, str]:
    """Runs never pruned (``why`` by run id): the latest, resumable ones and
    the latest run of each of the last ``keep_last`` applied releases."""
    why: dict[str, str] = {}
    if runs:
        why[runs[-1].run_id] = "latest"
    for run in runs:
        if not run.finished:
            why.setdefault(run.run_id, "resumable")
    releases: list[str] = []
    for run in reversed(runs):
        release = run.output("apply").get("release")
        if run.stage_state("apply") != "done" or not isinstance(release, str):
            continue
        if release in releases:
            continue
        if len(releases) >= keep_last:
            break
        releases.append(release)
        why.setdefault(run.run_id, "rollback")
    return why


def _build_configs(directory: Path) -> set[str]:
    """Config digests (hex) of the images in the current build receipts."""
    found: set[str] = set()
    builds = directory / "builds"
    if not builds.is_dir():
        return found
    for receipt in builds.glob("*/receipt.json"):
        try:
            images = json.loads(receipt.read_text())["outputs"]["images"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not isinstance(images, dict):
            continue
        for image in images.values():
            if isinstance(image, dict) and isinstance(image.get("image_id"), str):
                found.add(image["image_id"].removeprefix("sha256:"))
    return found


def temporary_entries(
    temp_dir: Path | None = None, *, now: float | None = None
) -> list[dict[str, Any]]:
    """Piceli's temporary entries in ``temp_dir`` (default: the system one)."""
    from piceli.tempfiles import STALE_SECONDS, is_temporary, tracked

    root = Path(temp_dir or tempfile.gettempdir())
    now = time.time() if now is None else now
    live = set(tracked())
    entries = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    for name in names:
        if not is_temporary(name):
            continue
        path = root / name
        try:
            info = path.lstat()
        except OSError:
            continue
        age = max(0.0, now - info.st_mtime)
        entries.append(
            {
                "name": name,
                "path": path,
                "bytes": _size(path),
                "age_seconds": round(age),
                "stale": age > STALE_SECONDS and str(path) not in live,
            }
        )
    return entries


# ---------------------------------------------------------------- status


def status(
    dirs: Iterable[StateDir],
    *,
    keep_last: int = DEFAULT_KEEP_LAST,
    temp_dir: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Sizes per category of each state directory, and Piceli's temporary
    directories. Read-only. ``reclaimable_bytes`` is what :func:`prune` with
    ``keep_last`` and no budget would free."""
    reports = []
    total = 0
    for item in dirs:
        usage = _usage(item.path)
        size = sum(entry["bytes"] for entry in usage.values())
        runs = _runs(item.path)
        plan = _candidates(item, keep_last=keep_last, budget=None, now=now)
        report = {
            **item.describe(),
            "exists": item.path.is_dir(),
            "bytes": size,
            "categories": usage,
            "runs": {
                "count": len(runs),
                "resumable": sum(1 for run in runs if not run.finished),
            },
            "reclaimable_bytes": sum(candidate.bytes for candidate in plan),
        }
        if item.budget is not None:
            report["budget"] = {"bytes": item.budget, "over": size > item.budget}
        reports.append(report)
        total += size
    temp = temporary_entries(temp_dir, now=now)
    temp_bytes = sum(entry["bytes"] for entry in temp)
    return {
        "schema": STATUS_SCHEMA,
        "state": "shown",
        "state_dirs": reports,
        "temp": {
            "path": str(Path(temp_dir or tempfile.gettempdir())),
            "entries": len(temp),
            "bytes": temp_bytes,
            "stale": sum(1 for entry in temp if entry["stale"]),
            "stale_bytes": sum(entry["bytes"] for entry in temp if entry["stale"]),
        },
        "total_bytes": total + temp_bytes,
    }


# ----------------------------------------------------------------- prune


@dataclass
class _Candidate:
    path: Path
    category: str
    why: str
    bytes: int = 0
    extra: list[Path] = field(default_factory=list)

    def to_dict(self, base: Path | None) -> dict[str, Any]:
        shown = str(self.path)
        if base is not None:
            try:
                shown = self.path.relative_to(base).as_posix()
            except ValueError:
                pass
        return {
            "path": shown,
            "category": self.category,
            "why": self.why,
            "bytes": self.bytes,
        }


def _stale_partials(directory: Path, now: float) -> list[_Candidate]:
    found = []
    for relative, info in _files(directory):
        if category(relative) != "temp":
            continue
        if now - info.st_mtime <= STALE_PARTIAL_SECONDS:
            continue
        found.append(
            _Candidate(
                directory / Path(*relative.parts), "temp", "stale-partial", info.st_size
            )
        )
    return found


def _candidates(
    item: StateDir, *, keep_last: int, budget: int | None, now: float | None
) -> list[_Candidate]:
    """What a prune of ``item`` removes, in order (see the module doc)."""
    directory = item.path
    now = time.time() if now is None else now
    if not directory.is_dir():
        return []
    chosen = _stale_partials(directory, now)
    runs = _runs(directory)
    protected = _protected(runs, keep_last)
    kept_ids = {run.run_id for run in runs[-keep_last:]} | set(protected)
    if not item.shared:
        for run in runs:
            if run.run_id in kept_ids:
                continue
            chosen.append(_run_candidate(run, "beyond-keep-last"))
        used = _build_configs(directory)
        for run in runs:
            if run.run_id in kept_ids:
                used |= run.configs()
        deliveries = directory / "deliveries"
        if deliveries.is_dir():
            for path in sorted(deliveries.glob("*.json")):
                hex_digest = path.stem.rpartition("-")[2]
                if path.is_symlink() or hex_digest in used:
                    continue
                chosen.append(
                    _Candidate(path, "receipts", "unused-delivery", _size(path))
                )
    if budget is None:
        return chosen
    size = sum(info.st_size for _relative, info in _files(directory))
    remaining = size - sum(candidate.bytes for candidate in chosen)
    if remaining <= budget:
        return chosen
    builds = directory / "builds"
    extra: list[_Candidate] = []
    if builds.is_dir():
        for build in sorted(builds.iterdir()):
            if not build.is_dir() or build.is_symlink():
                continue
            outputs = build / "outputs"
            if outputs.is_dir() and not outputs.is_symlink():
                extra.append(
                    _Candidate(outputs, "builds", "over-budget", _size(outputs))
                )
            for log in sorted(build.glob("*.log")):
                if log.is_file() and not log.is_symlink():
                    extra.append(_Candidate(log, "builds", "over-budget", _size(log)))
    extra.sort(key=lambda candidate: -candidate.bytes)
    if not item.shared:
        extra += [
            _run_candidate(run, "over-budget")
            for run in runs
            if run.run_id in kept_ids and run.run_id not in protected
        ]
    for candidate in extra:
        if remaining <= budget:
            break
        chosen.append(candidate)
        remaining -= candidate.bytes
    return chosen


def _run_candidate(run: _Run, why: str) -> _Candidate:
    extra = [run.directory] if run.directory.is_dir() else []
    size = _size(run.path) + sum(_size(path) for path in extra)
    return _Candidate(run.path, "runs", why, size, extra)


def _delete(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


@dataclass(frozen=True)
class PruneOptions:
    """``keep_last`` runs, an optional ``budget`` in bytes, ``dry_run``."""

    keep_last: int = DEFAULT_KEEP_LAST
    budget: int | None = None
    dry_run: bool = False

    def __post_init__(self) -> None:
        if type(self.keep_last) is not int or self.keep_last < 1:
            raise ValueError("keep_last must be an integer of at least 1")


def prune_one(
    item: StateDir,
    options: PruneOptions,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Prune one state directory (the caller holds its lock)."""
    budget = options.budget if options.budget is not None else item.budget
    before = sum(info.st_size for _relative, info in _files(item.path))
    chosen = _candidates(item, keep_last=options.keep_last, budget=budget, now=now)
    removed = []
    for candidate in chosen:
        if not options.dry_run:
            try:
                for path in (*candidate.extra, candidate.path):
                    _delete(path)
            except OSError:
                continue
        removed.append(candidate.to_dict(item.path))
    freed = sum(entry["bytes"] for entry in removed)
    after = before - freed
    report: dict[str, Any] = {
        **item.describe(),
        "bytes_before": before,
        "bytes_after": after,
        "freed_bytes": freed,
        "removed": removed,
    }
    if budget is not None:
        report["budget"] = {"bytes": budget, "over": after > budget}
    if item.shared:
        report["note"] = (
            "shared state: only machine-local files (build outputs, logs, "
            "partial files) are pruned"
        )
    return report


def prune_temporary(
    *, temp_dir: Path | None = None, dry_run: bool = False, now: float | None = None
) -> dict[str, Any]:
    """Remove stale ``piceli-*`` temporary directories (a killed process's)."""
    removed = []
    for entry in temporary_entries(temp_dir, now=now):
        if not entry["stale"]:
            continue
        if not dry_run:
            try:
                _delete(entry["path"])
            except OSError:
                continue
        removed.append(
            {
                "name": entry["name"],
                "bytes": entry["bytes"],
                "age_seconds": entry["age_seconds"],
            }
        )
    return {
        "path": str(Path(temp_dir or tempfile.gettempdir())),
        "removed": removed,
        "freed_bytes": sum(entry["bytes"] for entry in removed),
    }


def prune(
    dirs: Iterable[StateDir],
    options: PruneOptions | None = None,
    *,
    lock: bool = True,
    temp_dir: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Prune each state directory (under its run lock) and stale temporary
    directories; see the module doc for what is never removed.

    :raises piceli.state.errors.StateError: ``pipeline-locked`` when a run
        holds a state directory (nothing of it is removed).
    """
    from piceli.state.backend import directory_lock

    options = options or PruneOptions()
    reports = []
    for item in dirs:
        if not item.path.is_dir():
            continue
        if lock and not options.dry_run:
            with directory_lock(item.path):
                reports.append(prune_one(item, options, now=now))
        else:
            reports.append(prune_one(item, options, now=now))
    temp = prune_temporary(temp_dir=temp_dir, dry_run=options.dry_run, now=now)
    over = any(report.get("budget", {}).get("over") for report in reports)
    return {
        "schema": PRUNE_SCHEMA,
        "state": "planned" if options.dry_run else "pruned",
        "dry_run": options.dry_run,
        "keep_last": options.keep_last,
        "state_dirs": reports,
        "temp": temp,
        "freed_bytes": sum(report["freed_bytes"] for report in reports)
        + temp["freed_bytes"],
        "within_budget": not over,
    }
