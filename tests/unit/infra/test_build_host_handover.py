"""0.29.0: build hosts the composition no longer declares, hand-overs after a
failure before the build's first command, ``gitops build`` at a deployed
revision and hosts standing by for the next build (``--take-next``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from piceli.gitops.state import (
    DirectoryChannel,
    build_claim_request,
    build_failed_request,
    build_request,
    build_standby_request,
    built_request,
    host_release_request,
)
from piceli.infra.composition import load_composition
from piceli.infra.controller import CompositionConfig, CompositionController
from piceli.infra.sources import SourceSet
from tests.unit.infra.git_support import Repo, example_files
from tests.unit.infra.test_controller import EXAMPLE, FakePorts
from tests.unit.infra.test_remote_builds import _images

IMAGE = "ghcr.io/x/builder@sha256:" + "e" * 64


def _world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    builds: str = '("hosts", "cluster")',
    host_wait: str = "10m",
    hosts: tuple[str, ...] = ("builder-1", "builder-2"),
) -> dict[str, Any]:
    shop = Repo(tmp_path / "remotes", "shop", example_files(EXAMPLE / "shop"))
    catalog = Repo(tmp_path / "remotes", "catalog", example_files(EXAMPLE / "catalog"))
    catalog.tag("v0.1.0")
    monkeypatch.setenv("COMPOSITION_SHOP_URL", shop.url)
    monkeypatch.setenv("COMPOSITION_CATALOG_URL", catalog.url)
    declared = ", ".join(f'BuildHost.docker("{name}")' for name in hosts)
    text = (EXAMPLE / "infra.py").read_text()
    dev = (
        f',\n    dev=DevBuilds(node=None, image="{IMAGE}", hosts=[{declared}])'
        if hosts
        else ""
    )
    text = text.replace(
        "controller=Controller(on=REGISTRY_NODE)",
        f"controller=Controller(on=REGISTRY_NODE, builds={builds}, "
        f'host_wait="{host_wait}"){dev}',
    ).replace(
        "from piceli.infra import Cluster,",
        "from piceli.infra import BuildHost, DevBuilds\nfrom piceli.infra import Cluster,",
    )
    (tmp_path / "infra.py").write_text(text)
    composition = load_composition(tmp_path / "infra.py")
    config = CompositionConfig(
        composition=composition.to_dict(),
        poll_seconds=10,
        platforms=("linux/amd64", "linux/arm64"),
    )
    state = tmp_path / "state"
    ports = FakePorts()
    channel = DirectoryChannel(state)
    clock = {"now": 1_000_000.0}
    controller = CompositionController(
        config,
        state_dir=state,
        sources=SourceSet(composition.sources, state / "sources"),
        ports=ports,
        channel=channel,
        clock=lambda: clock["now"],
    )
    # As in repo mode, the imported composition (plain data drops DevBuilds).
    controller._composition = composition
    return {"controller": controller, "ports": ports, "channel": channel,
            "clock": clock, "shop": shop}  # fmt: skip


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return _world(tmp_path, monkeypatch)


def _serve(world: dict[str, Any], tmp_path: Path, host: str, **kw: Any) -> Any:
    from piceli.infra.builder_serve import BuilderServe

    said: list[str] = []
    worker = BuilderServe(
        host=host,
        image=IMAGE,
        channel=world["channel"],
        container=lambda *_a: 0,
        push=lambda *_a: "",
        index=lambda *_a: "",
        root=tmp_path / f"builder-{host}",
        say=said.append,
        sleep=lambda _s: world["controller"].poll_once(),
        clock=lambda: world["clock"]["now"],
        **kw,
    )
    worker.said = said  # type: ignore[attr-defined]
    return worker


# ------------------------------------------------- 1. undeclared build hosts


def test_a_host_the_composition_does_not_declare_is_refused_and_told(
    world: dict[str, Any], tmp_path: Path
) -> None:
    controller, channel = world["controller"], world["channel"]
    status = controller.poll_once()
    assert status["controller"]["build_hosts_declared"] == ["builder-1", "builder-2"]
    (wanted,) = status["controller"]["builds_wanted"]
    for request in (
        build_claim_request(wanted["id"], "old-host"),
        build_failed_request(wanted["id"], "old-host", "boom", ""),
        built_request(wanted["id"], "old-host", _images(wanted), {}),
    ):
        channel.add_request(*request)
    status = controller.poll_once()
    rejected = status["rejected_requests"]
    assert [item["kind"] for item in rejected] == [
        "build-claim",
        "build-failed",
        "built",
    ]
    assert all("not declared in the composition" in r["message"] for r in rejected)
    (still,) = status["controller"]["builds_wanted"]
    assert still["claimed"] is None and not still.get("failed")
    assert status["envs"]["main"]["reason"] == "build-waiting"

    # Its builder serve claims nothing and says why, once.
    worker = _serve(world, tmp_path, "old-host")
    assert worker.once() is None and worker.once() is None
    told = [line for line in worker.said if "does not declare build host" in line]
    assert len(told) == 1 and "builder uninstall --host old-host" in told[0]
    assert not [b for b in channel.requests().values() if b["kind"] == "build-claim"]

    # A declared host still claims it.
    channel.add_request(*build_claim_request(wanted["id"], "builder-1"))
    status = controller.poll_once()
    assert status["controller"]["builds_wanted"][0]["claimed"]["host"] == "builder-1"


def test_without_declared_hosts_any_host_may_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, hosts=())
    controller, channel = world["controller"], world["channel"]
    status = controller.poll_once()
    assert "build_hosts_declared" not in status["controller"]
    (wanted,) = status["controller"]["builds_wanted"]
    channel.add_request(*build_claim_request(wanted["id"], "laptop"))
    status = controller.poll_once()
    assert status["controller"]["builds_wanted"][0]["claimed"]["host"] == "laptop"


# ------------------------------------------------------------ 2. hand-over


def _fails_to_fetch(world: dict[str, Any], tmp_path: Path, host: str) -> Any:
    from piceli.infra.builder_serve import BuildFailed

    class _Serve(type(_serve(world, tmp_path, host))):  # type: ignore[misc]
        def build(self, entry: Any, tail: Any) -> dict[str, Any]:
            raise BuildFailed(
                "commit abc of https://example.com/shop.git cannot be fetched",
                early=True,
                source="shop",
            )

    worker = _serve(world, tmp_path, host)
    worker.__class__ = _Serve
    return worker


def test_a_fetch_failure_hands_the_build_to_the_next_host_and_quarantines(
    world: dict[str, Any], tmp_path: Path
) -> None:
    controller, channel, clock = world["controller"], world["channel"], world["clock"]
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    worker = _fails_to_fetch(world, tmp_path, "builder-1")
    assert worker.once() == wanted["id"]
    (body,) = [b for b in channel.requests().values() if b["kind"] == "build-failed"]
    assert body["early"] is True and body["source"] == "shop"
    status = controller.poll_once()
    main = status["envs"]["main"]
    # Not failed: still waiting, handed over, no attempt counted.
    assert main["reason"] == "build-waiting" and main["attempts"] == 0
    assert "handed over by builder-1" in str(main)
    (entry,) = status["controller"]["builds_wanted"]
    assert entry["claimed"] is None and entry["handed_over"][0]["host"] == "builder-1"
    held = status["controller"]["source_quarantine"]["builder-1"]["shop"]
    assert "cannot be fetched" in held["reason"]

    # builder-1 may not claim it again, and says why.
    channel.add_request(*build_claim_request(wanted["id"], "builder-1"))
    status = controller.poll_once()
    assert "cannot fetch source shop" in status["rejected_requests"][-1]["message"]
    again = _serve(world, tmp_path, "builder-1")
    assert again.once() is None
    assert any("could not fetch source shop" in line for line in again.said)

    # builder-2 may: it claims it before the fallback.
    channel.add_request(*build_claim_request(wanted["id"], "builder-2"))
    status = controller.poll_once()
    assert status["controller"]["builds_wanted"][0]["claimed"]["host"] == "builder-2"
    assert world["ports"].builder.built == []

    # release-host lifts the quarantine.
    channel.add_request(*host_release_request("builder-1"))
    status = controller.poll_once()
    assert "source_quarantine" not in status["controller"]
    clock["now"] += 1


def test_the_fallback_takes_a_handed_over_build_at_once_when_no_host_is_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, hosts=("builder-1",))
    controller, ports = world["controller"], world["ports"]
    controller.poll_once()
    worker = _fails_to_fetch(world, tmp_path, "builder-1")
    assert worker.once() is not None
    status = controller.poll_once()  # host_wait (10 min) has not passed
    assert ports.builder.built == [["api", "catalog", "web"]]
    assert status["envs"]["main"]["state"] == "deployed"


def test_another_declared_host_gets_a_short_window_before_the_fallback(
    world: dict[str, Any], tmp_path: Path
) -> None:
    from piceli.infra.remote_builds import HANDOVER_SECONDS

    controller, ports, clock = world["controller"], world["ports"], world["clock"]
    controller.poll_once()
    assert _fails_to_fetch(world, tmp_path, "builder-1").once() is not None
    controller.poll_once()
    assert ports.builder.built == []  # builder-2 may still take it
    due = controller.next_due()
    assert due is not None and due <= clock["now"] + HANDOVER_SECONDS + 1
    clock["now"] += HANDOVER_SECONDS + 1
    controller.poll_once()
    assert ports.builder.built == [["api", "catalog", "web"]]


def test_hosts_only_with_no_host_left_fails_with_the_hosts_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, builds='("hosts",)', hosts=("builder-1",))
    controller = world["controller"]
    controller.poll_once()
    assert _fails_to_fetch(world, tmp_path, "builder-1").once() is not None
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["reason"] == "component-build-failed"
    assert "cannot be fetched" in str(main)


def test_a_failure_after_the_first_command_still_fails_the_step(
    world: dict[str, Any],
) -> None:
    controller, channel = world["controller"], world["channel"]
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    channel.add_request(*build_claim_request(wanted["id"], "builder-1"))
    controller.poll_once()
    channel.add_request(
        *build_failed_request(wanted["id"], "builder-1", "the build exited 101", "E")
    )
    status = controller.poll_once()
    assert status["envs"]["main"]["reason"] == "component-build-failed"
    assert "source_quarantine" not in status["controller"]


def test_builder_serve_marks_fetch_and_engine_failures_early(
    world: dict[str, Any], tmp_path: Path
) -> None:
    from collections import deque

    from piceli.infra import builder_serve
    from piceli.infra.builder_serve import ENGINE_FAILED, BuildFailed

    world["controller"].poll_once()
    (entry,) = world["controller"].status()["controller"]["builds_wanted"]
    worker = _serve(world, tmp_path, "builder-1")

    def cannot_fetch(*_a: Any, **_k: Any) -> None:
        raise BuildFailed("commit x of y cannot be fetched from this host")

    original = builder_serve.export_source
    builder_serve.export_source = cannot_fetch  # type: ignore[assignment]
    try:
        with pytest.raises(BuildFailed) as caught:
            worker.build(entry, deque())
    finally:
        builder_serve.export_source = original  # type: ignore[assignment]
    assert caught.value.early and caught.value.source in {"catalog", "shop"}

    worker.git = lambda *_a, **_k: None  # never reached: export is faked
    builder_serve.export_source = lambda *_a, **_k: None  # type: ignore[assignment]
    try:
        worker.container = lambda *_a: ENGINE_FAILED
        with pytest.raises(BuildFailed) as caught:
            worker.build(entry, deque())
        assert caught.value.early and caught.value.source is None
        worker.container = lambda *_a: 101
        with pytest.raises(BuildFailed) as caught:
            worker.build(entry, deque())
        assert not caught.value.early
    finally:
        builder_serve.export_source = original  # type: ignore[assignment]


# ------------------------------------------- 3. gitops build when deployed


def test_gitops_build_of_a_deployed_revision_offers_hosts_a_real_build(
    world: dict[str, Any],
) -> None:
    controller, channel = world["controller"], world["channel"]
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    channel.add_request(*built_request(wanted["id"], "builder-1", _images(wanted), {}))
    assert controller.poll_once()["envs"]["main"]["state"] == "deployed"

    channel.add_request(*build_request("main", by="ana"))
    status = controller.poll_once()
    (again,) = status["controller"]["builds_wanted"]  # before: none
    assert again["id"] == wanted["id"]  # the same inputs, built again
    assert status["envs"]["main"]["reason"] == "build-waiting"

    channel.add_request(*built_request(again["id"], "builder-2", _images(again), {}))
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "deployed"
    for _ in range(3):  # delivered: never asked for again
        world["clock"]["now"] += 60
        status = controller.poll_once()
        assert status["controller"]["builds_wanted"] == []
    assert not controller.state.get("rebuild")


# ------------------------------------------------ 4. --once --take-next


def test_a_standby_holds_the_fallback_until_the_host_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.infra.remote_builds import STANDBY_SECONDS

    world = _world(tmp_path, monkeypatch, host_wait="20s")
    controller, channel, ports = world["controller"], world["channel"], world["ports"]
    clock = world["clock"]
    channel.add_request(*build_standby_request("builder-1", STANDBY_SECONDS))
    status = controller.poll_once()
    assert "builder-1" in status["controller"]["build_standby"]
    clock["now"] += 30  # past host_wait: the standby holds the fallback
    status = controller.poll_once()
    assert ports.builder.built == []
    assert "standing by: build host builder-1" in str(status["envs"]["main"])
    clock["now"] += STANDBY_SECONDS  # the standby lapsed: the fallback builds
    controller.poll_once()
    assert ports.builder.built == [["api", "catalog", "web"]]


def test_take_next_claims_the_next_build_before_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, host_wait="20s")
    controller, channel, ports = world["controller"], world["channel"], world["clock"]
    clock = world["clock"]
    built: list[str] = []
    polls = {"n": 0}

    def sleep(seconds: float) -> None:
        clock["now"] += seconds
        polls["n"] += 1
        if polls["n"] == 3:
            world["shop"].commit({"web/index.html": "<h1>v2</h1>\n"})
        controller.poll_once()

    worker = _serve(world, tmp_path, "builder-1")
    worker.sleep = sleep
    worker.build = lambda entry, tail: (built.append(entry["id"]), _images(entry))[1]
    found = worker.take_next(timeout=600, poll=2)
    assert found is not None and built == [found]
    assert world["ports"].builder.built == []  # the fallback never ran
    assert any("stands by for the next build" in line for line in worker.said)
    # The standby ended when it returned.
    ended = [b for b in channel.requests().values() if b["kind"] == "build-standby"]
    assert ended and ended[-1]["seconds"] == 0
    controller.poll_once()
    assert "build_standby" not in controller.status()["controller"]
    del ports


def test_take_next_gives_up_after_its_timeout(
    world: dict[str, Any], tmp_path: Path
) -> None:
    world["controller"].poll_once()
    clock = world["clock"]
    worker = _serve(world, tmp_path, "builder-9")  # undeclared: never claims

    def sleep(seconds: float) -> None:
        clock["now"] += seconds

    worker.sleep = sleep
    assert worker.take_next(timeout=10, poll=2) is None
    assert any("no build to take in 10s" in line for line in worker.said)
