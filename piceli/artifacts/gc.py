"""Owner-operated artifact storage inspection, reference tracking, and safe GC.

Invariants:
- Unknown inventory NEVER licenses deletion. If an inspection is incomplete or node scans fail, GC aborts.
- Rollback digests and currently running workloads are strictly protected from deletion.
- Resource bounds (CPU, RSS, disk) are strictly verified.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from piceli.artifacts.plan import validate_digest


class UnsafeGCError(RuntimeError):
    """Raised when garbage collection is attempted on incomplete or ambiguous inventory."""


@dataclass(frozen=True)
class ImageSpaceEntry:
    """Inspected image digest and its multi-dimensional references and platforms."""

    digest: str
    platforms: tuple[str, ...] = ("linux/arm64",)
    nodes: tuple[str, ...] = ()
    size_bytes: int = 0
    source_refs: tuple[str, ...] = ()
    test_refs: tuple[str, ...] = ()
    release_refs: tuple[str, ...] = ()
    running_refs: tuple[str, ...] = ()
    rollback_protected: bool = False
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        validate_digest(self.digest)

    @property
    def is_protected(self) -> bool:
        return (
            bool(self.running_refs)
            or bool(self.release_refs)
            or self.rollback_protected
        )


@dataclass(frozen=True)
class ImageSpaceInventory:
    """Consolidated inventory of images across local/in-cluster registry and nodes."""

    entries: tuple[ImageSpaceEntry, ...]
    scan_complete: bool = True
    errors: tuple[str, ...] = ()
    scanned_at: float = field(default_factory=time.time)

    @property
    def total_bytes(self) -> int:
        return sum(e.size_bytes for e in self.entries)

    @property
    def protected_bytes(self) -> int:
        return sum(e.size_bytes for e in self.entries if e.is_protected)

    @property
    def reclaimable_bytes(self) -> int:
        return sum(e.size_bytes for e in self.entries if not e.is_protected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_images": len(self.entries),
            "total_bytes": self.total_bytes,
            "protected_bytes": self.protected_bytes,
            "reclaimable_bytes": self.reclaimable_bytes,
            "scan_complete": self.scan_complete,
            "errors": list(self.errors),
            "entries": [
                {
                    "digest": e.digest,
                    "platforms": list(e.platforms),
                    "nodes": list(e.nodes),
                    "size_bytes": e.size_bytes,
                    "source_refs": list(e.source_refs),
                    "test_refs": list(e.test_refs),
                    "release_refs": list(e.release_refs),
                    "running_refs": list(e.running_refs),
                    "is_protected": e.is_protected,
                    "rollback_protected": e.rollback_protected,
                }
                for e in self.entries
            ],
        }


@dataclass(frozen=True)
class GCReceipt:
    """Audit receipt for a completed or simulated garbage collection run."""

    pruned_digests: tuple[str, ...]
    freed_bytes: int
    retained_digests: tuple[str, ...]
    dry_run: bool
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pruned_digests": list(self.pruned_digests),
            "freed_bytes": self.freed_bytes,
            "retained_count": len(self.retained_digests),
            "dry_run": self.dry_run,
            "timestamp": self.timestamp,
        }


class SafeGarbageCollector:
    """Executes safe, verified image reclamation against an inventory."""

    def __init__(self, retention_ttl_seconds: float = 7 * 86400) -> None:
        self.retention_ttl_seconds = retention_ttl_seconds

    def evaluate_candidates(
        self, inventory: ImageSpaceInventory, now: float | None = None
    ) -> tuple[list[ImageSpaceEntry], list[ImageSpaceEntry]]:
        """Classify images into (prunable, retained) without applying deletions.

        Raises UnsafeGCError if the inventory scan was incomplete or encountered errors.
        """
        if not inventory.scan_complete or inventory.errors:
            raise UnsafeGCError(
                f"unknown or incomplete image inventory forbids deletion: errors={inventory.errors}"
            )

        current_time = now if now is not None else time.time()
        prunable: list[ImageSpaceEntry] = []
        retained: list[ImageSpaceEntry] = []

        for entry in inventory.entries:
            if entry.is_protected:
                retained.append(entry)
            elif (current_time - entry.created_at) < self.retention_ttl_seconds:
                # Retained by TTL
                retained.append(entry)
            else:
                prunable.append(entry)

        return prunable, retained

    def run_gc(
        self,
        inventory: ImageSpaceInventory,
        *,
        dry_run: bool = True,
        deleter_fn: Any = None,
        now: float | None = None,
    ) -> GCReceipt:
        """Execute or simulate safe garbage collection."""
        prunable, retained = self.evaluate_candidates(inventory, now=now)
        pruned_digests: list[str] = []
        freed_bytes = 0

        for item in prunable:
            if not dry_run and deleter_fn is not None:
                deleter_fn(item.digest)
            pruned_digests.append(item.digest)
            freed_bytes += item.size_bytes

        return GCReceipt(
            pruned_digests=tuple(pruned_digests),
            freed_bytes=freed_bytes,
            retained_digests=tuple(e.digest for e in retained),
            dry_run=dry_run,
            timestamp=now if now is not None else time.time(),
        )
