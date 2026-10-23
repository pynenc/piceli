"""Merge-train state for the showcase UI's Trains view (fake data).

:func:`live_trains` writes a train's history (runs, step logs, flaky tests)
under ``root`` and keeps one run in progress while the context is open: it
holds the train's lock, as a real run does, and a thread appends lines to
the running step's log and moves to the next step, so the page shows a live
run. Everything lives under ``root`` (the showcase's temporary directory).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from piceli.train.state import TrainState

TRAIN = "shop"
HEAD = "4b7e0c2d9a1f3e5b6c8d0e2f4a6b8c0d1e3f5a7b"


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _record(n: int, start: datetime, unit: float, ejected: bool) -> dict[str, Any]:
    run = f"{start.strftime('%Y%m%dT%H%M%SZ')}-{n:06x}"
    steps = [
        {"name": "merge", "kind": "merge", "verdict": "passed", "duration": 0.2, "host": "local"},
        {"name": "spec", "kind": "spec", "verdict": "passed", "duration": 0.6, "host": "local"},
        {"name": "cargo-check", "kind": "cargo-check", "verdict": "passed", "duration": 48.0 + n, "host": "local"},
        {"name": "cargo-test", "kind": "cargo-test", "verdict": "passed", "duration": unit, "host": "local"},
        {"name": "fitness", "kind": "command", "verdict": "failed" if ejected else "passed", "duration": 3.2, "host": "local"},
    ]  # fmt: skip
    branches = [{"branch": f"feat-{n}", "verdict": "joined", "trial": 1}]
    if ejected:
        branches.append(
            {"branch": f"fix-{n}", "verdict": "ejected", "step": "fitness", "reason": "train-step-failed", "trial": 1}
        )  # fmt: skip
    tag = f"tested/{start.strftime('%Y-%m-%d')}.{n}"
    head = HEAD if n == 6 else f"{n:x}" * 40
    return {
        "schema": "piceli.train.v1", "run": run, "train": TRAIN, "state": "ejected" if ejected else "joined",
        "ok": not ejected, "started": _iso(start), "duration": round(sum(s["duration"] for s in steps) + 5, 1),
        "host": "local", "head": head[:40], "branches": branches,
        "trials": [{"id": 1, "steps": steps, "branches": [b["branch"] for b in branches]}],
        "flaky": [], "promote": {"state": "promoted", "tag": tag, "head": head[:40]}, "push": {"state": "pushed"},
    }  # fmt: skip


def targets() -> dict[str, Any]:
    """A targets document whose ``main`` runs the newest promoted head."""
    return {
        "schema": "piceli.targets.v1",
        "generated_at": "2026-10-23T10:16:30Z",
        "controller": {"read": True},
        "targets": [
            {"kind": "environment", "name": "main", "where": "my-cluster/shop", "state": "deployed",
             "revision": {"commit": "9" * 40, "tag": None}, "pinned": {"source": "product", "commit": HEAD}},
            {"kind": "nix-host", "name": "web-1", "where": "192.0.2.10", "state": "deployed",
             "revision": {"commit": "d" * 40, "tag": "web-v1.2.0"}, "pinned": None},
        ],
    }  # fmt: skip


@contextmanager
def live_trains(root: Path) -> Iterator[Path]:
    state = TrainState(root / TRAIN)
    now = datetime.now(UTC)
    for n, unit in enumerate((78.0, 81.0, 80.0, 84.0, 79.0, 118.0), start=1):
        state.write_record(
            (rec := _record(n, now - timedelta(hours=7 - n), unit, ejected=n == 4))[
                "run"
            ],
            rec,
        )
    state.record_flakes(
        [{"target": "-p api --test http", "test": "retries_once", "step": "cargo-test",
          "tail": "---- retries_once stdout ----\nthread 'retries_once' panicked: timed out after 2s",
          "branches": ["feat-3"]}],
        "run-3", _iso(now - timedelta(hours=4)),
    )  # fmt: skip
    state.record_flakes(
        [{"target": "-p api --test http", "test": "retries_once", "step": "cargo-test", "tail": "timed out", "branches": ["feat-6"]}],
        "run-6", _iso(now - timedelta(hours=1)),
    )  # fmt: skip
    run = f"{now.strftime('%Y%m%dT%H%M%SZ')}-0000ff"
    stop = threading.Event()
    with state.lock(run):
        directory = state.run_dir(run)
        state.append_event(
            run,
            {
                "event": "run-started",
                "at": _iso(now - timedelta(seconds=65)),
                "branches": ["feat-search", "fix-cart"],
            },
        )
        state.append_event(
            run,
            {
                "event": "trial-started",
                "at": _iso(now - timedelta(seconds=64)),
                "trial": 1,
                "branches": ["feat-search", "fix-cart"],
                "base": HEAD,
            },
        )
        state.append_event(
            run,
            {
                "event": "step-started",
                "at": _iso(now - timedelta(seconds=63)),
                "trial": 1,
                "step": "cargo-check",
                "kind": "cargo-check",
                "host": "local",
                "log": str(directory / "01-cargo-check.log"),
            },
        )
        state.append_event(
            run,
            {
                "event": "step-finished",
                "at": _iso(now - timedelta(seconds=12)),
                "trial": 1,
                "step": "cargo-check",
                "verdict": "passed",
                "duration": 51.0,
                "host": "local",
            },
        )
        log = directory / "01-cargo-test.log"
        log.write_text(
            "$ cargo test --no-fail-fast -p api -p core\n   Compiling core v0.4.0\n   Compiling api v0.4.0\n"
        )
        state.append_event(run, {"event": "step-started", "at": _iso(now - timedelta(seconds=12)), "trial": 1, "step": "cargo-test", "kind": "cargo-test", "host": "local", "log": str(log)})  # fmt: skip

        def advance() -> None:
            n = 0
            while not stop.wait(0.7):
                n += 1
                with log.open("a") as handle:
                    handle.write(f"test api::search::case_{n:03d} ... ok\n")

        worker = threading.Thread(target=advance, daemon=True)
        worker.start()
        try:
            yield root
        finally:
            stop.set()
            worker.join(timeout=2)
