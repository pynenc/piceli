"""Fixtures of the ``piceli infra`` tests."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest


def _tofu() -> str | None:
    found = os.environ.get("PICELI_TOFU") or shutil.which("tofu")
    return found if found and Path(found).is_file() else None


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Credentials, profiles and state homes under ``tmp_path``."""
    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    monkeypatch.setenv("PICELI_PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    for name in ("HCLOUD_TOKEN", "PICELI_PROVIDER_TOKEN", "PICELI_STATE_KEY"):
        monkeypatch.delenv(name, raising=False)
    tofu = _tofu()
    if tofu:
        monkeypatch.setenv("PICELI_TOFU", tofu)
    return tmp_path
