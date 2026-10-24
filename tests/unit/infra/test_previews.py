"""Previews in minutes: ``piceli env up BRANCH`` through the GitOps controller.

The controller is the real :class:`~piceli.infra.controller.CompositionController`
reading bare Git remotes in ``tmp_path`` (the composition example's ``shop``
and ``catalog``), with the fake ports of its own tests: no cluster, no
network. The example's branch rule here also declares a preview URL and
``down_when_merged=True``, and its controller takes prebuilt images.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from piceli.gitops import GitOpsError
from piceli.gitops.state import DirectoryChannel, prebuilt_request, preview_request
from piceli.infra.composition import load_composition
from piceli.infra.controller import CompositionConfig, CompositionController
from piceli.infra.sources import SourceSet
from tests.unit.infra.git_support import Repo, example_files, git
from tests.unit.infra.test_controller import EXAMPLE, FakePorts

URL = "https://{branch}.preview.example.org"


def _infra(*, merged: bool) -> str:
    text = (EXAMPLE / "infra.py").read_text()
    text = text.replace(
        "controller=Controller(on=REGISTRY_NODE),",
        "controller=Controller(on=REGISTRY_NODE, prebuilt=True),",
    )
    return text.replace(
        "on_nodes=[BRANCH_NODE],",
        f'on_nodes=[BRANCH_NODE],\n        url="{URL}",'
        + ("\n        down_when_merged=True," if merged else ""),
    )


class World:
    def __init__(self, tmp_path: Path, *, merged: bool = True) -> None:
        remotes = tmp_path / "remotes"
        self.shop = Repo(remotes, "shop", example_files(EXAMPLE / "shop"))
        self.catalog = Repo(remotes, "catalog", example_files(EXAMPLE / "catalog"))
        infra = tmp_path / "infra.py"
        infra.write_text(_infra(merged=merged))
        self.composition = load_composition(infra)
        state = tmp_path / "state"
        self.ports = FakePorts()
        self.channel = DirectoryChannel(state)
        self.clock = {"now": 1_000_000.0}
        self.registry: set[str] = set()
        self.logs: list[str] = []
        self.controller = CompositionController(
            CompositionConfig(composition=self.composition.to_dict(), poll_seconds=60),
            state_dir=state,
            sources=SourceSet(self.composition.sources, state / "sources"),
            ports=self.ports,
            channel=self.channel,
            clock=lambda: self.clock["now"],
            log=self.logs.append,
        )
        self.controller._in_registry = self.registry.__contains__  # type: ignore[method-assign]

    def poll(self) -> dict[str, Any]:
        self.clock["now"] += 1
        return self.controller.poll_once()

    def ask(self, branch: str, action: str = "up") -> str:
        key, body = preview_request(branch, action=action)
        self.channel.add_request(key, body)
        return key

    def offer(self, branch: str, component: str = "web") -> str:
        """Push the image a train built for ``component`` at the branch's head,
        under the change key the controller itself computes."""
        sources = self.controller.sources
        sources.new_poll()
        sources.fetch("shop")
        commit = git(self.shop.work, "rev-parse", branch)
        contract = sources.contract("shop", commit, component)
        key = sources.digest("shop", commit, contract)
        digest = "sha256:" + hashlib.sha256(key.encode()).hexdigest()
        host = self.composition.registry.host
        ref = f"{host}/{self.controller._repository(component)}@{digest}"
        self.registry.add(ref)
        self.channel.add_request(
            *prebuilt_request(
                "ci-1",
                {
                    component: {
                        "key": key,
                        "pull_ref": ref,
                        "manifest_digest": digest,
                        "source": "shop",
                        "commit": commit,
                    }
                },
                {"host": "ci-1", "train": {"name": "shop", "run": "r1"}},
            )
        )
        return ref


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    remotes = tmp_path / "remotes"
    monkeypatch.setenv("COMPOSITION_SHOP_URL", str(remotes / "shop.git"))
    monkeypatch.setenv("COMPOSITION_CATALOG_URL", str(remotes / "catalog.git"))
    return World(tmp_path)


def _merge(repo: Repo, branch: str) -> str:
    git(repo.work, "checkout", "-q", "main")
    git(repo.work, "merge", "-q", "--no-ff", "-m", f"merge {branch}", branch)
    git(repo.work, "push", "-q", "origin", "main")
    return git(repo.work, "rev-parse", "HEAD")


# ------------------------------------------------------------ env up


def test_env_up_with_a_prebuilt_image_deploys_without_a_build(world: World) -> None:
    world.poll()
    assert world.ports.builder.built == [["api", "catalog", "web"]]
    world.shop.commit({"web/index.html": "<h1>login</h1>\n"}, branch="wp-login")
    ref = world.offer("wp-login")
    key = world.ask("wp-login")
    status = world.poll()
    env = status["envs"]["wp-login"]
    assert env["state"] == "deployed"
    assert world.ports.builder.built == [["api", "catalog", "web"]]  # no build
    assert env["components"]["web"]["image"] == ref
    assert env["preview"] == {
        "request": key,
        "url": "https://wp-login.preview.example.org",
    }


def test_env_up_without_an_offer_builds_as_today(world: World) -> None:
    world.poll()
    world.shop.commit({"web/index.html": "<h1>login</h1>\n"}, branch="wp-login")
    key = world.ask("wp-login")
    status = world.poll()
    assert status["envs"]["wp-login"]["state"] == "deployed"
    assert world.ports.builder.built[-1] == ["web"]
    assert status["envs"]["wp-login"]["preview"]["request"] == key


def test_env_up_of_a_deployed_branch_marks_it_without_a_redeploy(
    world: World,
) -> None:
    world.poll()
    world.shop.commit({"web/index.html": "<h1>login</h1>\n"}, branch="wp-login")
    world.poll()
    deployed = len(world.ports.deployed)
    key = world.ask("wp-login")
    status = world.poll()
    assert len(world.ports.deployed) == deployed
    assert status["envs"]["wp-login"]["preview"]["request"] == key


def test_a_branch_outside_the_patterns_gets_a_preview_on_request(
    world: World,
) -> None:
    world.poll()
    world.shop.commit({"web/index.html": "<h1>fix</h1>\n"}, branch="fix-typo")
    status = world.poll()
    assert "fix-typo" not in status["envs"]  # not wp-*: no environment by itself
    world.ask("fix-typo")
    status = world.poll()
    assert status["envs"]["fix-typo"]["state"] == "deployed"
    assert (
        status["envs"]["fix-typo"]["preview"]["url"]
        == "https://fix-typo.preview.example.org"
    )
    world.ask("fix-typo", action="down")
    status = world.poll()
    assert "fix-typo" not in status["envs"] and world.ports.removed == ["fix-typo"]
    world.shop.commit({"web/index.html": "<h1>fix 2</h1>\n"}, branch="fix-typo")
    status = world.poll()
    assert "fix-typo" not in status["envs"]  # on request only


def test_a_preview_of_an_unknown_branch_is_rejected(world: World) -> None:
    world.poll()
    key = world.ask("no-such-branch")
    status = world.poll()
    assert "no-such-branch" not in status["envs"]
    (rejected,) = [r for r in status["rejected_requests"] if r["request"] == key]
    assert rejected["reason"] == "gitops-request-invalid"


def test_preview_requests_check_the_branch_name() -> None:
    for bad in ("", "a..b", "-x", "x y", "a" * 300):
        with pytest.raises(GitOpsError):
            preview_request(bad)
    with pytest.raises(GitOpsError):
        preview_request("wp-x", action="sideways")
    first, _ = preview_request("wp/login")
    second, _ = preview_request("wp/login")
    assert first != second  # each request is its own (wait follows its key)


# ------------------------------------------------------------ env down, merge


def test_env_down_tears_down_until_the_next_push(world: World) -> None:
    world.poll()
    world.shop.commit({"web/index.html": "<h1>login</h1>\n"}, branch="wp-login")
    world.poll()
    world.ask("wp-login", action="down")
    status = world.poll()
    assert "wp-login" not in status["envs"] and world.ports.removed == ["wp-login"]
    status = world.poll()
    assert "wp-login" not in status["envs"]  # the branch is still there
    world.shop.commit({"web/index.html": "<h1>login 2</h1>\n"}, branch="wp-login")
    status = world.poll()
    assert status["envs"]["wp-login"]["state"] == "deployed"


def test_a_branch_merged_into_main_is_torn_down(world: World) -> None:
    world.poll()
    world.shop.commit({"web/index.html": "<h1>login</h1>\n"}, branch="wp-login")
    # A branch with no commit of its own (at main's head) is no merge.
    git(world.shop.work, "push", "-q", "origin", "main:refs/heads/wp-empty")
    status = world.poll()
    assert status["envs"]["wp-login"]["state"] == "deployed"
    assert status["envs"]["wp-empty"]["state"] == "deployed"
    _merge(world.shop, "wp-login")
    status = world.poll()
    assert "wp-login" not in status["envs"]
    assert world.ports.removed == ["wp-login"]
    assert any("merged into main" in line for line in world.logs)
    assert status["envs"]["wp-empty"]["state"] == "deployed"
    assert status["envs"]["main"]["state"] == "deployed"
    status = world.poll()  # the merged branch still exists: it stays down
    assert "wp-login" not in status["envs"]


def test_merged_branches_stay_up_without_down_when_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remotes = tmp_path / "remotes"
    monkeypatch.setenv("COMPOSITION_SHOP_URL", str(remotes / "shop.git"))
    monkeypatch.setenv("COMPOSITION_CATALOG_URL", str(remotes / "catalog.git"))
    world = World(tmp_path, merged=False)
    world.poll()
    world.shop.commit({"web/index.html": "<h1>login</h1>\n"}, branch="wp-login")
    world.poll()
    _merge(world.shop, "wp-login")
    status = world.poll()
    assert status["envs"]["wp-login"]["state"] == "deployed"
    assert world.ports.removed == []


# ------------------------------------------------------------ served at once


def test_the_run_loop_wakes_for_a_preview_request(world: World) -> None:
    """A request is served within a few seconds, not at the next poll (60 s)."""
    from piceli.k8s.cli.gitops import WAKE_CHECK_SECONDS, _loop

    world.poll()
    world.shop.commit({"web/index.html": "<h1>login</h1>\n"}, branch="wp-login")
    world.offer("wp-login")
    asked: dict[str, float] = {}
    polls: list[float] = []
    original = world.controller.poll_once
    wall: dict[str, float] = {}

    def poll_once() -> dict[str, Any]:
        polls.append(world.clock["now"])
        started = time.perf_counter()
        try:
            return original()
        finally:
            wall[f"poll{len(polls)}"] = time.perf_counter() - started

    world.controller.poll_once = poll_once  # type: ignore[method-assign]

    def sleep(seconds: float) -> None:
        world.clock["now"] += seconds
        if "at" not in asked and len(polls) == 1 and world.clock["now"] >= polls[0] + 5:
            asked["at"] = world.clock["now"]
            world.ask("wp-login")

    _loop(
        world.controller,
        lambda: None,
        lambda: len(polls) >= 2,
        sleep,
        lambda: world.clock["now"],
        False,
    )
    assert len(polls) == 2
    assert polls[1] - asked["at"] <= WAKE_CHECK_SECONDS  # not 60 s later
    status = world.channel.read_status() or {}
    assert status["envs"]["wp-login"]["state"] == "deployed"
    assert world.ports.builder.built == [["api", "catalog", "web"]]
    # Toy Git and fake ports: one whole poll (ls-remote, offer, apply) is
    # well under the wake interval (informative bound, not a benchmark).
    assert wall["poll2"] < 30


def test_only_user_requests_wake_the_loop(world: World) -> None:
    world.poll()
    assert world.controller.wake_requested() is False
    world.channel.add_request(
        "build-progress.x", {"kind": "build-progress", "build": "b"}
    )
    assert world.controller.wake_requested() is False
    world.ask("wp-login")
    assert world.controller.wake_requested() is True


def test_the_loop_without_wake_support_sleeps_the_whole_poll() -> None:
    from piceli.k8s.cli.gitops import _loop

    class Plain:
        config = type("C", (), {"poll_seconds": 60})()
        polls = 0

        def poll_once(self) -> Mapping[str, Any]:
            self.polls += 1
            return {}

    plain = Plain()
    now = {"t": 0.0}
    times: list[float] = []

    def sleep(seconds: float) -> None:
        now["t"] += seconds

    def stopped() -> bool:
        times.append(now["t"])
        return plain.polls >= 2

    _loop(plain, lambda: None, stopped, sleep, lambda: now["t"], False)
    assert plain.polls == 2 and now["t"] >= 60
