"""CI's integration shards (scripts/kind_shard.py) cover every file once."""

from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_every_integration_file_runs_in_exactly_one_shard() -> None:
    shard = runpy.run_path(str(ROOT / "scripts" / "kind_shard.py"))["shard"]
    names = sorted(
        path.name for path in (ROOT / "tests" / "integration").glob("test_*.py")
    )
    chosen = [name for index in range(1, 5) for name in shard(index, 4, names)]
    assert sorted(chosen) == names
