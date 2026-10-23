"""Bounding what a train keeps: worktrees, target directories, the content
store and runs (``piceli.train.prune``, ``piceli train-prune``)."""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app
from piceli.train import Retention
from piceli.train.local import USED_MARKER
from piceli.train.prune import prune
from piceli.train.reuse import Store
from tests.train_toy import Toy, make_toy

DAY = 86400.0
NOW = time.time()


def aged(path: Path, days: float) -> None:
    moment = NOW - days * DAY
    os.utime(path, (moment, moment))


def target(cache: Path, name: str, days: float, size: int = 1000) -> Path:
    path = cache / "targets" / "shop" / "repo-1" / name
    path.mkdir(parents=True)
    (path / "blob").write_bytes(b"x" * size)
    (path / USED_MARKER).touch()
    aged(path / USED_MARKER, days)
    return path


@contextmanager
def holding(lock: Path) -> Iterator[None]:
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_prune(tmp_path: Path, **options: Any) -> dict[str, Any]:
    return prune(
        "shop", cache=tmp_path / "cache", state_root=tmp_path / "state" / "shop",
        now=NOW, **options,
    )  # fmt: skip


def removed(result: dict[str, Any], kind: str) -> list[str]:
    return sorted(Path(i["path"]).name for i in result["removed"] if i["kind"] == kind)


def test_targets_unused_too_long_go_unless_a_trial_holds_them(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    target(cache, "0", days=20)
    target(cache, "1", days=1)
    target(cache, "2", days=20)
    times = cache / "checkouts" / "shop" / "repo-1" / "0.times.json"
    times.parent.mkdir(parents=True)
    times.write_text("{}")
    with holding(cache / "checkouts" / "shop" / "repo-1" / "lock-2.lock"):
        result = run_prune(tmp_path)
    assert removed(result, "target") == ["0"]
    assert removed(result, "times") == ["0.times.json"] and not times.exists()
    assert (cache / "targets/shop/repo-1/1").is_dir()
    assert (cache / "targets/shop/repo-1/2").is_dir()  # in use
    assert result["sizes"]["targets"] > 0


def test_targets_over_their_size_go_least_recently_used_first(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    for name, days in (("0", 3), ("1", 1), ("2", 2)):
        target(cache, name, days=days, size=600_000)
    result = run_prune(tmp_path, retention=Retention(targets_size="1MiB"))
    # About 1.8 MB kept in three: the two least recently used go.
    assert removed(result, "target") == ["0", "2"]
    assert result["sizes"]["targets"] <= 1 << 20


def test_the_store_drops_old_and_least_recently_used_records_and_their_bytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache" / "store"
    store = Store(root)
    tree = tmp_path / "tree"
    tree.mkdir()
    keys = {}
    for name, days in (("old", 40), ("mid", 3), ("new", 1)):
        (tree / f"{name}.bin").write_bytes(name.encode() * 300_000)
        (tree / "shared.txt").write_text("shared")
        key = "sha256:" + name.encode().hex().ljust(64, "0")
        store.save(key, tree, [f"{name}.bin", "shared.txt"], {"run": name})
        record = root / "keys" / f"{key.split(':')[1]}.json"
        aged(record, days)
        keys[name] = key
    for blob in (root / "blobs").glob("*/*"):
        aged(blob, 1)  # older than the orphan grace
    young = root / "blobs" / "ff" / ("f" * 64)
    young.parent.mkdir(exist_ok=True)
    young.write_bytes(b"being stored")  # its record is not written yet
    result = run_prune(tmp_path, retention=Retention(store_size="1MiB"))
    assert store.lookup(keys["old"]) is None  # unused 30+ days
    assert store.lookup(keys["mid"]) is None  # over 1 MiB, least recently used
    assert store.lookup(keys["new"]) is not None  # and its shared bytes stay
    assert young.exists()
    assert len(removed(result, "store-record")) == 2
    assert len(removed(result, "store-blob")) == 2  # old.bin, mid.bin


def test_runs_keep_the_newest_and_drop_the_old_ones(tmp_path: Path) -> None:
    runs = tmp_path / "state" / "shop" / "runs"
    for index in range(25):
        path = runs / f"20260101T0000{index:02d}Z-abcdef"
        path.mkdir(parents=True)
        (path / "run.json").write_text("{}")
        aged(path / "run.json", 60)
    current = "20260101T000000Z-abcdef"
    result = run_prune(tmp_path, current=current)
    assert len(removed(result, "run")) == 4  # 25 - 20 newest - the current one
    assert (runs / current).is_dir()
    assert len(list(runs.iterdir())) == 21


def test_leftover_worktrees_go_and_a_dry_run_removes_nothing(tmp_path: Path) -> None:
    pool = tmp_path / "cache" / "checkouts" / "shop" / "repo-1"
    (pool / "0").mkdir(parents=True)
    (pool / "1").mkdir()
    target(tmp_path / "cache", "0", days=30)
    dry = run_prune(tmp_path, dry_run=True)
    assert removed(dry, "checkout") == ["0", "1"] and (pool / "0").is_dir()
    assert removed(dry, "target") == ["0"]
    with holding(pool / "lock-1.lock"):
        result = run_prune(tmp_path)
    assert removed(result, "checkout") == ["0"]
    assert not (pool / "0").exists() and (pool / "1").is_dir()


@pytest.fixture
def toy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Toy:
    monkeypatch.setenv("TOY_PUBLISH_OUT", str(tmp_path / "published"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return make_toy(tmp_path)


def test_every_run_prunes_and_reports_the_sizes(toy: Toy) -> None:
    runs = toy.root / "state" / "toy" / "runs"
    for index in range(22):
        old = runs / f"20250101T0000{index:02d}Z-abcdef"
        old.mkdir(parents=True)
        (old / "run.json").write_text("{}")
        aged(old / "run.json", 90)
    toy.branch("feat-a", {"src/a.txt": "a\n"})
    result = CliRunner().invoke(
        app,
        ["train", toy.spec, "feat-a", "--json", "--state-dir", str(toy.root / "state")],
    )
    body = json.loads(result.stdout.strip().splitlines()[-1])
    assert body["ok"], body
    assert body["prune"]["removed"] == 3  # 23 runs, the newest 20 kept
    assert set(body["prune"]["sizes"]) == {"checkouts", "targets", "store", "runs"}
    assert "cache: store" in result.stderr
    # No worktree is left behind after the run.
    pool = toy.root / "cache" / "piceli" / "train" / "checkouts" / "toy"
    assert not [p for p in pool.glob("*/*") if p.is_dir()]


def test_train_prune_names_a_train_and_reports_what_would_go(toy: Toy) -> None:
    target(toy.root / "cache" / "piceli" / "train", "0", days=30).rename(
        toy.root / "cache" / "piceli" / "train" / "targets" / "shop" / "repo-1" / "9"
    )
    result = CliRunner().invoke(
        app,
        ["train-prune", "shop", "--dry-run", "--json",
         "--state-dir", str(toy.root / "state")],
    )  # fmt: skip
    assert result.exit_code == 0, result.stderr
    body = json.loads(result.stdout.strip().splitlines()[-1])
    assert body["schema"] == "piceli.train-prune.v1" and body["dry_run"]
    assert [Path(i["path"]).name for i in body["removed"]] == ["9"]
    assert "would remove 1" in result.stderr and "targets" in result.stderr
    refused = CliRunner().invoke(app, ["train-prune", "Not A Name"])
    assert refused.exit_code == 2
