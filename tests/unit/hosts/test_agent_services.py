"""Service mode (0.45.0): a release moves a Nix profile and restarts only
its units; the system, its profile and its boot entry are never touched."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from piceli.hosts.agent import Agent
from piceli.hosts.model import HostCheck, HostError, NixHost, Services, agent_config

from .test_agent import (
    SYSTEM,
    Clock,
    FakeHost,
    _no_network,
    commit_of,
    host,
    publish,
    status,
    store_path,
)

PACKAGE = ".#packages.x86_64-linux.shop"
UNITS = ["shop-api.service", "shop-worker.service"]


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    path = tmp_path / "cache"
    path.mkdir()
    return path


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    path = tmp_path / "profiles" / "shop"
    path.parent.mkdir()
    return path


@pytest.fixture
def fake(tmp_path: Path) -> FakeHost:
    link = tmp_path / "current-system"
    link.symlink_to("/nix/store/" + "9" * 32 + "-nixos-system-web-1")
    return FakeHost(link)


def services_host(cache_dir: Path, **overrides: Any) -> NixHost:
    return host(
        cache_dir, flake=PACKAGE,
        scope=Services("/nix/var/nix/profiles/shop", UNITS), **overrides,
    )  # fmt: skip


def make_agent(
    tmp_path: Path, declared: NixHost, fake: FakeHost, profile: Path
) -> Agent:
    config = {
        **agent_config(declared),
        "state_dir": str(tmp_path / "state"),
        "profile": str(profile),
        "system": SYSTEM,
        "tools": {"nix": "fake-nix", "nix_env": "fake-nix-env",
                  "nix_store": "fake-nix-store", "systemctl": "fake-systemctl"},
    }  # fmt: skip
    config["current_system"] = str(profile)  # the profile is what runs
    clock = Clock()
    return Agent(
        config, runner=fake, opener=_no_network, sleep=clock.sleep,
        monotonic=clock.monotonic, check_interval=2.0,
    )  # fmt: skip


def deploy(tmp_path: Path, cache_dir: Path, fake: FakeHost, profile: Path,
           n: int, **host_args: Any) -> dict[str, Any]:  # fmt: skip
    publish(cache_dir, commit_of(n), tag=f"v1.0.{n}", attribute=PACKAGE)
    declared = services_host(cache_dir, **host_args)
    return make_agent(tmp_path, declared, fake, profile).run_once()


def test_services_scope_is_declared_and_shipped_to_the_agent(cache_dir: Path) -> None:
    declared = services_host(cache_dir)
    assert agent_config(declared)["scope"] == {
        "kind": "services", "profile": "/nix/var/nix/profiles/shop", "units": UNITS,
    }  # fmt: skip
    assert "scope" not in agent_config(host(cache_dir))  # the system: unchanged
    for bad in (
        lambda: Services("/nix/var/nix/profiles/system", UNITS),
        lambda: Services("/etc/shop", UNITS),
        lambda: Services("/nix/var/nix/profiles/shop", []),
        lambda: Services("/nix/var/nix/profiles/shop", "shop.service"),
        lambda: Services("/nix/var/nix/profiles/shop", ["bad unit"]),
    ):
        with pytest.raises(HostError) as raised:
            bad()
        assert raised.value.code == "host-invalid"


def test_a_release_moves_the_profile_and_restarts_only_its_units(
    tmp_path: Path, cache_dir: Path, fake: FakeHost, profile: Path
) -> None:
    system = os.readlink(fake.current)
    poll = deploy(tmp_path, cache_dir, fake, profile, 1)
    assert poll["state"] == "deployed", poll
    assert FakeHost.generations(profile) == {1: store_path(commit_of(1))}
    assert fake.commands("fake-systemctl restart") == [
        ["fake-systemctl", "restart", *UNITS]
    ]
    assert fake.commands("switch-to-configuration") == []
    assert fake.boot is None and os.readlink(fake.current) == system
    assert status(tmp_path)["current"]["store_path"] == store_path(commit_of(1))
    # The next poll finds the profile current and does nothing.
    calls = len(fake.calls)
    again = make_agent(tmp_path, services_host(cache_dir), fake, profile).run_once()
    assert again["state"] == "current" and len(fake.calls) == calls


def test_a_failed_check_sets_the_profile_back_and_restarts_again(
    tmp_path: Path, cache_dir: Path, fake: FakeHost, profile: Path
) -> None:
    assert deploy(tmp_path, cache_dir, fake, profile, 1)["state"] == "deployed"
    fake.unit_states = [True, False]  # up at the first try, dead at the next
    fake.unit_active = False
    checks = [HostCheck.unit("shop-api.service", timeout=10, stable=4)]
    poll = deploy(tmp_path, cache_dir, fake, profile, 2, checks=checks)
    assert poll["state"] == "rolled-back" and poll["reason"] == "host-check-failed"
    gens = FakeHost.generations(profile)
    assert os.readlink(profile) == f"shop-{max(gens)}-link"
    assert gens[max(gens)] == store_path(commit_of(1))  # back on the first
    assert len(fake.commands("fake-systemctl restart")) == 3
    assert fake.commands("switch-to-configuration") == [] and fake.boot is None
    attempt = status(tmp_path)["last_attempt"]
    assert attempt["rollback"]["state"] == "done"
    assert attempt["rollback"]["to"] == store_path(commit_of(1))


def test_a_failed_restart_rolls_the_profile_back(
    tmp_path: Path, cache_dir: Path, fake: FakeHost, profile: Path
) -> None:
    assert deploy(tmp_path, cache_dir, fake, profile, 1)["state"] == "deployed"
    fake.fail_restart = True
    poll = deploy(tmp_path, cache_dir, fake, profile, 2)
    assert poll["state"] == "rolled-back" and poll["reason"] == "host-switch-failed"
    gens = FakeHost.generations(profile)
    assert gens[max(gens)] == store_path(commit_of(1))


def test_a_manual_system_switch_is_never_undone(
    tmp_path: Path, cache_dir: Path, fake: FakeHost, profile: Path
) -> None:
    assert deploy(tmp_path, cache_dir, fake, profile, 1)["state"] == "deployed"
    manual = "/nix/store/" + "8" * 32 + "-nixos-system-web-1-manual"
    fake.current.unlink()
    fake.current.symlink_to(manual)  # the owner's nixos-rebuild switch
    calls = len(fake.calls)
    poll = make_agent(tmp_path, services_host(cache_dir), fake, profile).run_once()
    assert poll["state"] == "current" and len(fake.calls) == calls
    assert os.readlink(fake.current) == manual


def test_retention_prunes_the_service_profile_only(
    tmp_path: Path, cache_dir: Path, fake: FakeHost, profile: Path
) -> None:
    system_profile = tmp_path / "system"
    for n in range(1, 4):
        FakeHost.add_generation(system_profile, f"/nix/store/{n}" + "0" * 31 + "-s")
    for n in range(1, 6):
        assert deploy(tmp_path, cache_dir, fake, profile, n,
                      generations=2)["state"] == "deployed"  # fmt: skip
    assert sorted(FakeHost.generations(profile)) == [4, 5]
    assert len(FakeHost.generations(system_profile)) == 3
    deletes = fake.commands("--delete-generations")
    assert deletes and all(str(profile) in " ".join(c) for c in deletes)


def test_the_module_leaves_the_boot_menu_alone(cache_dir: Path) -> None:
    from piceli.hosts.module import render_module

    text = render_module(services_host(cache_dir), "hosts.py:web", "0.44.0")
    assert (
        "configurationLimit" not in text
        and '\\"kind\\":\\"services\\"' in text.replace(" ", "")
    )
