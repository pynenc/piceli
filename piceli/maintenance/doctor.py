"""``piceli doctor``: is this runner ready for the next build and deploy?

Checks, never changing anything and never contacting a cluster:

* free disk where the state directory and the temporary directory live,
  against what the next build needs;
* available memory (read from ``/proc/meminfo`` on Linux; ``unknown``
  elsewhere) against what the next build needs;
* the external tools the pipeline uses: ``docker`` and ``docker buildx`` for
  a build, ``kubectl`` for a node-loopback registry (its port forward).
  Without a pipeline every tool is checked.

The need is estimated from the last build receipts: twice the images and
build outputs they recorded (the engine keeps the old image while it builds
the new one), plus headroom, and never below :data:`MIN_BUILD_DISK` and
:data:`MIN_BUILD_MEMORY`. A pipeline without a build needs
:data:`MIN_DEPLOY_DISK` only.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from piceli.maintenance.cache import StateDir
    from piceli.pipeline.model import Pipeline

SCHEMA = "piceli.doctor.v1"
GIB = 1024**3
MIN_BUILD_DISK = 4 * GIB
MIN_BUILD_MEMORY = 2 * GIB
MIN_DEPLOY_DISK = 256 * 1024**2
HEADROOM = 1 * GIB

#: ``(name, argv)`` of each tool check; the argv runs with a timeout.
TOOLS: Mapping[str, tuple[str, ...]] = {
    "docker": ("docker", "--version"),
    "docker buildx": ("docker", "buildx", "version"),
    "kubectl": ("kubectl", "version", "--client=true", "--output=json"),
}

Runner = Callable[[list[str]], bool]


def _run(argv: list[str]) -> bool:
    """Whether ``argv`` exits 0 within 15 s (output discarded)."""
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def available_memory() -> int | None:
    """Available memory in bytes (Linux ``MemAvailable``), else ``None``."""
    try:
        text = Path("/proc/meminfo").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            try:
                return int(parts[1]) * 1024
            except (IndexError, ValueError):
                return None
    return None


def _existing(path: Path) -> Path:
    """``path`` or its nearest existing parent (``disk_usage`` needs one)."""
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path("/")


def _last_build_bytes(dirs: list[StateDir]) -> tuple[int, int]:
    """``(bytes, receipts)``: image and output bytes of the last build receipts."""
    from piceli.maintenance.cache import _size

    total = receipts = 0
    for item in dirs:
        for receipt in sorted((item.path / "builds").glob("*/receipt.json")):
            try:
                images = json.loads(receipt.read_text())["outputs"]["images"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            receipts += 1
            if isinstance(images, dict):
                for image in images.values():
                    size = image.get("size_bytes") if isinstance(image, dict) else None
                    if type(size) is int and size > 0:
                        total += size
            total += _size(receipt.parent / "outputs")
    return total, receipts


def needs(dirs: list[StateDir], *, builds: bool = True) -> dict[str, Any]:
    """What the next run needs on this runner (see the module doc)."""
    if not builds:
        return {
            "disk_bytes": MIN_DEPLOY_DISK,
            "memory_bytes": None,
            "basis": "no build",
        }
    recorded, receipts = _last_build_bytes(dirs)
    return {
        "disk_bytes": max(MIN_BUILD_DISK, 2 * recorded + HEADROOM),
        "memory_bytes": MIN_BUILD_MEMORY,
        "basis": f"{receipts} build receipt(s)" if receipts else "default",
    }


def _tools_for(pipeline: Pipeline | None) -> list[str]:
    if pipeline is None:
        return list(TOOLS)
    from piceli.pipeline.model import NodeLoopbackRegistry

    wanted = []
    if pipeline.builds:
        wanted += ["docker", "docker buildx"]
    if isinstance(pipeline.deliver, NodeLoopbackRegistry):
        wanted.append("kubectl")
    return wanted


def diagnose(
    dirs: list[StateDir],
    *,
    pipeline: Pipeline | None = None,
    temp_dir: Path | None = None,
    run: Runner | None = None,
    which: Callable[[str], str | None] | None = None,
    memory: Callable[[], int | None] | None = None,
    disk: Callable[[Path], int] | None = None,
) -> dict[str, Any]:
    """The doctor report: one entry per check, ``ok``, ``warn`` or ``unknown``.

    Each warning names a registered code (``runner-disk-low``,
    ``runner-memory-low``, ``runner-tool-missing``).
    """
    from piceli.maintenance.cache import format_size

    run = run or _run
    which = which or shutil.which
    memory = memory or available_memory
    free_bytes = disk or (lambda path: shutil.disk_usage(path).free)
    builds = pipeline is None or bool(pipeline.builds)
    need = needs(dirs, builds=builds)
    checks: list[dict[str, Any]] = []
    places = {"temp": Path(temp_dir or tempfile.gettempdir())}
    for index, item in enumerate(dirs[:1] or []):
        places[f"state_dir{index or ''}"] = item.path
    seen: set[Path] = set()
    for label, place in places.items():
        where = _existing(place)
        try:
            free = free_bytes(where)
        except OSError:
            checks.append({"check": f"disk:{label}", "status": "unknown"})
            continue
        if where in seen:
            continue
        seen.add(where)
        low = free < need["disk_bytes"]
        checks.append(
            {
                "check": f"disk:{label}",
                "status": "warn" if low else "ok",
                "free_bytes": free,
                "needed_bytes": need["disk_bytes"],
                **({"reason": "runner-disk-low"} if low else {}),
                "detail": f"{format_size(free)} free, "
                f"{format_size(need['disk_bytes'])} needed",
            }
        )
    if need["memory_bytes"] is not None:
        available = memory()
        if available is None:
            checks.append(
                {
                    "check": "memory",
                    "status": "unknown",
                    "needed_bytes": need["memory_bytes"],
                    "detail": "available memory is not readable on this system",
                }
            )
        else:
            low = available < need["memory_bytes"]
            checks.append(
                {
                    "check": "memory",
                    "status": "warn" if low else "ok",
                    "available_bytes": available,
                    "needed_bytes": need["memory_bytes"],
                    **({"reason": "runner-memory-low"} if low else {}),
                    "detail": f"{format_size(available)} available, "
                    f"{format_size(need['memory_bytes'])} needed",
                }
            )
    for name in _tools_for(pipeline):
        argv = list(TOOLS[name])
        found = which(argv[0]) is not None and run(argv)
        checks.append(
            {
                "check": f"tool:{name}",
                "status": "ok" if found else "warn",
                **({"reason": "runner-tool-missing"} if not found else {}),
                "detail": "found" if found else f"`{' '.join(argv)}` did not run",
            }
        )
    warnings = [item for item in checks if item["status"] == "warn"]
    return {
        "schema": SCHEMA,
        "state": "warnings" if warnings else "ok",
        "needs": need,
        "checks": checks,
        **({"reason": warnings[0]["reason"]} if warnings else {}),
    }
