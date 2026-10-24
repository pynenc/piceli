"""0.28.0: the controller records a deploy's cycle (``piceli.cycle``).

A deploy built on a build host, on a fake clock: first seen, asked, claimed,
built, delivered, deployed. Its cycle is in the environment's status and in
its history run, with each phase's seconds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from piceli.cycle.record import iso
from piceli.gitops.state import build_claim_request, built_request
from tests.unit.infra.test_remote_builds import _images, _world


def test_a_deploy_built_on_a_host_records_its_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch)
    controller, channel, clock = world["controller"], world["channel"], world["clock"]
    t0 = clock["now"]
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    clock["now"] = t0 + 30
    channel.add_request(*build_claim_request(wanted["id"], "builder-1"))
    controller.poll_once()
    clock["now"] = t0 + 330
    receipt = {"host": "builder-1", "started_at": iso(t0 + 31),
               "finished_at": iso(t0 + 320)}  # fmt: skip
    channel.add_request(
        *built_request(wanted["id"], "builder-1", _images(wanted), receipt)
    )
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "deployed", main
    found = main["cycle"]
    assert found["kind"] == "environment" and found["target"] == "main"
    assert found["ok"] and found["from"] == "seen"
    assert found["seen_at"] == iso(t0) and found["checked_at"] == iso(t0 + 330)
    assert found["seconds"] == 330.0
    phases = found["phases"]
    assert phases["queued"] == 0.0
    assert phases["waiting-for-host"] == 30.0
    assert phases["building"] == 290.0
    assert phases["delivering"] == 10.0
    assert "host_build" not in main and "first_step_at" not in main
    document = channel.read_history()
    (run,) = [r for r in document["envs"]["main"]["runs"] if r.get("cycle")]
    assert run["cycle"] == found


def test_a_later_step_of_a_running_commit_is_its_own_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.gitops.state import sync_request

    world = _world(tmp_path, monkeypatch)
    controller, channel, clock = world["controller"], world["channel"], world["clock"]
    controller.poll_once()
    clock["now"] += 11 * 60  # no host claimed it: the cluster builds it
    status = controller.poll_once()
    first: dict[str, Any] = status["envs"]["main"]["cycle"]
    assert first["ok"] and first["phases"]["waiting-for-host"] == 660.0
    clock["now"] += 3600
    channel.add_request(*sync_request("main"))
    status = controller.poll_once()
    again = status["envs"]["main"]["cycle"]
    assert again["seen_at"] == iso(clock["now"])  # not an hour of queue
    assert again.get("seconds") in (0.0, None)
