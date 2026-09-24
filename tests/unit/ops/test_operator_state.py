"""Unit tests for Piceli Operator state store, concurrency locking, and backup."""

from pathlib import Path
import pytest

from piceli.k8s.operator_state import (
    ConcurrentWriterError,
    FileStateStore,
    InstanceLock,
    OperatorUser,
    PolicyStore,
    StandingPolicy,
    UserStore,
)


def test_instance_lock_prevents_concurrent_writers(tmp_path: Path) -> None:
    lock_file = tmp_path / ".instance.lock"
    lock1 = InstanceLock(lock_file)
    lock2 = InstanceLock(lock_file)

    lock1.acquire()
    assert lock_file.exists()

    with pytest.raises(ConcurrentWriterError, match="exclusive operator instance lock already held"):
        lock2.acquire()

    lock1.release()
    # Now lock2 can acquire
    lock2.acquire()
    lock2.release()


def test_file_state_store_atomic_save_and_backup(tmp_path: Path) -> None:
    store_dir = tmp_path / "state"
    store = FileStateStore(store_dir)

    # Save data
    store.save_data("releases", {"active": "v1", "count": 1})
    store.save_data("preferences", {"theme": "dark"})

    loaded = store.load_data("releases")
    assert loaded == {"active": "v1", "count": 1}

    # Test backup
    backup_file = tmp_path / "backup.tar.gz"
    store.create_backup(backup_file)
    assert backup_file.exists()

    # Restore to clean destination
    restore_dir = tmp_path / "restored"
    store.restore_backup(backup_file, destination=restore_dir)
    restored_store = FileStateStore(restore_dir)
    assert restored_store.load_data("releases") == {"active": "v1", "count": 1}
    assert restored_store.load_data("preferences") == {"theme": "dark"}


def test_user_store_and_authentication(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path / "state")
    user_store = UserStore(store)

    user, token = user_store.create_user("alice", "admin", "super-secret-token-123", policies=("default",))
    assert user.username == "alice"
    assert user.role == "admin"

    # Authenticate with valid token
    authed = user_store.authenticate(token)
    assert authed is not None
    assert authed.username == "alice"

    # Reject invalid token
    assert user_store.authenticate("wrong-token") is None
    assert user_store.authenticate("") is None


def test_standing_policy_scope_enforcement() -> None:
    policy = StandingPolicy(
        name="staging-policy",
        allowed_namespaces=("my-app-*", "test-*"),
        allowed_branches=("main", "release/*"),
        allowed_registries=("registry.local:5000", "local"),
        allowed_operations=("preview", "promote", "rollback"),
        require_pr_approval=True,
    )

    # Allowed
    assert policy.authorize("preview", namespace="my-app-p2", branch="main")
    assert policy.authorize("promote", namespace="test-app", branch="release/v1.0", registry="registry.local:5000")

    # Disallowed namespace
    assert not policy.authorize("preview", namespace="production", branch="main")

    # Disallowed branch
    assert not policy.authorize("preview", namespace="test-app", branch="feature/unreviewed")

    # Disallowed action
    assert not policy.authorize("deploy", namespace="test-app", branch="main")

    # PR action requiring approval
    assert not policy.authorize("promote", namespace="test-app", branch="main", is_pr=True, approved=False)
    assert policy.authorize("promote", namespace="test-app", branch="main", is_pr=True, approved=True)
