"""Example: Atomic file state store, exclusive concurrency locking, and encrypted backup."""

from pathlib import Path
from piceli.k8s.operator_state import (
    ConcurrentWriterError,
    FileStateStore,
    InstanceLock,
    StandingPolicy,
    UserStore,
)


def run_state_and_backup(state_dir: Path) -> None:
    store = FileStateStore(state_dir)

    # 1. Single-instance lock
    with store.lock:
        print("[OK] Acquired exclusive single-instance lock")
        # Second instance fails
        lock2 = InstanceLock(state_dir / ".instance.lock")
        try:
            lock2.acquire()
            raise AssertionError("should have failed")
        except ConcurrentWriterError:
            print("[OK] Confirmed concurrent writer rejection via flock")

    # 2. Persist state
    store.save_data("preferences", {"theme": "slate-dark", "auto_refresh": True})
    user_store = UserStore(store)
    user, token = user_store.create_user("operator-bob", "admin", "secure-token-xyz")
    print(f"[OK] Created operator user '{user.username}' with role '{user.role}'")

    # 3. Create backup archive
    backup_file = state_dir.parent / "operator-state-backup.tar.gz"
    store.create_backup(backup_file)
    print(f"[OK] Created backup archive at {backup_file} (size: {backup_file.stat().st_size} bytes)")

    # 4. Restore to clean destination
    restore_target = state_dir.parent / "restored-state"
    store.restore_backup(backup_file, destination=restore_target)
    restored_store = FileStateStore(restore_target)
    assert restored_store.load_data("preferences") == {"theme": "slate-dark", "auto_refresh": True}
    assert UserStore(restored_store).authenticate(token) is not None
    print("[OK] Successfully verified restored state in new clean destination")


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        run_state_and_backup(Path(tmp) / "state")
