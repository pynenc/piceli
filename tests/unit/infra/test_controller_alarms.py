"""0.28.0: the composition controller sends its alarms after each poll.

Local Git sources and fake deploys (``test_controller.world``); the ntfy
target's send is recorded, never sent.
"""

from __future__ import annotations

from typing import Any

import pytest

from piceli.gitops.alarms import Alarms
from piceli.gitops.state import sync_request
from piceli.infra import CompositionError, Notify
from piceli.infra.controller import CompositionConfig, CompositionController
from piceli.train.notify import NtfyNotify
from tests.unit.infra.test_controller import world

__all__ = ["world"]


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []

    def send(self: Any, payload: Any, title: str, text: str) -> str:
        found.append((title, text))
        return "sent"

    monkeypatch.setattr(NtfyNotify, "send_message", send)
    return found


def _with_alarms(world: dict[str, Any], ports: Any = None) -> CompositionController:
    old = world["controller"]
    alarms = Alarms(targets=[Notify.ntfy("https://ntfy.example.org/deploys")])
    config = CompositionConfig(
        composition=old.config.composition,
        poll_seconds=old.config.poll_seconds,
        alarms=alarms.to_dict(),
    )
    return CompositionController(
        config,
        state_dir=old.state_dir,
        sources=old.sources,
        ports=ports or world["ports"],
        channel=world["channel"],
        clock=lambda: world["clock"]["now"],
    )


def test_a_failed_environment_alarms_once_across_polls_and_restarts(
    world: dict[str, Any], sent: list[tuple[str, str]]
) -> None:
    controller, ports = _with_alarms(world), world["ports"]

    def build(items: Any, checkout: Any) -> Any:
        raise CompositionError("component-build-unsupported", "no builder image")

    working = ports.builder.build
    ports.builder.build = build
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "failed"
    titles = [title for title, _ in sent]
    assert titles == ["piceli: main failed"]
    assert "(component-build-unsupported)" in sent[0][1]
    assert sent[0][1].endswith("Next: piceli gitops status")
    view = status["controller"]["alarms"]
    assert view["open"][0]["alarm"] == "env-failed:main" and view["sent"] == 1
    assert view["targets"] == ["ntfy ntfy.example.org"]

    world["clock"]["now"] += 60
    controller.poll_once()
    restarted = _with_alarms(world)  # a new process, the same state
    world["clock"]["now"] += 60
    restarted.poll_once()
    assert len(sent) == 1  # once per event

    ports.builder.build = working
    world["channel"].add_request(*sync_request("main"))
    world["clock"]["now"] += 60
    status = restarted.poll_once()
    assert status["envs"]["main"]["state"] == "deployed"
    assert [title for title, _ in sent] == [
        "piceli: main failed",
        "piceli: main recovered",
    ]
    world["clock"]["now"] += 60
    restarted.poll_once()
    assert len(sent) == 2


def test_a_new_version_alarms_once_and_a_failed_send_never_fails_the_poll(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: Any, payload: Any, title: str, text: str) -> str:
        raise OSError("status 503")

    monkeypatch.setattr(NtfyNotify, "send_message", refuse)
    controller = _with_alarms(world)
    controller.state["alarms"] = {"version": "0.1.0"}  # an older one
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "deployed"
    (failure,) = status["controller"]["alarms"]["failures"]
    assert failure == {
        "target": "ntfy ntfy.example.org",
        "alarm": "controller-restarted:controller",
        "state": "firing",
        "reason": "gitops-alarm-failed",
        "why": "OSError",
        "at": failure["at"],
    }
    controller.poll_once()
    assert len(controller.status()["controller"]["alarms"]["failures"]) == 1


def test_without_alarms_nothing_changes(world: dict[str, Any]) -> None:
    status = world["controller"].poll_once()
    assert "alarms" not in status["controller"]
    assert "alarms" not in world["controller"].state
