from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _ruff() -> str | None:
    candidate = Path(sys.executable).with_name("ruff")
    return str(candidate) if candidate.exists() else shutil.which("ruff")


@pytest.fixture
def ruff_clean(tmp_path: Path):
    """Assert that source passes ``ruff check`` and ``ruff format --check``."""
    ruff = _ruff()
    if ruff is None:
        pytest.skip("ruff is not installed")

    def check(source: str, name: str = "generated.py") -> None:
        path = tmp_path / name
        path.write_text(source)
        config = str(ROOT / "pyproject.toml")
        for args in (["check", "--no-cache"], ["format", "--check", "--no-cache"]):
            result = subprocess.run(
                [ruff, *args, "--config", config, str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            # The generated code must also be clean under ruff's defaults.
            result = subprocess.run(
                [ruff, *args, "--isolated", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr

    return check
