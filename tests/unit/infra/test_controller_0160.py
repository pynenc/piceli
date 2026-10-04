"""0.16.0: what the history and the status tell about time and restarts.

Reproduces the gaps of 0.15.1:

- runs had stage states and seconds but no stage or build times;
- a run the controller died under stayed ``running`` in the history and its
  record ``in_progress`` in the status, forever;
- policy approvals and declared stops had ``at: null``;
- stops, starts and teardowns were not in the history;
- ``last_poll`` stood still during a long step (an 18-minute build read as
  a stale controller): no heartbeat.

The controller runs against local Git sources with fake deploys that journal
real runs (as in ``test_controller_history.py``).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from piceli.gitops import history, liveness
from piceli.gitops.heartbeat import Heartbeat
from piceli.gitops.recovery import INTERRUPTED
from piceli.gitops.state import DirectoryChannel, approve_request, stop_request
from piceli.infra.controller import CompositionController
from piceli.pipeline.journal import Journal
from tests.unit.infra.test_controller import world
from tests.unit.infra.test_controller_history import (
    PLAN,
    _manual_main,
    _published,
    journaled,
)

__all__ = ["journaled", "world"]


def _runs(channel: DirectoryChannel, env: str = "main") -> list[dict[str, Any]]:
    return list(_published(channel)["envs"][env]["runs"])


def _iso(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        history._when(value)
    except ValueError:  # pragma: no cover - _when never raises
        return False
    return history._when(value) != history._EPOCH


# ------------------------------------------------------------ timestamps


def test_each_stage_and_build_of_a_run_has_its_times(
    journaled: dict[str, Any],
) -> None:
    controller, channel = journaled["controller"], journaled["channel"]
    controller.poll_once()
    run = _runs(channel)[0]
    assert run["kind"] == "run"
    for name in ("inputs", "build", "deliver", "plan", "apply", "checks"):
        stage = run["stages"][name]
        assert _iso(stage["started_at"]) and _iso(stage["finished_at"]), (name, stage)
        assert stage["started_at"] <= stage["finished_at"]
    # The controller's own builds (one per image or component), and the mirror.
    assert set(run["builds"]) == {"api", "cache", "catalog", "web"}
    for name, build in run["builds"].items():
        assert build["state"] == "done", name
        assert _iso(build["started_at"]) and _iso(build["finished_at"])
    # A policy approval has its time: the run's creation.
    assert run["approved_by"]["via"] == "policy"
    assert run["approved_by"]["at"] == run["started_at"] or _iso(
        run["approved_by"]["at"]
    )


def test_a_failed_build_has_its_times_and_state(journaled: dict[str, Any]) -> None:
    from piceli.gitops.state import sync_request
    from piceli.infra import CompositionError

    controller, channel, ports = (
        journaled["controller"],
        journaled["channel"],
        journaled["ports"],
    )
    controller.poll_once()

    def build(items: Any, checkout: Any) -> Any:
        raise CompositionError("component-build-failed", "the build Job failed")

    ports.builder.build = build
    channel.add_request(*sync_request("main", "web"))
    controller.poll_once()
    failed = _runs(channel)[0]
    assert failed["builds"]["web"]["state"] == "failed"
    assert _iso(failed["builds"]["web"]["started_at"])


def test_the_wait_for_an_owner_approval_is_measured(
    journaled: dict[str, Any],
) -> None:
    controller, channel, clock = (
        journaled["controller"],
        journaled["channel"],
        journaled["clock"],
    )
    _manual_main(controller)
    clock["now"] = 2_000_000_000.0
    status = controller.poll_once()
    assert status["envs"]["main"]["approval_required_since"] == ("2033-05-18T03:33:20Z")
    clock["now"] += 90
    channel.add_request(*approve_request("main", PLAN, via="ui"))
    status = controller.poll_once()
    assert "approval_required_since" not in status["envs"]["main"]
    run = _runs(channel)[0]
    assert run["approved_by"] == {"via": "ui", "at": "2033-05-18T03:34:50Z"}
    assert run["approval_wait"] == {
        "started_at": "2033-05-18T03:33:20Z",
        "finished_at": "2033-05-18T03:34:50Z",
        "seconds": 90.0,
    }


def test_prune_and_rollback_times_become_stages_of_the_history_run(
    tmp_path: Path,
) -> None:
    """A run journal with a prune (in the apply's execution) and a rollback
    (in the checks output) lists both as stages with their times."""
    journal = Journal(tmp_path / "pipelines" / "main" / "environments" / "main")
    run = journal.create(
        pipeline={"app": "shop", "target": {"namespace": "shop"}},
        combined_hash="sha256:" + "1" * 64,
        until="checks",
        approval="sha256:" + "1" * 64,
        plan={"stages": {}},
    )
    for name in ("inputs", "build", "deliver", "plan"):
        run.set_stage(name, state="running")
        run.set_stage(name, state="done", seconds=0.1)
    run.set_stage("apply", state="running")
    run.set_stage(
        "apply",
        state="done",
        seconds=1.0,
        output={
            "release": "shop-1",
            "execution": {
                "state": "ready",
                "prune": {
                    "started_at": "2026-10-04T10:00:01.000Z",
                    "finished_at": "2026-10-04T10:00:03.500Z",
                },
            },
        },
    )
    run.set_stage("checks", state="running")
    run.set_stage(
        "checks",
        state="failed",
        reason="pipeline-checks-failed",
        output={
            "passed": False,
            "results": [],
            "rollback": {
                "state": "ready",
                "release": "shop-0",
                "started_at": "2026-10-04T10:00:05.000Z",
                "finished_at": "2026-10-04T10:00:09.000Z",
            },
        },
    )
    run.set_state("rolled-back", reason="pipeline-checks-failed")
    entry = history.run_entry(run.path)
    assert entry is not None
    assert entry["stages"]["prune"] == {
        "state": "done",
        "started_at": "2026-10-04T10:00:01.000Z",
        "finished_at": "2026-10-04T10:00:03.500Z",
        "seconds": 2.5,
    }
    assert entry["stages"]["rollback"]["state"] == "done"
    assert entry["stages"]["rollback"]["seconds"] == 4.0
    assert (
        _iso(entry["finished_at"]) and entry["finished_at"] == run.data["finished_at"]
    )


# ------------------------------------------------------------ interrupted


def _restart(world: dict[str, Any], ports: Any = None) -> CompositionController:
    controller = world["controller"]
    return CompositionController(
        controller.config,
        state_dir=controller.state_dir,
        sources=controller.sources,
        ports=ports or controller.ports,
        channel=world["channel"],
        clock=lambda: world["clock"]["now"],
    )


def test_a_run_the_controller_died_under_is_interrupted_after_its_restart(
    journaled: dict[str, Any],
) -> None:
    from piceli.gitops.state import save_state, sync_request

    controller, channel, clock = (
        journaled["controller"],
        journaled["channel"],
        journaled["clock"],
    )
    controller.poll_once()
    # The controller dies during the next deploy: its record says
    # in_progress and its run journal stays running at the apply.
    clock["now"] = time.time()
    since = history._when(_runs(channel)[0]["started_at"]).timestamp() + 5
    journal = Journal(
        controller.state_dir / "pipelines" / "main" / "environments" / "main"
    )
    run = journal.create(
        pipeline={"app": "shop", "target": {"namespace": "shop"}},
        combined_hash="sha256:" + "2" * 64,
        until="checks",
        approval="sha256:" + "2" * 64,
        plan={"stages": {}},
    )
    for name in ("inputs", "build", "deliver", "plan"):
        run.set_stage(name, state="running")
        run.set_stage(name, state="done", seconds=0.1)
    run.set_stage("apply", state="running")
    record = controller.state["envs"]["main"]
    record["in_progress"] = {
        "action": "deploy",
        "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since - 5)),
    }
    save_state(controller.state_dir, controller.state)
    channel.publish(controller.status())  # what the dead controller left

    clock["now"] = time.time() + 60
    restarted = _restart(journaled)
    status = restarted.poll_once()
    main = status["envs"]["main"]
    assert "in_progress" not in main
    assert main["interrupted"]["action"] == "deploy"
    assert _iso(main["interrupted"]["at"])

    data = json.loads(run.path.read_text())
    assert data["state"] == "interrupted" and data["reason"] == INTERRUPTED
    assert _iso(data["finished_at"])
    assert data["stages"]["apply"]["state"] == "interrupted"
    assert _iso(data["stages"]["apply"]["finished_at"])

    entry = next(item for item in _runs(channel) if item["run_id"] == run.run_id)
    assert entry["state"] == "interrupted" and entry["run_state"] == "interrupted"
    assert entry["reason"] == INTERRUPTED
    assert _iso(entry["finished_at"])
    assert not [item for item in _runs(channel) if item["state"] == "running"]

    # Its next step clears ``interrupted``.
    channel.add_request(*sync_request("main"))
    status = restarted.poll_once()
    assert "interrupted" not in status["envs"]["main"]
    assert status["envs"]["main"]["state"] == "deployed"


def test_a_run_another_process_holds_is_not_marked(tmp_path: Path) -> None:
    from piceli.gitops.recovery import interrupt_runs

    root = tmp_path / "pipelines" / "main" / "environments" / "main"
    journal = Journal(root)
    run = journal.create(
        pipeline={"app": "shop"},
        combined_hash="sha256:" + "3" * 64,
        until="checks",
        approval="sha256:" + "3" * 64,
        plan={},
    )
    with journal.locked():  # alive: another process runs it
        assert interrupt_runs(tmp_path) == []
    assert json.loads(run.path.read_text())["state"] == "running"
    assert interrupt_runs(tmp_path) == [run.run_id]


# ------------------------------------------------------------ stops


def _stop_world(world: dict[str, Any]) -> dict[str, Any]:
    from tests.unit.infra.test_controller_stops import StopPorts

    ports = StopPorts()
    world["controller"].ports = ports
    world["ports"] = ports
    return world


def test_stops_and_starts_are_history_entries_next_to_the_runs(
    world: dict[str, Any],
) -> None:
    world = _stop_world(world)
    controller, channel = world["controller"], world["channel"]
    controller.poll_once()
    channel.add_request(
        *stop_request("main", via="ui", at="2026-10-04T10:00:00.000000Z")
    )
    controller.poll_once()
    channel.add_request(
        *stop_request("main", start=True, via="cli", at="2026-10-04T10:05:00.000000Z")
    )
    controller.poll_once()
    entries = _runs(channel)
    kinds = [item["kind"] for item in entries]
    assert kinds[:2] == ["start", "stop"] and "run" in kinds
    start, stop = entries[0], entries[1]
    assert stop["state"] == "stopped" and stop["env"] == "main"
    assert stop["stop"] == {
        "by": "requested",
        "via": "ui",
        "at": "2026-10-04T10:00:00.000000Z",
    }
    assert stop["by"] == "requested" and stop["via"] == "ui" and _iso(stop["at"])
    assert start["state"] == "started" and start["action"] == "start"
    assert start["by"] == "requested" and start["via"] == "cli"


def test_a_declared_stop_has_its_time(world: dict[str, Any]) -> None:
    from tests.unit.infra.test_controller_stops import _declare_stopped

    world = _stop_world(world)
    controller, channel = world["controller"], world["channel"]
    controller.poll_once()
    _declare_stopped(controller, "main")
    status = controller.poll_once()
    stop = status["envs"]["main"]["stop"]
    assert stop["by"] == "declared" and stop["via"] == "declaration"
    assert stop["at"] == "1970-01-12T13:46:40Z"  # first seen (the fake clock)
    world["clock"]["now"] += 600
    assert controller.poll_once()["envs"]["main"]["stop"]["at"] == stop["at"]
    entry = _runs(channel)[0]
    assert entry["kind"] == "stop" and entry["stop"]["at"] == stop["at"]
    _declare_stopped(controller, "main", stopped=False)
    controller.poll_once()
    start = _runs(channel)[0]
    assert start["kind"] == "start" and start["by"] == "declared-removed"
    assert "main" not in controller.state["declared_stops"]


def test_a_teardown_stays_in_the_history(world: dict[str, Any]) -> None:
    controller, channel, shop = world["controller"], world["channel"], world["shop"]
    controller.poll_once()
    shop.commit({"web/index.html": "<h1>login</h1>\n"}, branch="wp-login")
    controller.poll_once()
    shop.delete_branch("wp-login")
    status = controller.poll_once()
    assert "wp-login" not in status["envs"]
    entries = _runs(channel, "wp-login")
    assert entries[0]["kind"] == "teardown" and entries[0]["state"] == "removed"
    assert entries[0]["by"] == "removed" and entries[0]["via"] == "controller"
    assert any(item["kind"] == "run" for item in entries[1:])
    controller.poll_once()  # kept on later polls
    assert _runs(channel, "wp-login")[0]["kind"] == "teardown"


def test_removed_environments_kept_in_the_history_are_bounded() -> None:
    state: dict[str, Any] = {"history": {}}
    for index in range(history.MAX_REMOVED + 5):
        history.remember(
            state,
            f"wp-{index:02d}",
            history.lifecycle(
                "teardown", at=f"2026-10-04T10:{index:02d}:00Z", state="removed"
            ),
        )
    history.remember(state, "wp-running", {"kind": "run", "started_at": "x"})
    history.forget_gone(state, [])
    assert len(state["history"]) == history.MAX_REMOVED
    assert "wp-00" not in state["history"] and "wp-24" in state["history"]


# ------------------------------------------------------------ heartbeat


def test_a_heartbeat_publishes_only_its_field(world: dict[str, Any]) -> None:
    controller, channel, clock = world["controller"], world["channel"], world["clock"]
    controller.poll_once()
    before = channel.read_status()
    history_before = channel.read_history()
    assert before is not None
    assert before["controller"]["heartbeat_at"] == before["controller"]["last_poll"]
    assert before["controller"]["heartbeat_seconds"] == 30
    clock["now"] += 45
    controller.beat()
    after = channel.read_status()
    assert after is not None
    assert after["controller"]["heartbeat_at"] == "1970-01-12T13:47:25Z"
    after["controller"]["heartbeat_at"] = before["controller"]["heartbeat_at"]
    assert after == before  # nothing else changed
    assert channel.read_history() == history_before


def test_a_heartbeat_never_creates_a_status(tmp_path: Path) -> None:
    channel = DirectoryChannel(tmp_path)
    channel.publish_heartbeat("2026-10-04T10:00:00Z")
    assert channel.read_status() is None


def test_the_heartbeat_advances_during_a_long_build(
    world: dict[str, Any],
) -> None:
    """A build that runs for a while: ``last_poll`` stands still, the
    heartbeat thread keeps ``heartbeat_at`` moving, and its publishes never
    overlap the step's own (they share one lock)."""
    controller, channel, ports = world["controller"], world["channel"], world["ports"]
    world["clock"]["now"] = time.time()
    controller.clock = time.time
    building = threading.Event()
    seen: list[str] = []
    builder = ports.builder
    original = builder.build

    def slow_build(items: Any, checkout: Any) -> Any:
        building.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = channel.read_status() or {}
            beat = (status.get("controller") or {}).get("heartbeat_at")
            if beat and beat not in seen:
                seen.append(beat)
            if len(seen) >= 3:
                break
            time.sleep(0.05)
        return original(items, checkout)

    builder.build = slow_build
    overlaps: list[bool] = []
    real_publish = channel.publish
    active = threading.Lock()

    def guarded(status: Any) -> None:
        overlaps.append(not active.acquire(blocking=False))
        try:
            real_publish(status)
        finally:
            if active.locked():
                active.release()

    real_beat = channel.publish_heartbeat

    def guarded_beat(at: str) -> None:
        overlaps.append(not active.acquire(blocking=False))
        try:
            real_beat(at)
        finally:
            if active.locked():
                active.release()

    channel.publish = guarded  # type: ignore[method-assign]
    channel.publish_heartbeat = guarded_beat  # type: ignore[method-assign]
    with Heartbeat(controller.beat, seconds=1.0):
        status = controller.poll_once()
    assert building.is_set()
    assert status["envs"]["main"]["state"] == "deployed"
    assert len(seen) >= 3, seen  # it moved while the build ran
    assert seen == sorted(seen)
    assert not any(overlaps)


def test_the_heartbeat_thread_survives_a_failing_beat() -> None:
    calls: list[int] = []
    lines: list[str] = []

    def beat() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("the API server is unreachable")

    with Heartbeat(beat, seconds=0.05, log=lines.append):
        deadline = time.monotonic() + 5
        while len(calls) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
    assert len(calls) >= 3
    assert lines[0] == "heartbeat not published (OSError)"


NOW = 2_000_000_000.0


def _at(seconds_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - seconds_ago))


@pytest.mark.parametrize(
    ("controller", "state"),
    [
        # A long build: the poll is old, the heartbeat fresh.
        ({"last_poll": _at(1800), "heartbeat_at": _at(10)}, "running"),
        ({"last_poll": _at(1800), "heartbeat_at": _at(400)}, "stale"),
        # A controller before 0.16.0: the last poll decides.
        ({"last_poll": _at(1800)}, "stale"),
        ({"last_poll": _at(30)}, "running"),
    ],
    ids=["heartbeat-fresh", "heartbeat-old", "poll-old", "poll-fresh"],
)
def test_liveness_reads_the_heartbeat_when_there_is_one(
    controller: dict[str, Any], state: str
) -> None:
    from piceli.k8s.cli.gitops import _health

    document = {"controller": {"poll_seconds": 60, **controller}}
    found = liveness.liveness(document, {"ready": True}, NOW)
    assert found["state"] == state, found
    assert _health(document, True, NOW) == ("stale" if state == "stale" else "healthy")
    if "heartbeat_at" in controller and state == "stale":
        assert found["message"].startswith("no heartbeat for ")


def test_the_pod_loop_runs_the_heartbeat() -> None:
    from piceli.k8s.cli.gitops import _forever

    running: list[bool] = []

    class Controller:
        config = type("Config", (), {"poll_seconds": 1})()
        publish_lock = threading.RLock()

        def poll_once(self) -> None:
            running.append(
                any(t.name == "piceli-gitops-heartbeat" for t in threading.enumerate())
            )

        def beat(self) -> None:  # pragma: no cover - 30 s apart
            pass

    _forever(
        Controller(),
        lambda: None,
        stop=lambda: len(running) >= 2,
        sleep=lambda _s: None,
        handle_signals=False,
    )
    assert running == [True, True]  # the thread ran during the polls
    assert not [t for t in threading.enumerate() if t.name == "piceli-gitops-heartbeat"]
