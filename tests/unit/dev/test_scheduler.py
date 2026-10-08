"""The development-run queue: slots, priorities, fair share, records."""

from __future__ import annotations

from typing import Any

import pytest

from piceli.dev.model import DevBuilds
from piceli.dev.scheduler import Scheduler, auto_slots, order

IMAGE = "ghcr.io/example/builder@sha256:" + "a" * 64


def job(
    run: str,
    *,
    requester: str = "a",
    priority: str = "agent",
    created: str = "2026-10-08T07:00:00Z",
    suspend: bool = True,
    done: str | None = None,
) -> dict[str, Any]:
    conditions = [{"type": done, "status": "True"}] if done else []
    return {
        "metadata": {
            "name": f"piceli-dev-{run}",
            "creationTimestamp": created,
            "labels": {
                "piceli.io/dev-run": run,
                "piceli.io/dev-requester": requester,
                "piceli.io/dev-priority": priority,
                "piceli.io/dev-profile": "rust",
            },
        },
        "spec": {"suspend": suspend},
        "status": {
            "conditions": conditions,
            **({"active": 1} if not suspend and not done else {}),
        },
    }


def test_rounds_go_first_then_fair_share_then_age() -> None:
    queued = [
        job("a1", requester="alice", created="2026-10-08T07:00:01Z"),
        job("a2", requester="alice", created="2026-10-08T07:00:02Z"),
        job("b1", requester="bob", created="2026-10-08T07:00:03Z"),
        job(
            "r1",
            requester="coordinator",
            priority="round",
            created="2026-10-08T07:00:09Z",
        ),
        job("n1", requester="carol", priority="normal", created="2026-10-08T07:00:00Z"),
    ]
    running = [job("x", requester="alice", suspend=False)]
    picked = [
        j["metadata"]["labels"]["piceli.io/dev-run"] for j in order(queued, running)
    ]
    # The round first; bob before alice (alice already runs one); then alice
    # in age order; normal last.
    assert picked == ["r1", "b1", "a1", "a2", "n1"]


def test_auto_slots_fit_the_node() -> None:
    dev = DevBuilds(node="n", image=IMAGE, run_cpu="6", run_memory="12Gi")
    node = {"status": {"allocatable": {"cpu": "32", "memory": "64Gi"}}}
    assert auto_slots(dev, node) == 5  # memory: 64/12 = 5, cpu: 32/6 = 5
    small = {"status": {"allocatable": {"cpu": "2", "memory": "4Gi"}}}
    assert auto_slots(dev, small) == 1  # at least one
    assert auto_slots(DevBuilds(node="n", image=IMAGE, slots=3), node) == 3


class FakePort:
    def __init__(
        self,
        jobs: list[dict[str, Any]],
        results: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.items = jobs
        self.results = results or {}
        self.unsuspended: list[str] = []
        self.deleted: list[str] = []
        self.published: list[dict[str, Any]] = []

    def jobs(self, selector: str | None = None) -> list[dict[str, Any]]:
        return [j for j in self.items if j["metadata"]["name"] not in self.deleted]

    def unsuspend(self, run: str) -> None:
        self.unsuspended.append(run)
        for j in self.items:
            if j["metadata"]["labels"]["piceli.io/dev-run"] == run:
                j["spec"]["suspend"] = False
                j["status"]["active"] = 1

    def delete(self, run: str) -> None:
        self.deleted.append(f"piceli-dev-{run}")

    def result(self, run: str) -> dict[str, Any] | None:
        return self.results.get(run)

    def node(self, name: str) -> dict[str, Any]:
        return {"status": {"allocatable": {"cpu": "8", "memory": "16Gi"}}}

    def publish(self, document: dict[str, Any]) -> None:
        self.published.append(document)


def _dev(slots: int | str = 2) -> DevBuilds:
    return DevBuilds(node="n", image=IMAGE, slots=slots)  # type: ignore[arg-type]


def test_a_tick_fills_free_slots_only() -> None:
    port = FakePort([
        job("running", suspend=False),
        job("q1", requester="b", created="2026-10-08T07:00:01Z"),
        job("q2", requester="c", created="2026-10-08T07:00:02Z"),
    ])  # fmt: skip
    events: list[Any] = []
    Scheduler(port, _dev(2), clock=lambda: 1000.0, on_finished=events.append).tick()
    assert port.unsuspended == ["q1"]
    status = port.published[-1]
    assert status["slots"] == 2 and status["scheduler_at"]
    assert [r["run"] for r in status["running"]] == ["running", "q1"]
    assert [(r["run"], r["position"]) for r in status["queued"]] == [("q2", 1)]


def test_finished_runs_are_recorded_then_removed() -> None:
    result = {
        "state": "passed",
        "exit_code": 0,
        "durations": {"command": 3.0},
        "cache": {"used_bytes": 10},
    }
    port = FakePort(
        [
            job("done", suspend=False, done="Complete"),
            job("gone", suspend=False, done="Failed"),
        ],
        {"done": result},
    )
    finished: list[dict[str, Any]] = []
    scheduler = Scheduler(
        port, _dev(), clock=lambda: 1000.0, on_finished=finished.append
    )
    scheduler.tick()
    assert sorted(port.deleted) == ["piceli-dev-done", "piceli-dev-gone"]
    recent = {r["run"]: r for r in port.published[-1]["recent"]}
    assert recent["done"]["state"] == "passed" and recent["done"]["exit_code"] == 0
    assert (
        recent["gone"]["state"] == "error"
        and recent["gone"]["reason"] == "dev-run-failed"
    )
    assert port.published[-1]["cache"]["used_bytes"] == 10
    assert {f["run"] for f in finished} == {"done", "gone"}
    # Recorded once: the next tick does not record them again.
    scheduler.tick()
    assert len(finished) == 2


def test_a_cancelled_run_is_recorded_as_cancelled() -> None:
    port = FakePort([job("q1")])
    finished: list[dict[str, Any]] = []
    scheduler = Scheduler(
        port, _dev(0 + 1), clock=lambda: 1000.0, on_finished=finished.append
    )
    port.items[0]["spec"]["suspend"] = True
    scheduler.tick()  # q1 starts (one slot)
    port.items.clear()  # deleted by piceli dev cancel
    scheduler.tick()
    assert finished[-1]["run"] == "q1" and finished[-1]["state"] == "cancelled"


def test_the_history_is_bounded() -> None:
    port = FakePort([])
    scheduler = Scheduler(port, _dev(), clock=lambda: 1000.0)
    for index in range(80):
        scheduler.record({"run": f"r{index}", "state": "passed"})
    scheduler.tick()
    assert len(port.published[-1]["recent"]) == 50
    assert port.published[-1]["recent"][0]["run"] == "r79"


def test_the_loop_idles_until_installed_and_rebuilds_after_a_failed_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from piceli.dev.scheduler import loop

    calls: list[str] = []
    made = {"n": 0}

    class Flaky:
        def tick(self) -> None:
            calls.append("tick")
            if len(calls) == 1:
                raise OSError("token rotated")

    def scheduler_for() -> Any:
        made["n"] += 1
        return None if made["n"] == 1 else Flaky()

    ticks = {"n": 0}

    def stop() -> bool:
        ticks["n"] += 1
        return ticks["n"] > 6

    import piceli.dev.scheduler as module

    clock = {"now": 0.0}

    def monotonic() -> float:
        clock["now"] += 61  # every check is a minute apart
        return clock["now"]

    monkeypatch.setattr(module.time, "monotonic", monotonic)
    loop(scheduler_for, stop=stop, sleep=lambda _s: None)
    # Not installed at first; then a scheduler; its failed tick rebuilds it.
    assert made["n"] >= 3 and calls.count("tick") >= 2
