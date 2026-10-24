"""Activate first, move the boot default after the checks (0.26.2).

The agent activates a new system with ``switch-to-configuration test``
(the boot entry stays), runs the checks, and only then sets the system
profile and the boot default (``nix-env --set`` and ``boot``). A pending
attempt is written before activation: a run that was killed or hung
(its own switch stopped it, a reboot) is rolled back by the next run
before anything else, and a reboot comes up on the old system.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from .test_agent import (
    OLD,
    Clock,
    FakeHost,
    Killed,
    commit_of,
    host,
    make_agent,
    publish,
    status,
    store_path,
)

NEW = store_path(commit_of(1))


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    path = tmp_path / "cache"
    path.mkdir()
    return path


@pytest.fixture
def fake(tmp_path: Path) -> FakeHost:
    link = tmp_path / "current-system"
    link.symlink_to(OLD)
    return FakeHost(link)


def verbs(fake: FakeHost) -> list[tuple[str, str]]:
    """``(system, verb)`` of each ``switch-to-configuration`` and the
    profile moves (``("set", path)``), in order."""
    found = []
    for call in fake.calls:
        if call[0].endswith("/bin/switch-to-configuration"):
            found.append((call[0].split("/bin/")[0], call[1]))
        elif call[0] == "fake-nix-env" and "--set" in call:
            found.append((call[-1], "set"))
        elif call[0] == "fake-systemctl" and "is-active" in call:
            found.append(("check", call[-1]))
    return found


def state(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "state" / "state.json").read_text())


def test_activation_comes_first_and_the_boot_default_moves_after_the_checks(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    poll = make_agent(tmp_path, host(cache_dir), fake).run_once()
    assert poll["state"] == "deployed"
    assert verbs(fake) == [
        (NEW, "test"), ("check", "nginx.service"), (NEW, "set"), (NEW, "boot"),
    ]  # fmt: skip
    assert fake.boot == NEW and os.readlink(fake.current) == NEW
    body = status(tmp_path)
    assert [s["name"] for s in body["last_attempt"]["stages"]] == [
        "resolve", "verify", "realise", "activate", "checks", "boot",
    ]  # fmt: skip
    assert body.get("pending") is None and state(tmp_path).get("pending") is None
    assert not any(c[1:] == ["switch"] for c in fake.calls)


def test_failed_checks_roll_back_and_never_move_the_boot_default(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    fake.unit_active = False
    poll = make_agent(tmp_path, host(cache_dir), fake).run_once()
    assert poll["state"] == "rolled-back" and poll["reason"] == "host-check-failed"
    assert verbs(fake)[0] == (NEW, "test")
    assert verbs(fake)[-1] == (OLD, "test")
    assert not any(v in ("set", "boot") for _, v in verbs(fake))
    assert fake.boot is None and os.readlink(fake.current) == OLD
    assert not FakeHost.generations(tmp_path / "profile")  # the profile never moved
    assert state(tmp_path).get("pending") is None


def test_a_hung_check_counts_as_failed_and_rolls_back(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    fake.hang = "is-active"
    poll = make_agent(tmp_path, host(cache_dir), fake, Clock()).run_once()
    assert poll["state"] == "rolled-back" and poll["reason"] == "host-check-failed"
    check = status(tmp_path)["last_attempt"]["checks"][0]
    assert check["state"] == "failed"
    assert all(t is not None and t >= 1 for t in fake.timeouts)
    assert os.readlink(fake.current) == OLD and fake.boot is None


def test_a_hung_activation_rolls_back(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    fake.hang = f"{NEW}/bin/switch-to-configuration test"
    poll = make_agent(tmp_path, host(cache_dir), fake).run_once()
    assert poll["state"] == "rolled-back" and poll["reason"] == "host-switch-failed"
    assert verbs(fake)[-1] == (OLD, "test") and fake.boot is None


def test_a_failed_boot_step_rolls_back_the_activation_and_the_profile(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    fake.fail_verb.add(("boot", NEW))
    poll = make_agent(tmp_path, host(cache_dir), fake).run_once()
    assert poll["state"] == "rolled-back" and poll["reason"] == "host-switch-failed"
    assert verbs(fake)[-3:] == [(OLD, "test"), (OLD, "set"), (OLD, "boot")]
    assert fake.boot == OLD and os.readlink(fake.current) == OLD
    assert os.readlink(tmp_path / "profile").endswith("-2-link")
    assert os.readlink(tmp_path / "profile-2-link") == OLD


def test_an_agent_killed_mid_check_is_rolled_back_by_its_next_run(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    fake.kill = "is-active"  # the switch restarted the agent's unit
    with pytest.raises(Killed):
        make_agent(tmp_path, host(cache_dir), fake).run_once()
    assert os.readlink(fake.current) == NEW and fake.boot is None
    pending = state(tmp_path)["pending"]
    assert pending["store_path"] == NEW and pending["from"] == OLD
    assert status(tmp_path)["pending"]["store_path"] == NEW
    fake.kill = None
    fake.calls.clear()
    poll = make_agent(tmp_path, host(cache_dir), fake).run_once()
    # The rollback comes first, before the poll resolves anything.
    assert verbs(fake) == [(OLD, "test")]
    assert os.readlink(fake.current) == OLD and fake.boot is None
    assert poll["state"] == "skipped" and poll["reason"] == "host-build-bad"
    body = status(tmp_path)
    attempt = body["last_attempt"]
    assert attempt["state"] == "rolled-back"
    assert attempt["reason"] == "host-deploy-interrupted"
    assert attempt["rollback"]["state"] == "done" and attempt["rollback"]["to"] == OLD
    assert body.get("pending") is None and state(tmp_path).get("pending") is None
    assert body["current"]["store_path"] == OLD


def test_a_reboot_after_an_interrupted_attempt_is_on_the_old_system(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    fake.kill = "is-active"
    with pytest.raises(Killed):
        make_agent(tmp_path, host(cache_dir), fake).run_once()
    # A reboot: the boot default never moved, so the old system comes up.
    fake.current.unlink()
    fake.current.symlink_to(OLD)
    fake.kill = None
    fake.calls.clear()
    poll = make_agent(tmp_path, host(cache_dir), fake).run_once()
    assert verbs(fake) == []  # nothing to roll back
    assert poll["state"] == "skipped" and poll["reason"] == "host-build-bad"
    attempt = status(tmp_path)["last_attempt"]
    assert attempt["reason"] == "host-deploy-interrupted"
    assert attempt["rollback"] == {"to": OLD, "state": "not-needed", "seconds": 0.0,
                                   "reason": None}  # fmt: skip


def test_a_command_waits_for_the_recovery_too(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    fake.kill = "is-active"
    with pytest.raises(Killed):
        make_agent(tmp_path, host(cache_dir), fake).run_once()
    fake.kill = None
    result = make_agent(tmp_path, host(cache_dir), fake).control("unpin")
    assert result["state"] == "unpinned"
    assert os.readlink(fake.current) == OLD
    assert status(tmp_path)["last_attempt"]["reason"] == "host-deploy-interrupted"
