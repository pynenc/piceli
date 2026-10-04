"""Stopping a named environment: ``Environment(stopped=True)`` and ``piceli env stop|start``.

Reproduces the 0.14.6 gap: only branch environments could be stopped (their
idle stop); a named environment kept running and was planned on every push.
The controller runs against local Git sources with fake deploys (as in
``test_controller.py``).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from piceli.gitops.state import stop_request
from tests.unit.infra.test_controller import FakePorts, world

__all__ = ["world"]


class StopPorts(FakePorts):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str, str | None]] = []
        self.planned: list[str] = []

    def env_up(self, pipeline: Any, name: str, **kwargs: Any) -> dict[str, Any]:
        self.planned.append(name)
        return super().env_up(pipeline, name, **kwargs)

    def env_stop(self, pipeline: Any, name: str, reason: str = "idle") -> None:
        self.calls.append(("stop", name, reason))

    def env_start(self, pipeline: Any, name: str) -> None:
        self.calls.append(("start", name, None))


@pytest.fixture
def stops(world: dict[str, Any]) -> dict[str, Any]:
    ports = StopPorts()
    world["controller"].ports = ports
    world["ports"] = ports
    return world


def _declare_stopped(controller: Any, name: str, stopped: bool = True) -> None:
    """The composition as if its module now declared ``stopped`` for ``name``."""
    composition = controller.composition
    environments = []
    for item in composition.environments:
        if getattr(item, "name", None) == name and hasattr(item, "stopped"):
            item = copy.copy(item)
            object.__setattr__(item, "stopped", stopped)
        environments.append(item)
    object.__setattr__(composition, "environments", tuple(environments))


def _ask(world: dict[str, Any], env: str, *, start: bool, at: str) -> None:
    world["channel"].add_request(*stop_request(env, start=start, via="cli", at=at))


def test_a_requested_stop_scales_to_zero_and_a_push_is_not_planned(
    stops: dict[str, Any],
) -> None:
    controller, ports = stops["controller"], stops["ports"]
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "deployed"
    deployed = dict(status["envs"]["main"]["deployed_revision"])
    _ask(stops, "main", start=False, at="2026-10-03T10:00:00.000000Z")
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert (main["state"], main["reason"]) == ("stopped", "requested")
    assert main["stop"] == {
        "by": "requested",
        "via": "cli",
        "at": "2026-10-03T10:00:00.000000Z",
    }
    assert ports.calls == [("stop", "main", "requested")]

    # The rule: a push to a followed source while stopped is neither planned
    # nor deployed (no env_up at all, no plan hash), and the record stays.
    planned = len(ports.planned)
    stops["shop"].commit({"web/index.html": "<h1>while stopped</h1>\n"})
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert len(ports.planned) == planned
    assert (main["state"], main["reason"], main["plan_hash"]) == (
        "stopped",
        "requested",
        None,
    )
    assert main["deployed_revision"] == deployed
    # Not even wanted: the record keeps the stopped revision and trigger, and
    # the environment is not stopped a second time.
    assert main["revision"] == deployed and main["trigger"] != "start"
    assert ports.calls == [("stop", "main", "requested")]

    # Start: scaled back, then the revision that moved meanwhile deploys.
    _ask(stops, "main", start=True, at="2026-10-03T10:05:00.000000Z")
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert ports.calls[-1] == ("start", "main", None)
    assert main["state"] == "deployed" and main["trigger"] == "start"
    assert main["deployed_revision"] != deployed
    assert "stop" not in main
    assert len(ports.planned) == planned + 1


def test_start_without_a_new_revision_only_scales_back(stops: dict[str, Any]) -> None:
    controller, ports = stops["controller"], stops["ports"]
    controller.poll_once()
    deploys = len(ports.deployed)
    _ask(stops, "main", start=False, at="2026-10-03T10:00:00.000000Z")
    controller.poll_once()
    _ask(stops, "main", start=True, at="2026-10-03T10:01:00.000000Z")
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "deployed"
    assert status["envs"]["main"]["reason"] is None
    assert len(ports.deployed) == deploys  # nothing to deploy: scaled back only
    assert [call[0] for call in ports.calls] == ["stop", "start"]


def test_a_stop_and_a_start_waiting_together_follow_their_order(
    stops: dict[str, Any],
) -> None:
    controller, ports = stops["controller"], stops["ports"]
    controller.poll_once()
    # Keys sort "start." before "stop."; the times decide: the later one wins.
    _ask(stops, "main", start=False, at="2026-10-03T10:00:00.000000Z")
    _ask(stops, "main", start=True, at="2026-10-03T10:00:01.000000Z")
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "deployed"
    assert ports.calls == []
    _ask(stops, "main", start=True, at="2026-10-03T10:00:02.000000Z")
    _ask(stops, "main", start=False, at="2026-10-03T10:00:03.000000Z")
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "stopped"


def test_a_declared_stop_wins_over_a_start_request(stops: dict[str, Any]) -> None:
    controller, ports = stops["controller"], stops["ports"]
    controller.poll_once()
    _declare_stopped(controller, "main")
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert (main["state"], main["reason"]) == ("stopped", "declared")
    assert main["stop"]["by"] == "declared"
    assert ports.calls == [("stop", "main", "declared")]
    assert {"name": "main", "promote": False, "stopped": True} in status["controller"][
        "environments"
    ]

    _ask(stops, "main", start=True, at="2026-10-03T10:00:00.000000Z")
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "stopped"
    assert status["rejected_requests"][-1]["reason"] == "gitops-env-stop-declared"

    # The declaration removed: the environment starts again.
    _declare_stopped(controller, "main", stopped=False)
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "deployed"
    assert ports.calls[-1] == ("start", "main", None)


def test_a_requested_stop_outlasts_a_removed_declaration(stops: dict[str, Any]) -> None:
    controller = stops["controller"]
    controller.poll_once()
    _declare_stopped(controller, "main")
    _ask(stops, "main", start=False, at="2026-10-03T10:00:00.000000Z")
    controller.poll_once()
    _declare_stopped(controller, "main", stopped=False)
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert (main["state"], main["reason"]) == ("stopped", "requested")


def test_a_declared_stop_before_the_first_deploy_never_deploys(
    stops: dict[str, Any],
) -> None:
    controller, ports = stops["controller"], stops["ports"]
    _declare_stopped(controller, "main")
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "stopped"
    assert "main" not in ports.planned


def test_stop_and_start_name_only_named_environments(stops: dict[str, Any]) -> None:
    controller = stops["controller"]
    stops["shop"].commit({"web/index.html": "<h1>b</h1>\n"}, branch="wp-login")
    controller.poll_once()
    _ask(stops, "wp-login", start=False, at="2026-10-03T10:00:00.000000Z")
    _ask(stops, "nope", start=False, at="2026-10-03T10:00:01.000000Z")
    status = controller.poll_once()
    reasons = [item["reason"] for item in status["rejected_requests"][-2:]]
    assert reasons == ["gitops-request-invalid", "gitops-request-invalid"]
    assert status["envs"]["wp-login"]["state"] == "deployed"


def test_a_failed_stop_never_deploys_meanwhile(stops: dict[str, Any]) -> None:
    controller, ports, clock = stops["controller"], stops["ports"], stops["clock"]
    controller.poll_once()

    def broken(pipeline: Any, name: str, reason: str = "idle") -> None:
        raise RuntimeError("api down")

    ports.env_stop = broken  # type: ignore[method-assign]
    _ask(stops, "main", start=False, at="2026-10-03T10:00:00.000000Z")
    stops["shop"].commit({"web/index.html": "<h1>x</h1>\n"})
    planned = len(ports.planned)
    status = controller.poll_once()
    assert status["envs"]["main"]["reason"] == "gitops-step-failed"
    assert len(ports.planned) == planned
    del ports.env_stop
    clock["now"] += 3600
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "stopped"
    assert len(ports.planned) == planned
