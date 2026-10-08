"""Each recorded development run as OpenTelemetry (0.18.0)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")

from tests.unit.infra.test_controller_otel import Signals


def _record(**extra: Any) -> dict[str, Any]:
    return {
        "run": "20261008t071900-ab12",
        "requester": "agent-7",
        "priority": "agent",
        "profile": "rust",
        "created_at": "2026-10-08T07:19:00Z",
        "started_at": "2026-10-08T07:19:30Z",
        "finished_at": "2026-10-08T07:21:00Z",
        "state": "failed",
        "exit_code": 101,
        "reason": "dev-command-failed",
        "durations": {
            "sync": 2.0,
            "fetch": 1.0,
            "build": 60.0,
            "test": 20.0,
            "command": 80.0,
        },
        "tests": {"passed": 40, "failed": 1, "ignored": 0, "suites": 3},
        "cache": {
            "lineage": "shop-rust-ab/0",
            "warm": True,
            "crates_compiled": 4,
            "crates_locked": 400,
            "hit_ratio": 0.99,
        },
        **extra,
    }


def test_a_run_is_a_trace_with_its_phases_and_an_event(tmp_path: Path) -> None:
    signals = Signals()
    clock = {"now": 1_790_000_000.0}
    telemetry = signals.telemetry(tmp_path, clock)
    telemetry.dev_run(_record())
    spans = {s.name: s for s in signals.spans.get_finished_spans()}
    root = spans["DEV rust agent-7"]
    assert root.parent is None
    assert root.attributes["piceli.dev.run"] == "20261008t071900-ab12"
    assert root.attributes["piceli.dev.state"] == "failed"
    assert root.attributes["error.type"] == "dev-command-failed"
    assert root.attributes["piceli.dev.queue.wait"] == 30.0
    assert root.attributes["piceli.dev.cache.hit_ratio"] == 0.99
    assert root.attributes["piceli.dev.tests.failed"] == 1
    assert root.status.status_code.name == "ERROR"
    assert (root.end_time - root.start_time) / 1e9 == 120.0
    phases = {name for name, span in spans.items() if span.parent is not None}
    assert phases == {"queue", "sync", "fetch", "build", "test"}
    assert spans["queue"].end_time == spans["sync"].start_time
    (event,) = signals.events("piceli.dev.run.finished")
    assert event.trace_id == root.context.trace_id
    waits = signals.metrics()["piceli.dev.queue.wait"]
    assert waits[0].sum == 30.0
    durations = signals.metrics()["piceli.dev.run.duration"]
    assert durations[0].attributes["piceli.dev.state"] == "failed"


def test_a_cancelled_run_without_times_still_ends_its_trace(tmp_path: Path) -> None:
    signals = Signals()
    telemetry = signals.telemetry(tmp_path, {"now": 1_790_000_000.0})
    telemetry.dev_run(
        {
            "run": "r2",
            "requester": "a",
            "state": "cancelled",
            "reason": "dev-run-cancelled",
            "finished_at": "2026-10-08T07:21:00Z",
        }
    )
    (root,) = signals.spans.get_finished_spans()
    assert root.attributes["piceli.dev.state"] == "cancelled"


def test_disabled_telemetry_ignores_runs(tmp_path: Path) -> None:
    from piceli.gitops.otel import ControllerTelemetry

    ControllerTelemetry.disabled(tmp_path).dev_run(_record())  # no error, nothing sent
