"""0.53.0: the nearest lineage first, new lineages seeded from a sibling, and
a run over its cap paused instead of stopped while other runs hold the cache.

Everything runs on plain directories, a real Git repository in ``tmp_path``
and short real subprocesses; clocks are injected.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from piceli.dev import pod
from tests.unit.dev.test_pack import commit, repo

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")


def _lineage(cache: Path, name: str, **meta: Any) -> Path:
    path = cache / "lineages" / name
    (path / "target").mkdir(parents=True)
    pod._write_meta(path, meta)
    return path


def _holding(head: str) -> dict[str, Any]:
    return {"sources": {"shop": {"head": head, "overlay": []}}}


def _spec(**extra: Any) -> dict[str, Any]:
    return {"run": "r9", "lineage_key": "shop-rust", "max_lineages": 3, **extra}


# ------------------------------------------------------------ nearest first


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_nearest_lineage_wins_over_the_most_recent(tmp_path: Path) -> None:
    work = repo(tmp_path / "work", {"a.rs": "0"})
    heads = [commit(work, {"a.rs": str(step)}) for step in range(1, 6)]
    mirror = work / ".git"
    cache = tmp_path / "cache"
    _lineage(
        cache, "shop-rust/0", last_used=1.0, runs=1, materialized=_holding(heads[3])
    )
    _lineage(
        cache, "shop-rust/1", last_used=2.0, runs=1, materialized=_holding(heads[0])
    )
    _lineage(
        cache, "shop-rust/2", last_used=3.0, runs=1, materialized=_holding("f" * 40)
    )
    near = pod.DeltaSource("shop", mirror, heads[4], tmp_path / "in")
    taken = pod.take_lineage(cache, _spec(), time.monotonic, near)
    try:
        # One commit away beats four away and the most recent (not in the mirror).
        assert taken.path.name == "0" and taken.warm is True
        meta = json.loads((taken.path / "meta.json").read_text())
        assert meta["run"] == "r9" and meta["taken_at"] > 0
    finally:
        os.close(taken.descriptor)
    # The same branch still comes first.
    pod._write_meta(
        cache / "lineages/shop-rust/1",
        {"last_used": 2.0, "hint": "wp-b", "materialized": _holding(heads[0])},
    )
    taken = pod.take_lineage(cache, _spec(lineage_hint="wp-b"), time.monotonic, near)
    os.close(taken.descriptor)
    assert taken.path.name == "1"
    # No head to compare: the most recently used.
    assert pod.distance(mirror, "f" * 40, heads[4]) is None
    taken = pod.take_lineage(cache, _spec(), time.monotonic, None)
    os.close(taken.descriptor)
    assert taken.path.name == "2"


# ------------------------------------------------------------------ seeding


def _build(path: Path) -> None:
    """A small tree and cargo target, with two units."""
    files = {
        "tree/app/src/lib.rs": "pub fn a() {}",
        "target/debug/deps/libfoo-1a.rlib": "foo",
        "target/debug/deps/foo-1a.d": "foo.d",
        "target/debug/.fingerprint/foo-1a/lib-foo": "fp",
        "target/debug/build/foo-1a/out/gen.rs": "gen",
        "target/debug/deps/libbar-2b.rlib": "bar",
        "target/debug/.fingerprint/bar-2b/lib-bar": "fp",
        "target/debug/.cargo-lock": "",
    }
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)


def test_a_new_lineage_is_seeded_from_a_busy_sibling(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    sibling = _lineage(
        cache, "shop-rust/0", runs=3, bytes=4096, fetched="lock:home:app"
    )
    _build(sibling)
    busy = pod._try_lock(sibling)  # another run builds in it
    assert busy is not None
    try:
        taken = pod.take_lineage(cache, _spec(), time.monotonic)
        assert taken.path.name == "1" and taken.seed_from == sibling
        seeded = pod.seed_lineage(taken.path, sibling, 10**9)
        os.close(taken.descriptor)
    finally:
        os.close(busy)
    assert seeded is not None and seeded["from"] == "shop-rust/0"
    assert seeded["method"] in {"cp", "copytree"} and seeded["dropped_units"] == 0
    copy = taken.path
    assert (copy / "tree/app/src/lib.rs").read_text() == "pub fn a() {}"
    assert (copy / "target/debug/deps/libfoo-1a.rlib").read_text() == "foo"
    assert not (copy / "target/debug/.cargo-lock").exists()
    original = sibling / "tree/app/src/lib.rs"
    assert (
        copy / "tree/app/src/lib.rs"
    ).stat().st_mtime_ns == original.stat().st_mtime_ns
    meta = json.loads((copy / "meta.json").read_text())
    assert meta["seeded_from"] == "shop-rust/0" and meta["fetched"] == "lock:home:app"
    assert "materialized" not in meta and meta["bytes"] == seeded["bytes"] > 0


def test_a_file_changed_during_the_copy_drops_its_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    sibling = _lineage(cache, "shop-rust/0", runs=1, bytes=4096)
    _build(sibling)
    copy_dir = pod._copy_dir

    def copy_then_build(source: Path, dest: Path) -> str:
        method = copy_dir(source, dest)
        if source.name == "target":  # the sibling's run rebuilds foo meanwhile
            (source / "debug/deps/libfoo-1a.rlib").write_text("foo, rebuilt")
        else:
            (source / "app/src/lib.rs").unlink()
        return method

    monkeypatch.setattr(pod, "_copy_dir", copy_then_build)
    new = cache / "lineages/shop-rust/1"
    new.mkdir()
    seeded = pod.seed_lineage(new, sibling, 10**9)
    assert seeded is not None and seeded["dropped_units"] == 1
    debug = new / "target/debug"
    for gone in (
        "deps/libfoo-1a.rlib",
        "deps/foo-1a.d",
        ".fingerprint/foo-1a",
        "build/foo-1a",
    ):
        assert not (debug / gone).exists(), gone
    assert (debug / "deps/libbar-2b.rlib").exists()
    assert (debug / ".fingerprint/bar-2b/lib-bar").exists()
    assert not (new / "tree/app/src/lib.rs").exists()  # gone from the source


def test_without_room_a_new_lineage_starts_cold(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    sibling = _lineage(cache, "shop-rust/0", runs=1, bytes=50_000)
    _build(sibling)
    new = cache / "lineages/shop-rust/1"
    new.mkdir()
    assert pod.seed_lineage(new, sibling, 40_000) is None
    assert not (new / "target").exists() and not (new / "tree").exists()


def test_a_run_in_a_new_lineage_starts_seeded(tmp_path: Path) -> None:
    from tests.unit.dev.test_pod import _archive, _run
    from tests.unit.dev.test_pod import _spec as run_spec

    data = _archive({"app/a.txt": "x"})
    build = 'head -c 200000 /dev/zero > "$CARGO_TARGET_DIR/blob"'
    first, _ = _run(tmp_path, data, run_spec(data, ["sh", "-c", build]), "w1")
    sibling = Path(first["cache"]["lineage_dir"])
    busy = pod._try_lock(sibling)  # that run's branch builds again meanwhile
    assert busy is not None
    try:
        command = ["sh", "-c", 'test -f "$CARGO_TARGET_DIR/blob"']
        again, lines = _run(tmp_path, data, run_spec(data, command, run="r2"), "w2")
        # A budget too small for the copy: the third run starts cold.
        small = run_spec(
            data, command, run="r3", cache_max_bytes=300_000, max_lineages=3
        )
        busy_too = pod._try_lock(Path(again["cache"]["lineage_dir"]))
        assert busy_too is not None
        try:
            cold, _ = _run(tmp_path, data, small, "w3")
        finally:
            os.close(busy_too)
    finally:
        os.close(busy)
    assert again["state"] == "passed", again  # the blob came with the copy
    cache = again["cache"]
    assert cache["warm"] == "seeded" and cache["seed"]["from"] == "app-rust/0"
    assert cache["lineage"] == "app-rust/1" and cache["lineage_bytes"] >= 200_000
    assert any("seeded from app-rust/0" in line for line in lines)
    assert cold["cache"]["warm"] is False and "seed" not in cold["cache"]
    assert cold["state"] == "failed"  # no blob: it started cold


# ---------------------------------------------------------- pause, not kill


class _Cache:
    """A watch's world: the disk, the clock and the other busy lineages."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.lineage = _lineage(tmp_path / "cache", "shop-rust/0", taken_at=20.0)
        (self.lineage / "target" / "blob").write_bytes(b"x" * 200_000)
        self.now = 0.0
        self.cap = 100_000
        self.others: list[dict[str, Any]] = []
        self.lines: list[str] = []
        monkeypatch.setattr(pod, "filesystem", lambda _p: (10**9, 2 * 10**9))

    def watch(self, taken_at: float = 20.0) -> pod.CacheWatch:
        return pod.CacheWatch(
            self.lineage, self.cap, 200_000, every=1, measure_every=1,
            clock=lambda: self.now, make_room=lambda _bytes: self.cap,
            lineages=lambda: [
                {"name": "shop-rust/0", "busy": True, "taken_at": taken_at},
                *self.others,
            ],
            taken_at=taken_at, emit=self.lines.append,
        )  # fmt: skip

    def tick(self, watch: pod.CacheWatch) -> bool:
        self.now += 2
        return watch()


def _other(taken_at: float, **extra: Any) -> dict[str, Any]:
    return {
        "name": "other/0",
        "busy": True,
        "bytes": 3 * 2**30,
        "taken_at": taken_at,
        **extra,
    }


def test_the_youngest_run_pauses_and_resumes_once_room_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _Cache(tmp_path, monkeypatch)
    world.others = [_other(10.0)]
    watch = world.watch()
    assert world.tick(watch) is False and watch.stopped is None
    assert watch.paused is not None and watch.paused["reason"] == "over-cap"
    assert (
        json.loads((world.lineage / "meta.json").read_text())["paused"]["reason"]
        == "over-cap"
    )
    assert "1 other run(s) hold 3.0 GiB" in world.lines[-1]
    assert world.tick(watch) is False and watch.paused is not None  # still no room
    world.cap = 10**9  # the other run ended and its lineage was evicted
    assert world.tick(watch) is False and watch.paused is None
    assert watch.pauses == [{"reason": "over-cap", "seconds": 4.0}]
    assert "paused" not in json.loads((world.lineage / "meta.json").read_text())
    assert world.lines[-1].startswith("[piceli-dev] run resumed after 4 s")


def test_an_older_run_continues_and_a_lone_run_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _Cache(tmp_path, monkeypatch)
    world.others = [_other(30.0)]  # a younger run
    older = world.watch()
    assert world.tick(older) is False and older.paused is None and older.stopped is None
    assert "this run continues" in world.lines[-1]
    # A younger run that is paused does not count: the only other one is older.
    world.others = [_other(10.0, paused={"since": 1.0, "reason": "over-cap"})]
    assert world.tick(world.watch()) is False
    world.others = []  # alone: the lineage itself is over the budget
    lone = world.watch()
    assert world.tick(lone) is True and lone.stopped == "over-cap"


def test_a_paused_run_resumes_when_it_is_the_oldest_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _Cache(tmp_path, monkeypatch)
    world.others = [_other(10.0)]
    watch = world.watch()
    world.tick(watch)
    assert watch.paused is not None
    # The older run ended; a younger one, paused itself, still holds space.
    world.others = [_other(30.0, paused={"since": 1.0, "reason": "over-cap"})]
    assert world.tick(watch) is False and watch.paused is None
    assert "(the oldest run in the cache)" in world.lines[-1]
    world.others = []  # then alone and still over: it stops
    assert world.tick(watch) is True and watch.stopped == "over-cap"


def test_a_run_paused_too_long_stops_and_frees_its_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0.56.0: paused runs no longer hold their lineages for good."""
    world = _Cache(tmp_path, monkeypatch)
    world.others = [_other(10.0)]  # an older run that keeps the cache full
    watch = world.watch()
    watch.pause_limit = 5
    world.tick(watch)
    assert watch.paused is not None
    assert world.tick(watch) is False and world.tick(watch) is False  # 4 s
    assert world.tick(watch) is True and watch.stopped == "over-cap"  # 6 s
    assert watch.paused is None and watch.pauses == [
        {"reason": "over-cap", "seconds": 6.0}
    ]
    assert "paused 6 s while 1 older run(s) still ran" in world.lines[-1]
    assert "paused" not in json.loads((world.lineage / "meta.json").read_text())


def test_paused_time_does_not_count_against_the_deadline(tmp_path: Path) -> None:
    """A real process group, stopped and continued; the deadline moves."""
    began = time.monotonic()
    seen: list[float] = []
    script = "import time\nfor i in range(10**6):\n    print(i, flush=True)\n    time.sleep(0.01)"

    def paused() -> bool:
        return 0.15 <= time.monotonic() - began < 0.45

    code, timed_out = pod.run_command(
        [sys.executable, "-c", script], tmp_path, dict(os.environ), 0.5,
        lambda _line: None, lambda _line: seen.append(time.monotonic() - began),
        None, paused,
    )  # fmt: skip
    took = time.monotonic() - began
    assert timed_out is True and code is not None and code < 0
    assert took >= 0.75  # 0.5 s of deadline plus the 0.3 s paused
    assert not [at for at in seen if 0.25 < at < 0.4]  # nothing ran while paused
    assert [at for at in seen if at > 0.55]  # it ran again after


def test_a_run_paused_for_ever_times_out_at_twice_its_timeout(tmp_path: Path) -> None:
    """The paused time given back is at most the timeout (the Job allows 2x)."""
    began = time.monotonic()
    code, timed_out = pod.run_command(
        [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path,
        dict(os.environ), 0.3, lambda _line: None, lambda _line: None,
        None, lambda: True,
    )  # fmt: skip
    took = time.monotonic() - began
    assert timed_out is True and code is not None and code < 0
    assert 0.55 <= took < 5
