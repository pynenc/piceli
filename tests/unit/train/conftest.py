"""Every train test gets its own cache and state roots.

A train keeps slot locks, trial worktrees, cargo targets and a content store
under ``$XDG_CACHE_HOME/piceli/train``. Without this, tests would share the
developer's cache: parallel workers would queue on one slot lock, a stored
step from one test would be ``reused`` by another, and worktrees would be
left in the home directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _own_train_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
