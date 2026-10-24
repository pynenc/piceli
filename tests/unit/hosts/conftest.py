"""Every host test gets its own state root: ``piceli host wait`` keeps the
switches it saw under ``$XDG_STATE_HOME/piceli/cycle`` (0.28.0)."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _own_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
