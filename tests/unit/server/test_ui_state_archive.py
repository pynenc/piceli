"""Offline UI backups preserve evidence and reject active or altered state."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from piceli.server.state_archive import backup, restore
from piceli.services.store import Store


def test_backup_restore_and_dispatcher_exclusion(tmp_path: Path) -> None:
    state = tmp_path / "control"
    store = Store(state / "operations.sqlite3")
    store.acquire_dispatcher()
    journal = state / "release" / "journal.json"
    journal.parent.mkdir()
    journal.write_text('{"run":"recorded"}')
    archive = tmp_path / "ui-backup.tar.gz"
    with pytest.raises(ValueError, match="dispatcher is running"):
        backup(state, archive)
    store.close()
    backup(state, archive)
    assert archive.stat().st_mode & 0o777 == 0o600
    with tarfile.open(archive, "r:gz") as contents:
        assert "operations.sqlite3" in contents.getnames()
        assert "release/journal.json" in contents.getnames()
        assert not any(name.endswith(".runner.lock") for name in contents.getnames())
    restored = restore(archive, tmp_path / "recovered")
    assert (restored / "release" / "journal.json").read_text() == journal.read_text()
    assert (restored / "operations.sqlite3").read_bytes() == (
        state / "operations.sqlite3"
    ).read_bytes()
    assert restored.stat().st_mode & 0o777 == 0o700
    assert (restored / "release" / "journal.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="empty"):
        restore(archive, restored)


def test_backup_rejects_symlinked_state(tmp_path: Path) -> None:
    state = tmp_path / "control"
    store = Store(state / "operations.sqlite3")
    store.acquire_dispatcher()
    store.close()
    (state / "link").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="unsupported entry"):
        backup(state, tmp_path / "archive.tar.gz")


def test_pipeline_only_control_store_can_be_backed_up(tmp_path: Path) -> None:
    state = tmp_path / "pipeline-control"
    store = Store(state / "pipeline-control.sqlite3")
    store.acquire_dispatcher()
    store.close()
    archive = backup(state, tmp_path / "pipeline-ui.tar.gz")
    recovered = restore(archive, tmp_path / "recovered")
    assert (recovered / "pipeline-control.sqlite3").is_file()
