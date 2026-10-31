"""0.42.0: builds that never stop the controller, and builds that are not
replaced for nothing.

1. An in-cluster build Job is started, then looked at once per poll: while
   it runs, other environments' approvals and a NixRelease approval apply
   and ``gitops builds`` reads the status; a restarted controller looks at
   the same Job.
2. A commit that changes no input of a running build (here ``flake.lock``,
   which no image reads) keeps it: the host's claim continues, nothing is
   cancelled, and its images serve the new revision.
3. A build whose inputs did change goes first to the live host that held
   the build it replaces; the in-cluster fallback starts only once that
   host stops reporting.

The real controller and real Git repositories; the builder, the host and
the cluster are fakes.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from piceli.gitops.state import (
    DirectoryChannel,
    approve_request,
    build_cancel_request,
    build_claim_request,
    build_progress_request,
    built_request,
)
from piceli.infra.builders import BuildItem, BuiltImage
from piceli.infra.composition import load_composition
from piceli.infra.controller import CompositionConfig, CompositionController
from piceli.infra.remote_builds import CLAIM_STALE_SECONDS
from piceli.infra.sources import SourceSet
from tests.unit.infra.git_support import Repo, example_files
from tests.unit.infra.test_controller import EXAMPLE, FakeBuilder, FakePorts
from tests.unit.infra.test_remote_builds import (
    _image_builds,
    _images,
    _next_try,
    _world,
)

NIX_RELEASE = """
from piceli.infra import NixCache, NixHost, NixRelease, Tag as HostTag

hosts = Source(os.environ["NIX_RELEASE_URL"], name="hosts")
nix_cache = NixCache("cache-1:" + "A" * 43 + "=", url="file:///nowhere")
web_1 = NixHost("web-1", server="ssh://root@web-1.example.org", flake=".#web-1",
                follow=HostTag("web-v*"), cache=nix_cache)
web_release = NixRelease(web_1, source=hosts, builder="builder-1")
"""


class JobFake(FakeBuilder):
    """An in-cluster builder whose Jobs run until the test ends them."""

    def __init__(self) -> None:
        super().__init__()
        self.asynchronous = False
        self.environment: str | None = None
        self.attempt = 1
        self.started: list[tuple[str, list[str]]] = []
        self.looks: dict[str, int] = {}
        self.done: set[str] = set()
        self.stopped: list[str] = []

    @property
    def builds_async(self) -> bool:
        return self.asynchronous

    def start(self, kind: str, work: Sequence[BuildItem]) -> dict[str, Any]:
        name = f"piceli-image-build-{len(self.started):010d}"
        self.started.append((name, sorted(item.component for item in work)))
        return {"job": name}

    def collect(
        self, kind: str, work: Any, record: dict[str, Any], elapsed: float
    ) -> dict[str, BuiltImage] | None:
        name = str(record["job"])
        self.looks[name] = self.looks.get(name, 0) + 1
        if name not in self.done:
            record["phase"] = "running"
            return None
        return self.build(work, None)

    def stop(self, record: dict[str, Any]) -> bool:
        self.stopped.append(str(record["job"]))
        return True


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return _world(tmp_path, monkeypatch)


def _jobs_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """``main`` (auto-approved), ``rc`` (approved by hand) and a NixRelease;
    builds on hosts, then the cluster after 20 s."""
    shop = Repo(tmp_path / "remotes", "shop", example_files(EXAMPLE / "shop"))
    catalog = Repo(tmp_path / "remotes", "catalog", example_files(EXAMPLE / "catalog"))
    hosts = Repo(tmp_path / "remotes", "hosts", {"flake.nix": "{ outputs = _: {}; }"})
    for repo in (shop, catalog):
        repo.tag("v0.1.0")
    hosts.tag("web-v1.0.0")
    monkeypatch.setenv("COMPOSITION_SHOP_URL", shop.url)
    monkeypatch.setenv("COMPOSITION_CATALOG_URL", catalog.url)
    monkeypatch.setenv("NIX_RELEASE_URL", hosts.url)
    text = (EXAMPLE / "infra.py").read_text()
    text = text.replace(
        "controller=Controller(on=REGISTRY_NODE)",
        'controller=Controller(on=REGISTRY_NODE, builds=("hosts", "cluster"), '
        'host_wait="20s")',
    )
    rc = 'follow={shop: Tag("v*"), catalog: Tag("v*")},\n        auto_approve=True'
    assert text.count(rc) == 1
    text = text.replace(rc, rc.replace("True", "False"))
    (tmp_path / "infra.py").write_text(text + NIX_RELEASE)
    composition = load_composition(tmp_path / "infra.py")
    config = CompositionConfig(
        composition=composition.to_dict(), poll_seconds=10, platforms=("linux/amd64",)
    )
    ports = FakePorts()
    ports.builder = JobFake()
    clock = {"now": 1_000_000.0}
    state = tmp_path / "state"

    def controller() -> CompositionController:
        return CompositionController(
            config,
            state_dir=state,
            sources=SourceSet(composition.sources, state / "sources"),
            ports=ports,
            channel=DirectoryChannel(state),
            clock=lambda: clock["now"],
        )

    return {"controller": controller(), "restart": controller, "ports": ports,
            "channel": DirectoryChannel(state), "clock": clock, "shop": shop,
            "hosts": hosts}  # fmt: skip


def _poll(world: dict[str, Any], seconds: float = 11) -> dict[str, Any]:
    world["clock"]["now"] += seconds
    return world["controller"].poll_once()


def test_a_running_build_job_never_blocks_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _jobs_world(tmp_path, monkeypatch)
    controller, channel, jobs = (
        world["controller"],
        world["channel"],
        world["ports"].builder,
    )
    controller.poll_once()
    world["shop"].tag("v0.2.0")  # a new tag: rc deploys it
    _poll(world)
    status = _poll(world, 21)  # host_wait over: the cluster builds (at once here)
    assert status["envs"]["main"]["state"] == "deployed"
    rc = status["envs"]["rc"]
    assert rc["state"] == "approval-required", rc
    assert status["nix_releases"]["web-1"]["state"] == "baseline"

    # A new commit on main: its build Job runs over many polls.
    jobs.asynchronous = True
    world["shop"].commit({"web/index.html": "<h1>v2</h1>\n"})
    _poll(world)
    status = _poll(world, 21)
    ((name, images),) = jobs.started
    assert images == ["web"]
    main = status["envs"]["main"]
    assert main["state"] == "retrying" and main["reason"] == "build-waiting"
    assert main["waiting_for"].startswith(f"building web in the cluster: Job {name}")
    assert main["waiting_for"].endswith("(running)")
    assert main["attempts"] == 0
    assert status["controller"]["builds_wanted"] == []
    (shown,) = status["controller"]["cluster_builds"]
    assert shown["job"] == name and shown["owner"] == "main"

    # Meanwhile: rc is approved and deploys; a NixRelease tag is approved.
    channel.add_request(*approve_request("rc", rc["plan_hash"], via="cli"))
    status = _poll(world)
    assert status["envs"]["rc"]["state"] == "deployed"
    commit = world["hosts"].commit({"hosts/web-1.nix": "{ }\n"})
    world["hosts"].tag("web-v1.1.0")
    waiting = _poll(world)["nix_releases"]["web-1"]
    assert waiting["state"] == "approval-required" and waiting["commit"] == commit
    channel.add_request(*approve_request("web-1", waiting["plan_digest"], via="cli"))
    status = _poll(world)
    assert status["nix_releases"]["web-1"]["state"] == "waiting-build"
    (running,) = [item for item in _image_builds(world) if item["stage"] == "building"]
    assert running["host"] == "cluster" and running["images"] == ["web"]
    assert running["actions"]["cancel"] is True

    # A restarted controller looks at the same Job; it ends; main deploys.
    world["controller"] = world["restart"]()
    status = _poll(world)
    assert status["envs"]["main"]["reason"] == "build-waiting"
    assert [item[0] for item in jobs.started] == [name]  # adopted, not started
    jobs.done.add(name)
    status = _poll(world)
    main = status["envs"]["main"]
    assert main["state"] == "deployed", main
    assert jobs.looks[name] >= 6 and jobs.stopped == []
    assert status["controller"]["cluster_builds"] == []


def test_cancel_and_a_superseding_commit_delete_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _jobs_world(tmp_path, monkeypatch)
    controller, channel, jobs = (
        world["controller"],
        world["channel"],
        world["ports"].builder,
    )
    controller.poll_once()
    _poll(world, 21)
    jobs.asynchronous = True
    world["shop"].commit({"web/index.html": "<h1>v2</h1>\n"})
    _poll(world)
    _poll(world, 21)
    ((first, _),) = jobs.started

    # Other inputs: the old Job goes and the new build returns to the cluster
    # at once (the cluster held the build it replaces), with no host_wait.
    world["shop"].commit({"web/index.html": "<h1>v3</h1>\n"})
    status = _poll(world)
    assert jobs.stopped == [first]
    assert [item[0] for item in jobs.started][1:] == [
        status["controller"]["cluster_builds"][0]["job"]
    ]
    second = jobs.started[1][0]

    channel.add_request(*build_cancel_request("main", by="tester"))
    status = _poll(world)
    assert jobs.stopped == [first, second]
    main = status["envs"]["main"]
    assert main["reason"] == "component-build-failed"
    assert main["failure"]["host"] == "cluster"
    (failure,) = world["controller"].state["host_builds_failed"].values()
    assert failure["reason"] == "cancelled by tester" and failure["cancelled"]
    assert status["controller"]["cluster_builds"] == []


# ------------------------------------------------- unchanged inputs (defect 2)


def test_a_commit_that_changes_no_input_keeps_the_running_claim(
    world: dict[str, Any],
) -> None:
    controller, channel = world["controller"], world["channel"]
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    channel.add_request(*build_claim_request(wanted["id"], "builder-1"))
    controller.poll_once()
    commit = world["shop"].commit({"flake.lock": '{"nodes": {}}\n'})  # read by none
    status = _next_try(world)
    (same,) = status["controller"]["builds_wanted"]
    assert same["id"] == wanted["id"]
    assert same["claimed"]["host"] == "builder-1"  # not re-offered, not cancelled
    assert "cancelled" not in str(status["rejected_requests"])
    channel.add_request(
        *built_request(
            wanted["id"], "builder-1", _images(wanted), {"host": "builder-1"}
        )
    )
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "deployed", main
    assert main["revision"]["shop"] == commit  # the result serves the new revision
    assert world["ports"].builder.built == []


def test_a_wait_before_any_claim_hands_the_newest_commits_to_the_host(
    world: dict[str, Any],
) -> None:
    controller = world["controller"]
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    commit = world["shop"].commit({"flake.lock": '{"nodes": {}}\n'})
    (same,) = _next_try(world)["controller"]["builds_wanted"]
    assert same["id"] == wanted["id"]
    assert {
        item["commit"] for item in same["components"] if item["source"] == "shop"
    } == {commit}


# ------------------------------------------ replacements go to the live host (3)


def test_a_replacement_goes_to_the_live_host_not_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, host_wait="20s")
    controller, channel, ports = world["controller"], world["channel"], world["ports"]
    (first,) = controller.poll_once()["controller"]["builds_wanted"]
    channel.add_request(*build_claim_request(first["id"], "builder-1"))
    controller.poll_once()
    world["shop"].commit({"web/index.html": "<h1>v2</h1>\n"})
    (second,) = _next_try(world)["controller"]["builds_wanted"]
    assert second["id"] != first["id"]
    assert second["prefer"] == {"host": "builder-1", "replaces": first["id"]}
    world["clock"]["now"] += 60  # well past host_wait
    status = _next_try(world)
    main = status["envs"]["main"]
    assert main["reason"] == "build-waiting"
    assert "offered first to build host builder-1" in main["waiting_for"]
    assert ports.builder.built == []  # no fallback while the host is alive
    channel.add_request(*build_claim_request(second["id"], "builder-1"))
    channel.add_request(
        *build_progress_request(second["id"], "builder-1", "build", "", "t")
    )
    controller.poll_once()
    channel.add_request(
        *built_request(
            second["id"], "builder-1", _images(second), {"host": "builder-1"}
        )
    )
    assert controller.poll_once()["envs"]["main"]["state"] == "deployed"
    assert ports.builder.built == []


def test_a_replacement_falls_back_when_no_host_is_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, host_wait="20s")
    controller, channel, ports = world["controller"], world["channel"], world["ports"]
    (first,) = controller.poll_once()["controller"]["builds_wanted"]
    channel.add_request(*build_claim_request(first["id"], "builder-1"))
    controller.poll_once()
    world["shop"].commit({"web/index.html": "<h1>v2</h1>\n"})
    _next_try(world)
    world["clock"]["now"] += CLAIM_STALE_SECONDS  # builder-1 went silent
    status = _next_try(world)
    assert ports.builder.built == [["api", "catalog", "web"]]
    assert status["envs"]["main"]["state"] == "deployed"
