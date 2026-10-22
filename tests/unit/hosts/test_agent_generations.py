"""The host agent keeps ``NixHost(generations=)`` system generations (0.24.0).

A 20 GB disk fills with old systems: after a deploy and its checks, the
agent deletes the system profile's older generations and collects the
store. It never deletes the current, the previous or a held system, and a
failure there is reported without failing the deploy. The profile is
emulated by :class:`~tests.unit.hosts.test_agent.FakeHost` in ``tmp_path``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from piceli.hosts.model import HostError, HostReport, agent_config
from piceli.hosts.module import render_module

from .test_agent import (
    OLD,
    Collector,
    FakeHost,
    _events,
    commit_of,
    host,
    make_agent,
    publish,
    status,
    store_path,
)


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


def seed(tmp_path: Path, count: int, running: str = OLD) -> Path:
    """``count`` generations of the system profile, the newest ``running``."""
    profile = tmp_path / "profile"
    for n in range(1, count):
        FakeHost.add_generation(profile, store_path(f"old-{n}"))
    FakeHost.add_generation(profile, running)
    return profile


def test_a_deploy_keeps_the_newest_generations_and_collects_the_store(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    profile = seed(tmp_path, 6)
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    poll = make_agent(tmp_path, host(cache_dir), fake).run_once()
    assert poll["state"] == "deployed"
    assert sorted(FakeHost.generations(profile)) == [3, 4, 5, 6, 7]
    assert fake.commands("--delete-generations") == [
        ["fake-nix-env", "-p", str(profile), "--delete-generations", "+5"],
    ]
    assert fake.commands("fake-nix-store") == [["fake-nix-store", "--gc"]]
    # Both run with a timeout, after the checks.
    names = [" ".join(call) for call in fake.calls]
    assert names.index("fake-nix-store --gc") > max(
        i for i, name in enumerate(names) if "is-active" in name
    )
    assert all(
        timeout
        for call, timeout in zip(fake.calls, fake.timeouts, strict=True)
        if call[0] in ("fake-nix-env", "fake-nix-store")
    )
    body = status(tmp_path)
    prune = body["last_attempt"]["prune"]
    assert prune == body["prune"]
    assert prune["state"] == "done" and prune["reason"] is None
    assert prune["keep"] == 5 and prune["deleted"] == [1, 2]
    assert prune["kept"] == [3, 4, 5, 6, 7] and prune["current"] == 7
    assert prune["gc"]["state"] == "done"
    assert prune["gc"]["paths"] == 12 and prune["gc"]["freed"] == "345.60 MiB"
    assert isinstance(prune["seconds"], float)


def test_nothing_to_delete_still_collects_the_store(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    seed(tmp_path, 1)
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    make_agent(tmp_path, host(cache_dir), fake).run_once()
    prune = status(tmp_path)["prune"]
    assert prune["deleted"] == [] and prune["kept"] == [1, 2]
    assert fake.commands("--delete-generations") == []
    assert prune["gc"]["state"] == "done"


def test_the_previous_and_held_systems_are_never_deleted(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    # The running system (the previous one after this deploy) is the oldest
    # generation: the profile was moved by hand since.
    profile = tmp_path / "profile"
    FakeHost.add_generation(profile, OLD)
    for n in range(2, 7):
        FakeHost.add_generation(profile, store_path(f"old-{n}"))
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    make_agent(tmp_path, host(cache_dir, generations=2), fake).run_once()
    # +2 would delete generation 1: the previous system. The agent names
    # the generations it deletes instead.
    assert fake.commands("--delete-generations") == [
        ["fake-nix-env", "-p", str(profile), "--delete-generations",
         "2", "3", "4", "5"],
    ]  # fmt: skip
    assert sorted(FakeHost.generations(profile)) == [1, 6, 7]
    assert status(tmp_path)["prune"]["protected"] == [1]
    # A rollback goes to generation 1's system and holds it: the next prune
    # keeps it, though it is not among the newest.
    agent = make_agent(tmp_path, host(cache_dir, generations=2), fake)
    assert agent.control("rollback")["state"] == "rolled-back"
    assert os.readlink(fake.current) == OLD
    # Generation 8 is OLD again: generation 1 is no longer needed for it.
    kept = FakeHost.generations(profile)
    assert kept == {7: store_path(commit_of(1)), 8: OLD}
    assert fake.commands("--delete-generations")[-1][-1] == "+2"


def test_a_failed_prune_or_gc_is_reported_and_the_deploy_stands(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    seed(tmp_path, 6)
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    fake.fail_delete = True
    fake.fail_gc = subprocess.TimeoutExpired(["nix-store"], 1)
    up = Collector(up=True)
    declared = host(cache_dir, report=HostReport("https://otel.example.net:4318"))
    poll = make_agent(tmp_path, declared, fake, opener=up).run_once()
    assert poll["state"] == "deployed"
    assert os.readlink(fake.current) == store_path(commit_of(1))
    prune = status(tmp_path)["prune"]
    assert prune["state"] == "failed" and prune["reason"] == "host-prune-failed"
    assert prune["deleted"] == []
    assert prune["gc"] == {
        "state": "failed", "reason": "host-prune-failed", "paths": None,
        "freed": None, "seconds": prune["gc"]["seconds"],
    }  # fmt: skip
    assert "piceli.host.prune" in _events(up.posts)
    # Only the gc fails: the generations go, the failure is reported.
    fake.fail_delete, fake.fail_gc = False, 1
    publish(cache_dir, commit_of(2), tag="v1.1.0")
    make_agent(tmp_path, declared, fake, opener=up).run_once()
    prune = status(tmp_path)["prune"]
    assert prune["state"] == "failed" and prune["deleted"] == [1, 2, 3]
    assert prune["gc"]["state"] == "failed"
    assert status(tmp_path)["current"]["tag"] == "v1.1.0"


def test_a_failed_deploy_prunes_nothing(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    seed(tmp_path, 6)
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    fake.unit_active = False
    make_agent(tmp_path, host(cache_dir), fake).run_once()
    assert fake.commands("--delete-generations") == []
    assert fake.commands("fake-nix-store") == []
    assert status(tmp_path)["last_attempt"]["prune"] is None


def test_generations_are_declared_and_reach_the_boot_loader(
    tmp_path: Path, cache_dir: Path
) -> None:
    assert host(cache_dir).generations == 5
    assert agent_config(host(cache_dir))["generations"] == 5
    assert agent_config(host(cache_dir, generations=3))["generations"] == 3
    for bad in (1, 0, 1001, "5", True):
        with pytest.raises(HostError) as raised:
            host(cache_dir, generations=bad)
        assert raised.value.code == "host-invalid"
    text = render_module(host(cache_dir, generations=3), "hosts.py:web", "0.24.0")
    assert "boot.loader.grub.configurationLimit = lib.mkDefault 3;" in text
    assert "boot.loader.systemd-boot.configurationLimit = lib.mkDefault 3;" in text
    assert 'nix_store = "${config.nix.package}/bin/nix-store";' in text
