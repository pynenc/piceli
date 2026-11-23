"""0.75.0: a build host whose container engine could not start a build is
backed off, not quarantined for its sources; repeated failures block it,
and a block shows in ``gitops hosts`` and ``gitops alarms``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.gitops.alarms import Alarms, observe
from piceli.gitops.state import (
    build_claim_request,
    build_failed_request,
    built_request,
    host_release_request,
)
from piceli.infra import remote_builds
from piceli.infra.builder_serve import ENGINE_FAILED
from piceli.k8s.cli import app as cli
from tests.unit.infra.test_build_host_handover import _serve, _world
from tests.unit.infra.test_remote_builds import _images

ENGINE_REASON = "the container engine could not start the build (exit 125)"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    found = _world(tmp_path, monkeypatch, hosts=("builder-1",), host_wait="30m")
    found["state"] = tmp_path / "state"
    return found


def _engine_fails_once(
    world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """builder-1's builder serve, its engine exits 125 (sources are faked)."""
    from piceli.infra import builder_serve

    monkeypatch.setattr(builder_serve, "export_source", lambda *_a, **_k: None)
    worker = _serve(world, tmp_path, "builder-1")
    worker.container = lambda *_a: ENGINE_FAILED
    return worker


def _new_build(world: dict[str, Any], n: int) -> dict[str, Any] | None:
    """A new commit; the build it asks a host for (``None``: no host may
    take it, the in-cluster fallback built it at once)."""
    world["shop"].commit({"web/index.html": f"<h1>v{n}</h1>\n"})
    status = world["controller"].poll_once()
    found = status["controller"].get("builds_wanted") or []
    return found[0] if found else None


def _engine_failure(world: dict[str, Any], wanted: dict[str, Any], **kw: Any) -> None:
    channel = world["channel"]
    channel.add_request(*build_claim_request(wanted["id"], "builder-1"))
    world["controller"].poll_once()
    channel.add_request(
        *build_failed_request(
            wanted["id"], "builder-1", ENGINE_REASON, "", early=True, **kw
        )
    )
    world["controller"].poll_once()


def _hosts(world: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    result = CliRunner().invoke(
        cli, ["gitops", "hosts", "--state-dir", str(world["state"]), "--json"]
    )
    assert result.exit_code == 0, result.output
    return result.stderr, json.loads(result.stdout)


def test_one_engine_failure_blocks_no_host_and_the_next_build_runs_there(
    world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, channel, clock = world["controller"], world["channel"], world["clock"]
    ports = world["ports"]
    controller.poll_once()
    worker = _engine_fails_once(world, tmp_path, monkeypatch)
    assert worker.once() is not None
    (body,) = [b for b in channel.requests().values() if b["kind"] == "build-failed"]
    assert body["early"] is True and body["cause"] == "engine"
    assert "source" not in body
    assert any("(cause: container engine)" in line for line in worker.said)

    status = controller.poll_once()
    # Handed over: the fallback builds it at once; no source is quarantined.
    assert ports.builder.built == [["api", "catalog", "web"]]
    assert "source_quarantine" not in status["controller"]
    held = status["controller"]["engine_backoff"]["builder-1"]
    assert held["state"] == "backoff" and held["failures"] == 1
    assert held["cause"] == "engine" and "container engine" in held["reason"]
    until = held_until(world)
    assert held["until"] == remote_builds._iso(until)
    assert 0 < until - clock["now"] <= remote_builds.ENGINE_BACKOFF_SECONDS
    said, shown = _hosts(world)
    assert "engine backoff until" in said and "cause: container engine" in said
    assert shown["engine_backoff"]["builder-1"]["state"] == "backoff"
    # A backoff alone raises no alarm.
    assert "build-host-blocked:builder-1" not in observe(status, clock["now"], 600)

    # While backed off, builder-1 may not take a new build: the fallback
    # builds it at once, and the controller refuses its claim.
    assert _new_build(world, 2) is None
    assert len(ports.builder.built) == 2
    entry = {"id": "b", "kind": "spec", "commits": {"shop": "a" * 40}}
    why = remote_builds.refused(controller.state, "builder-1", entry, clock["now"])
    assert why is not None and "container engine" in why and "backed off" in why

    # After the backoff, the next build runs on builder-1 and clears it.
    clock["now"] += remote_builds.ENGINE_BACKOFF_SECONDS + 1
    wanted = _new_build(world, 3)
    assert wanted is not None
    later = _serve(world, tmp_path, "builder-1")
    assert later.pick() is not None
    channel.add_request(*build_claim_request(wanted["id"], "builder-1"))
    status = controller.poll_once()
    assert status["controller"]["builds_wanted"][0]["claimed"]["host"] == "builder-1"
    channel.add_request(*built_request(wanted["id"], "builder-1", _images(wanted), {}))
    status = controller.poll_once()
    assert "engine_backoff" not in status["controller"]
    assert (
        remote_builds.ENGINE_KEY not in controller.state
        or not (controller.state[remote_builds.ENGINE_KEY])
    )
    assert "builds_fallback" not in status["controller"]


def held_until(world: dict[str, Any]) -> float:
    record = world["controller"].state[remote_builds.ENGINE_KEY]["builder-1"]
    return float(record["until"])


def test_repeated_engine_failures_block_the_host_and_raise_an_alarm(
    world: dict[str, Any], tmp_path: Path
) -> None:
    controller, channel, clock = world["controller"], world["channel"], world["clock"]
    controller.poll_once()
    waits = []
    for n in range(remote_builds.ENGINE_BLOCK_AFTER):
        wanted = _new_build(world, n + 2)
        assert wanted is not None
        _engine_failure(world, wanted, cause="engine")
        record = controller.state[remote_builds.ENGINE_KEY]["builder-1"]
        waits.append(round(float(record["until"]) - clock["now"]))
        clock["now"] = float(record["until"]) + 1
    assert waits == [300, 600, 1200]  # 5 min, then doubling

    status = controller.poll_once()
    held = status["controller"]["engine_backoff"]["builder-1"]
    assert held["state"] == "blocked" and held["failures"] == 3
    assert "source_quarantine" not in status["controller"]
    # Blocked: no build for it, also long after the last backoff would end.
    clock["now"] += 24 * 3600
    assert _new_build(world, 9) is None
    status = controller.status()
    entry = {"id": "b", "kind": "spec", "commits": {"shop": "a" * 40}}
    why = remote_builds.refused(controller.state, "builder-1", entry, clock["now"])
    assert why is not None and "blocked" in why
    blocked = _serve(world, tmp_path, "builder-1")
    blocked.wanted()
    assert blocked.engine["state"] == "blocked"

    said, shown = _hosts(world)
    assert "ENGINE BLOCKED" in said and "cause: container engine" in said
    assert "release-host builder-1" in said
    assert shown["engine_backoff"]["builder-1"]["state"] == "blocked"

    alarm = observe(status, clock["now"], 600)["build-host-blocked:builder-1"]
    assert alarm.reason == "build-host-engine-failed"
    assert "container engine" in alarm.text and "release-host builder-1" in alarm.text
    result = CliRunner().invoke(
        cli, ["gitops", "alarms", "--state-dir", str(world["state"]), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert "build host builder-1 blocked" in result.stderr
    assert "container engine" in result.stderr

    # Every image build went to the fallback: that is an alarm too.
    fallback = observe(status, clock["now"], 600)["builds-fallback:main"]
    assert fallback.reason == "build-host-fallback-only"
    assert status["controller"]["builds_fallback"]["main"]["builds"] >= 3

    channel.add_request(*host_release_request("builder-1"))
    status = controller.poll_once()
    assert "engine_backoff" not in status["controller"]
    assert "build-host-blocked:builder-1" not in observe(status, clock["now"], 600)


def test_an_early_failure_from_an_older_builder_with_no_cause(
    world: dict[str, Any],
) -> None:
    """A 0.74.0 builder sends no cause: an engine failure names no source,
    so no source is quarantined; a fetch failure names its source."""
    controller = world["controller"]
    controller.poll_once()
    wanted = _new_build(world, 2)
    assert wanted is not None
    _engine_failure(world, wanted)  # no cause, no source
    status = controller.status()
    assert "source_quarantine" not in status["controller"]
    assert status["controller"]["engine_backoff"]["builder-1"]["failures"] == 1
    assert status["envs"]["main"]["state"] == "deployed"

    world["clock"]["now"] += 3600
    wanted = _new_build(world, 3)
    assert wanted is not None
    _engine_failure(world, wanted, source="shop")  # a fetch failure, no cause
    status = controller.status()
    held = status["controller"]["source_quarantine"]["builder-1"]
    assert list(held) == ["shop"] and held["shop"]["cause"] == "source"


def test_a_source_quarantine_ends_after_a_day(world: dict[str, Any]) -> None:
    controller, clock = world["controller"], world["clock"]
    controller.poll_once()
    wanted = _new_build(world, 2)
    assert wanted is not None
    _engine_failure(world, wanted, source="shop", cause="source")
    status = controller.status()
    held = status["controller"]["source_quarantine"]["builder-1"]["shop"]
    assert held["until"] == remote_builds._iso(
        clock["now"] + remote_builds.SOURCE_QUARANTINE_SECONDS
    )
    assert "engine_backoff" not in status["controller"]
    alarm = observe(status, clock["now"], 600)["build-host-blocked:builder-1"]
    assert alarm.reason == "build-host-source-failed"
    clock["now"] += remote_builds.SOURCE_QUARANTINE_SECONDS + 1
    wanted = _new_build(world, 3)
    assert wanted is not None
    world["channel"].add_request(*build_claim_request(wanted["id"], "builder-1"))
    status = controller.poll_once()
    assert "source_quarantine" not in status["controller"]
    assert status["controller"]["builds_wanted"][0]["claimed"]["host"] == "builder-1"


def test_a_quarantine_an_older_controller_wrote_ends_a_day_after_it_began() -> None:
    state = {
        remote_builds.SOURCE_QUARANTINE_KEY: {
            "builder-1": {"shop": {"reason": "x", "at": "2026-01-01T00:00:00Z"}}
        }
    }
    entry = {"kind": "spec", "commits": {"shop": "a" * 40}}
    began = remote_builds._epoch("2026-01-01T00:00:00Z")
    assert began is not None
    assert remote_builds.blocked_sources(state, "builder-1", entry, began + 60)
    assert not remote_builds.blocked_sources(state, "builder-1", entry, began + 86401)
    # Without a time (the status of an older caller) it still holds.
    assert remote_builds.blocked_sources(state, "builder-1", entry)


def test_a_build_that_ran_clears_the_engine_failures() -> None:
    state: dict[str, Any] = {}
    remote_builds.engine_failed(state, "builder-1", ENGINE_REASON, "b1", 0.0)
    remote_builds.engine_failed(state, "builder-1", ENGINE_REASON, "b2", 400.0)
    assert state[remote_builds.ENGINE_KEY]["builder-1"]["failures"] == 2
    state[remote_builds.STATE_KEY] = {"b3": {"id": "b3", "kind": "spec"}}
    body = {"id": "b3", "host": "builder-1", "reason": "the build exited 101"}
    assert remote_builds.failed(state, body, 2000.0) is False
    assert not state[remote_builds.ENGINE_KEY]


def test_fallback_alarm_policy() -> None:
    since = remote_builds._iso(1000.0)
    status = {"controller": {"builds_fallback": {
        "main": {"builds": 2, "since": since, "last_at": since}}}}  # fmt: skip
    assert "builds-fallback:main" not in observe(status, 2000.0, 600)
    assert "builds-fallback:main" in observe(status, 1000.0 + 6 * 3600, 600)
    assert "builds-fallback:main" in observe(status, 2000.0, 600, (2, 3600.0))
    alarms = Alarms(fallback_after=5, fallback_for="1h")
    assert alarms.fallback == (5, 3600.0)
    assert Alarms.from_dict(alarms.to_dict()).fallback == (5, 3600.0)
    from piceli.infra.cluster import ClusterError

    with pytest.raises(ClusterError):
        Alarms(fallback_after=0)
    with pytest.raises(ClusterError):
        Alarms(fallback_for="10s")
