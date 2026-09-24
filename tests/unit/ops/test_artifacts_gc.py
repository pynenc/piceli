"""Unit tests for Piceli owner-operated artifact storage and safe GC."""

import time

import pytest

from piceli.artifacts.gc import (
    ImageSpaceEntry,
    ImageSpaceInventory,
    SafeGarbageCollector,
    UnsafeGCError,
)


def test_incomplete_inventory_blocks_garbage_collection() -> None:
    entry = ImageSpaceEntry(
        digest="sha256:" + "1" * 64,
        size_bytes=1000,
        created_at=time.time() - 100_000,
    )
    # Incomplete inventory
    inventory = ImageSpaceInventory(
        entries=(entry,),
        scan_complete=False,
        errors=("node-2: Connection timed out",),
    )
    gc = SafeGarbageCollector(retention_ttl_seconds=1000)

    with pytest.raises(UnsafeGCError, match="unknown or incomplete image inventory forbids deletion"):
        gc.run_gc(inventory)


def test_rollback_and_running_images_strictly_protected() -> None:
    now = time.time()
    old_time = now - 100_000

    running_entry = ImageSpaceEntry(
        digest="sha256:" + "1" * 64,
        size_bytes=500,
        running_refs=("pod/web-abc",),
        created_at=old_time,
    )
    rollback_entry = ImageSpaceEntry(
        digest="sha256:" + "2" * 64,
        size_bytes=600,
        rollback_protected=True,
        created_at=old_time,
    )
    release_entry = ImageSpaceEntry(
        digest="sha256:" + "3" * 64,
        size_bytes=700,
        release_refs=("v1-0-0",),
        created_at=old_time,
    )
    orphaned_old_entry = ImageSpaceEntry(
        digest="sha256:" + "4" * 64,
        size_bytes=800,
        created_at=old_time,
    )
    orphaned_fresh_entry = ImageSpaceEntry(
        digest="sha256:" + "5" * 64,
        size_bytes=900,
        created_at=now - 50,  # fresh within TTL
    )

    inventory = ImageSpaceInventory(
        entries=(
            running_entry,
            rollback_entry,
            release_entry,
            orphaned_old_entry,
            orphaned_fresh_entry,
        ),
        scan_complete=True,
    )

    assert inventory.protected_bytes == 500 + 600 + 700
    assert inventory.reclaimable_bytes == 800 + 900

    gc = SafeGarbageCollector(retention_ttl_seconds=1000)
    receipt = gc.run_gc(inventory, dry_run=True, now=now)

    assert receipt.dry_run is True
    assert receipt.freed_bytes == 800
    assert receipt.pruned_digests == ("sha256:" + "4" * 64,)
    assert len(receipt.retained_digests) == 4
