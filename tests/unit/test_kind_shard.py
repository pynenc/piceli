"""CI's integration shards (scripts/kind_shard.py) cover every file once."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("count", [4, 5, 6])
def test_every_integration_file_runs_in_exactly_one_shard(count: int) -> None:
    shard = runpy.run_path(str(ROOT / "scripts" / "kind_shard.py"))["shard"]
    names = sorted(
        path.name for path in (ROOT / "tests" / "integration").glob("test_*.py")
    )
    chosen = [
        name for index in range(1, count + 1) for name in shard(index, count, names)
    ]
    assert sorted(chosen) == names
