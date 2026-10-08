"""The UI's Development builds page: the public view of piceli-dev-status."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from piceli.services.dev_status import DevStatusControl
from tests.unit.services.test_ui014_controls import _query

NOW = 1_790_000_000.0  # 2026-09-21T14:13:20Z


def _raw() -> dict[str, Any]:
    return {
        "schema": "piceli.dev-status.v1",
        "scheduler_at": "2026-09-21T14:13:00Z",
        "node": "builder-1",
        "slots": 5,
        "running": [
            {
                "run": "r1",
                "requester": "agent-7",
                "priority": "agent",
                "profile": "rust",
                "secret": "never shown",
            }
        ],
        "queued": [
            {
                "run": "r2",
                "requester": "coordinator",
                "priority": "round",
                "position": 1,
            }
        ],
        "recent": [
            {
                "run": "r0",
                "state": "failed",
                "exit_code": 101,
                "reason": "dev-command-failed",
                "log_tail": ["private output"],
                "durations": {"build": 40.0},
                "tests": {"passed": 3, "failed": 1},
                "cache": {
                    "lineage": "shop-rust/0",
                    "warm": True,
                    "hit_ratio": 0.98,
                    "lineage_dir": "/cache/lineages/shop-rust/0",
                },
            }
        ],
        "cache": {
            "used_bytes": 3 * 2**30,
            "max_bytes": 100 * 2**30,
            "at": "2026-09-21T14:12:00Z",
        },
    }


def test_the_page_shows_runs_queue_and_cache_and_nothing_else(tmp_path: Path) -> None:
    view = DevStatusControl(
        _query(tmp_path), "cluster", _raw, clock=lambda: NOW
    ).status()
    assert (
        view["state"] == "running"
        and view["slots"] == 5
        and view["node"] == "builder-1"
    )
    assert view["running"] == [
        {"run": "r1", "requester": "agent-7", "priority": "agent", "profile": "rust"}
    ]
    assert view["queued"][0]["position"] == 1
    recent = view["recent"][0]
    assert "log_tail" not in recent and "lineage_dir" not in recent["cache"]
    assert recent["cache"]["hit_ratio"] == 0.98
    assert view["cache"]["used_bytes"] == 3 * 2**30


def test_a_silent_queue_is_stale_and_none_is_not_installed(tmp_path: Path) -> None:
    later = DevStatusControl(_query(tmp_path), "cluster", _raw, clock=lambda: NOW + 600)
    assert later.status()["state"] == "stale"
    absent = DevStatusControl(
        _query(tmp_path), "cluster", lambda: None, clock=lambda: NOW
    )
    assert absent.status() == {
        "schema": "piceli.ui-dev-builds.v1",
        "state": "not-installed",
    }
    assert absent.installed() is False
    assert later.installed() is True


def test_the_ui_may_read_the_dev_status_and_nothing_more() -> None:
    from piceli.infra.ui_install import render_ui
    from tests.unit.infra.test_ui_install import _by_kind, _cluster

    (role,) = _by_kind(render_ui(_cluster()))["Role"]
    (reads,) = [
        r
        for r in role["rules"]
        if r["resources"] == ["configmaps"] and r["verbs"] == ["get"]
    ]
    assert "piceli-dev-status" in reads["resourceNames"]
