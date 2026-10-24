"""0.21: GitOps builds on build hosts (``Controller(builds=("hosts", ...))``).

The controller publishes what it waits for and the step waits (no attempt
counted, no history entry); a host (here, the test) claims it and answers
``built``; the controller checks it and deploys the delivered images like
its own. A request it did not ask for, an image built from other inputs and
a host's failure are each handled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from piceli.gitops.state import (
    DirectoryChannel,
    build_claim_request,
    build_failed_request,
    built_request,
    sync_request,
)
from piceli.infra.composition import load_composition
from piceli.infra.controller import CompositionConfig, CompositionController
from piceli.infra.sources import SourceSet
from tests.unit.infra.git_support import Repo, example_files
from tests.unit.infra.test_controller import EXAMPLE, FakePorts


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return _world(tmp_path, monkeypatch)


def _world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_wait: str = "10m",
    poll_seconds: int = 10,
) -> dict[str, Any]:
    shop = Repo(tmp_path / "remotes", "shop", example_files(EXAMPLE / "shop"))
    catalog = Repo(tmp_path / "remotes", "catalog", example_files(EXAMPLE / "catalog"))
    catalog.tag("v0.1.0")
    monkeypatch.setenv("COMPOSITION_SHOP_URL", shop.url)
    monkeypatch.setenv("COMPOSITION_CATALOG_URL", catalog.url)
    text = (EXAMPLE / "infra.py").read_text()
    assert text.count("controller=Controller(on=REGISTRY_NODE)") == 1
    (tmp_path / "infra.py").write_text(
        text.replace(
            "controller=Controller(on=REGISTRY_NODE)",
            'controller=Controller(on=REGISTRY_NODE, builds=("hosts", "cluster"), '
            f'host_wait="{host_wait}")',
        )
    )
    composition = load_composition(tmp_path / "infra.py")
    config = CompositionConfig(
        composition=composition.to_dict(),
        poll_seconds=poll_seconds,
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
    return {
        "controller": controller,
        "ports": ports,
        "channel": channel,
        "clock": clock,
        "shop": shop,
    }


def _images(wanted: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["component"]: {
            "key": item["digest"],
            "pull_ref": f"registry.example:5000/{item['repository']}@sha256:{'d' * 64}",
            "manifest_digest": "sha256:" + "d" * 64,
            "platforms": {"linux/amd64": "sha256:" + "a" * 64,
                          "linux/arm64": "sha256:" + "b" * 64},
        }
        for item in wanted["components"]
    }  # fmt: skip


def test_the_controller_waits_for_a_host_then_deploys_what_it_built(
    world: dict[str, Any],
) -> None:
    controller, ports, channel = world["controller"], world["ports"], world["channel"]
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "retrying" and main["reason"] == "build-waiting"
    assert main["attempts"] == 0  # waiting is not a failed attempt
    assert {e["state"] for e in main["components"].values()} >= {"waiting-build"}
    assert ports.builder.built == []  # no in-cluster build Job
    (wanted,) = status["controller"]["builds_wanted"]
    assert wanted["kind"] == "components" and wanted["claimed"] is None
    assert sorted(wanted["images"]) == ["api", "catalog", "web"]
    assert all(item["commit"] for item in wanted["components"])

    channel.add_request(*build_claim_request(wanted["id"], "laptop"))
    status = controller.poll_once()
    assert status["controller"]["builds_wanted"][0]["claimed"]["host"] == "laptop"

    channel.add_request(
        *built_request(wanted["id"], "laptop", _images(wanted), {"host": "laptop"})
    )
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "deployed", main
    assert status["controller"]["builds_wanted"] == []
    assert main["components"]["web"]["digest"] == "sha256:" + "d" * 64
    assert ports.builder.built == []  # still never built in the cluster


def test_a_built_request_for_other_inputs_is_refused(world: dict[str, Any]) -> None:
    controller, channel = world["controller"], world["channel"]
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    images = _images(wanted)
    images["web"]["key"] = "sha256:" + "0" * 64  # built from other sources
    channel.add_request(*built_request(wanted["id"], "laptop", images, {}))
    status = controller.poll_once()
    assert status["envs"]["main"]["reason"] == "build-waiting"
    (rejected,) = status["rejected_requests"]
    assert rejected["kind"] == "built" and "other inputs" in rejected["message"]
    channel.add_request(*built_request("f" * 20, "laptop", _images(wanted), {}))
    status = controller.poll_once()
    assert "no such wanted build" in status["rejected_requests"][-1]["message"]


def test_a_hosts_failure_fails_the_step_with_its_tail(world: dict[str, Any]) -> None:
    controller, channel = world["controller"], world["channel"]
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    channel.add_request(
        *build_failed_request(wanted["id"], "laptop", "cargo failed", "error[E0308]")
    )
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["reason"] == "component-build-failed" and main["attempts"] == 1
    assert "error[E0308]" in str(main["failure"])


def test_no_host_in_time_falls_back_to_the_cluster(world: dict[str, Any]) -> None:
    controller, ports, clock = world["controller"], world["ports"], world["clock"]
    controller.poll_once()
    clock["now"] += 11 * 60  # past host_wait, nobody claimed it
    status = controller.poll_once()
    assert ports.builder.built == [["api", "catalog", "web"]]
    assert status["envs"]["main"]["state"] == "deployed"
    assert status["controller"]["builds_wanted"] == []


def test_builder_serve_builds_delivers_and_the_controller_deploys(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """The whole round: published, claimed, sources exported with this
    host's Git, built in the (fake) container, pushed with an index per
    image, answered, deployed."""
    from piceli.artifacts.cluster_build import encode_receipt
    from piceli.infra.builder_serve import BuilderServe

    controller, channel = world["controller"], world["channel"]
    controller.poll_once()
    seen_sources: list[list[str]] = []
    pushed: list[tuple[str, str]] = []
    indexed: list[tuple[str, dict[str, str]]] = []

    def container(
        argv: list[str], sources: dict[str, Path], out: Path, line: Any
    ) -> int:
        import subprocess

        seen_sources.append(sorted(sources))
        for checkout in sources.values():
            # A Git work tree at the wanted commit, so the build can verify
            # it: its .git, a clean tree, the source's URL as origin.
            def git(*args: str, at: Path = checkout) -> str:
                return subprocess.run(
                    ["git", "-C", str(at), *args],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()  # fmt: skip

            assert git("rev-parse", "--show-toplevel") == str(checkout.resolve())
            assert git("status", "--porcelain") == ""
            assert git("config", "--get", "remote.origin.url").endswith(".git")
        assert "--archives-only" in argv and argv.count("--platform") == 2
        archives = []
        for spec in (a for a in argv if a.startswith("{")):
            import json

            item = json.loads(spec)
            for platform in ("linux/amd64", "linux/arm64"):
                archives.append(
                    {
                        "archive": f"{platform.replace('/', '-')}/{item['component']}.oci.tar",
                        "platform": platform,
                        "repository": item["repository"],
                        "image_id": "sha256:" + "1" * 64,
                        "digest": "sha256:" + ("a" if "amd" in platform else "b") * 64,
                    }
                )
        line("[build] compiling")
        line(
            encode_receipt(
                {
                    "components": {},
                    "archives": archives,
                    "toolchains": {"rust": "1.97.0"},
                }
            )
        )
        return 0

    def push(image_id: str, repository: str, archive: Path) -> str:
        pushed.append((repository, archive.name))
        digest = "sha256:" + ("a" if "amd64" in str(archive) else "b") * 64
        return f"registry.example:5000/{repository}@{digest}"

    def index(repository: str, digests: dict[str, str]) -> str:
        indexed.append((repository, dict(digests)))
        return "sha256:" + "9" * 64

    worker = BuilderServe(
        host="laptop",
        image="ghcr.io/x/builder@sha256:" + "e" * 64,
        channel=channel,
        container=container,
        push=push,
        index=index,
        root=tmp_path / "builder",
        sleep=lambda _s: controller.poll_once(),
    )
    built = worker.once()
    assert built is not None
    assert seen_sources == [["catalog", "shop"]]
    assert len(pushed) == 6 and len(indexed) == 3
    assert indexed[0][1] == {"linux/amd64": "sha256:" + "a" * 64,
                             "linux/arm64": "sha256:" + "b" * 64}  # fmt: skip
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "deployed", main
    assert main["components"]["web"]["digest"] == "sha256:" + "9" * 64
    # Where it was built, with the toolchain the build reported (0.22.0).
    built_where = main["components"]["web"]["provenance"]
    assert built_where["host"] == "laptop"
    assert built_where["toolchains"] == {"rust": "1.97.0"}
    assert worker.once() is None  # nothing left to build
    # The Git mirrors and checkouts stay for the next build; nothing else does.
    assert sorted(p.name for p in (tmp_path / "builder").iterdir()) == [
        "checkouts",
        "git",
    ]


# ---------------------------------------------------- the queue (0.22.0)


def _next_try(world: dict[str, Any]) -> dict[str, Any]:
    controller = world["controller"]
    record = controller.state["envs"]["main"]
    world["clock"]["now"] = (
        max(world["clock"]["now"], float(record.get("next_attempt_at") or 0)) + 1
    )
    return controller.poll_once()


def test_a_newer_commit_replaces_the_build_it_superseded(world: dict[str, Any]) -> None:
    controller = world["controller"]
    (first,) = controller.poll_once()["controller"]["builds_wanted"]
    world["shop"].commit({"web/index.html": "<h1>v2</h1>\n"})
    (current,) = _next_try(world)["controller"]["builds_wanted"]
    assert current["id"] != first["id"]
    assert current["owner"] == "main"


def test_leftovers_no_step_asks_for_are_dropped(world: dict[str, Any]) -> None:
    """An older controller left entries with no owner; the current one stays."""
    controller = world["controller"]
    controller.poll_once()
    wanted = controller.state["host_builds"]
    wanted["old0"] = {
        "id": "old0", "kind": "components", "images": ["web"],
        "requested_epoch": world["clock"]["now"] - 3600, "claimed": {"host": "mini"},
    }  # fmt: skip
    status = _next_try(world)
    assert [item["id"] for item in status["controller"]["builds_wanted"]] == [
        key for key in wanted if key != "old0"
    ]
    assert "old0" not in controller.state["host_builds"]


def test_a_failed_build_is_shown_and_not_offered_again_until_a_sync(
    world: dict[str, Any],
) -> None:
    from piceli.infra import remote_builds

    controller, channel = world["controller"], world["channel"]
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    # Shown with its reason while listed (hosts skip it).
    controller.state["host_builds"][wanted["id"]]["failed"] = {
        "host": "mini", "reason": "the build exited 101", "log_tail": "boom",
    }  # fmt: skip
    (shown,) = remote_builds.public(controller.state, world["clock"]["now"])
    assert shown["failed"]["reason"] == "the build exited 101"
    del controller.state["host_builds"][wanted["id"]]["failed"]
    channel.add_request(
        *build_failed_request(wanted["id"], "mini", "the build exited 101", "boom")
    )
    status = controller.poll_once()
    assert status["envs"]["main"]["reason"] == "component-build-failed"
    for _ in range(3):  # automatic retries: failed at once, never offered
        status = _next_try(world)
        assert status["controller"]["builds_wanted"] == []
        assert status["envs"]["main"]["reason"] == "component-build-failed"
    channel.add_request(*sync_request("main"))
    (again,) = _next_try(world)["controller"]["builds_wanted"]
    assert again["id"] == wanted["id"]


def test_a_delivered_build_is_not_built_again(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """The status still lists it until the controller's next poll."""
    from piceli.infra.builder_serve import BuilderServe

    controller, channel = world["controller"], world["channel"]
    controller.poll_once()
    builds: list[str] = []

    class _Serve(BuilderServe):
        def build(self, entry: Any, tail: Any) -> dict[str, Any]:
            builds.append(str(entry["id"]))
            return _images(entry)

    worker = _Serve(
        host="mini",
        image="ghcr.io/x/builder@sha256:" + "e" * 64,
        channel=channel,
        container=lambda *_a: 0,
        push=lambda *_a: "",
        index=lambda *_a: "",
        root=tmp_path / "builder",
        sleep=lambda _s: controller.poll_once(),
    )
    built = worker.once()
    assert built is not None and builds == [built]
    assert worker.once() is None  # still listed, already answered
    assert builds == [built]


def test_a_short_host_wait_is_the_real_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``host_wait="20s"`` with a 60 s poll: the in-cluster build starts about
    20 s after the request, not two polls (121-122 s) later (0.23.0)."""
    world = _world(tmp_path, monkeypatch, host_wait="20s", poll_seconds=60)
    controller, ports, clock = world["controller"], world["ports"], world["clock"]
    asked = clock["now"]
    status = controller.poll_once()
    assert status["envs"]["main"]["reason"] == "build-waiting"
    due = controller.next_due()
    assert due is not None and asked + 20 <= due <= asked + 22
    clock["now"] = due  # the run loop wakes here, not at the next poll
    controller.poll_once()
    assert ports.builder.built, "the fallback did not build at host_wait"
    assert controller.state["envs"]["main"]["state"] == "deployed"


def test_a_newer_commit_mid_build_is_what_gets_deployed(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """The shape a real user hit (0.23.0): a build host builds commit A, a
    newer commit B arrives while it builds; A's late answer is refused, B is
    built once and deployed, and nothing is left in the queue. The real
    controller, the real builder serve, real Git repositories; only the
    container and the registry are fakes."""
    import json

    from piceli.artifacts.cluster_build import encode_receipt
    from piceli.infra.builder_serve import BuilderServe

    controller, channel, clock = world["controller"], world["channel"], world["clock"]
    controller.poll_once()
    (first,) = controller.state["host_builds"].values()
    builds: list[str] = []

    def container(argv: list[str], sources: Any, out: Path, line: Any) -> int:
        builds.append(argv[-1] if argv else "")
        if len(builds) == 1:  # the newer commit lands while A builds
            world["shop"].commit({"web/index.html": "<h1>v2</h1>\n"})
            record = controller.state["envs"]["main"]
            clock["now"] = max(clock["now"], float(record["next_attempt_at"])) + 1
            controller.poll_once()
        archives = [
            {
                "archive": f"{platform.replace('/', '-')}/{item['component']}.oci.tar",
                "platform": platform,
                "repository": item["repository"],
                "image_id": "sha256:" + "1" * 64,
                "digest": "sha256:" + ("a" if "amd" in platform else "b") * 64,
            }
            for item in (json.loads(a) for a in argv if a.startswith("{"))
            for platform in ("linux/amd64", "linux/arm64")
        ]
        line(encode_receipt({"components": {}, "archives": archives}))
        return 0

    worker = BuilderServe(
        host="laptop",
        image="ghcr.io/x/builder@sha256:" + "e" * 64,
        channel=channel,
        container=container,
        push=lambda _i, repository, archive: (
            f"registry.example:5000/{repository}@sha256:"
            + ("a" if "amd64" in str(archive) else "b") * 64
        ),
        # Each build's index digest names the build: 1 for A, 2 for B.
        index=lambda _r, _d: "sha256:" + str(len(builds)) * 64,
        root=tmp_path / "builder",
        sleep=lambda _s: controller.poll_once(),
    )
    # 0.26.0: B superseded A mid-build; the host stops A before its push.
    assert worker.once() == first["id"]
    status = controller.poll_once()
    assert not [r for r in status["rejected_requests"] if r["kind"] == "built"]
    (second,) = status["controller"]["builds_wanted"]
    assert second["id"] != first["id"]
    assert worker.once() == second["id"]  # B, once
    assert worker.once() is None
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "deployed", main
    assert main["components"]["web"]["digest"] == "sha256:" + "2" * 64
    assert status["controller"]["builds_wanted"] == []
    assert len(builds) == 2


# ------------------------------------- dead claims and what a wait waits for (0.27.1)


def test_a_dead_hosts_claim_expires_for_the_fallback_and_other_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host that claimed and then stopped reporting no longer blocks."""
    from piceli.infra.remote_builds import CLAIM_STALE_SECONDS

    world = _world(tmp_path, monkeypatch, host_wait="20s", poll_seconds=60)
    controller, ports, channel = world["controller"], world["ports"], world["channel"]
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    channel.add_request(*build_claim_request(wanted["id"], "builder-2"))
    status = _next_try(world)
    main = status["envs"]["main"]
    assert "claimed by builder-2 at" in main["waiting_for"], main
    assert ports.builder.built == []  # a live claim holds the fallback
    world["clock"]["now"] += CLAIM_STALE_SECONDS - 120
    channel.add_request(*build_claim_request(wanted["id"], "builder-1"))
    status = controller.poll_once()
    assert "builder-2 already builds it" in status["rejected_requests"][-1]["message"]
    world["clock"]["now"] += 121  # no report from builder-2 for 10 min
    status = _next_try(world)
    assert ports.builder.built == [["api", "catalog", "web"]]
    assert status["envs"]["main"]["state"] == "deployed"


def test_progress_reports_keep_a_claim_alive(world: dict[str, Any]) -> None:
    from piceli.gitops.state import build_progress_request
    from piceli.infra.remote_builds import CLAIM_STALE_SECONDS, public

    controller, channel, clock = world["controller"], world["channel"], world["clock"]
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    channel.add_request(*build_claim_request(wanted["id"], "builder-2"))
    controller.poll_once()
    for _ in range(3):
        clock["now"] += CLAIM_STALE_SECONDS - 60
        channel.add_request(
            *build_progress_request(wanted["id"], "builder-2", "build", "", "t")
        )
        controller.poll_once()
    (shown,) = public(controller.state, clock["now"])
    assert shown["claimed"]["host"] == "builder-2"
    clock["now"] += CLAIM_STALE_SECONDS
    (shown,) = public(controller.state, clock["now"])
    assert shown["claimed"] is None and shown["claim_expired"]["host"] == "builder-2"


def test_the_waiting_status_says_the_fallback_time_or_why_there_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, host_wait="20s", poll_seconds=60)
    controller = world["controller"]
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["waiting_for"] == (
        "waiting for a build host (docker) to build api, catalog, web: offered "
        "to any build host; not claimed; in-cluster fallback at 1970-01-12T13:47:00Z"
    )
    controller.ports.builder.builds_images = False  # no --builder-image
    world["clock"]["now"] += 30
    main = controller.poll_once()["envs"]["main"]
    assert main["reason"] == "build-waiting"
    assert main["waiting_for"].endswith(
        "no in-cluster fallback: no in-cluster builder image "
        "(piceli gitops enable --builder-image)"
    )
    assert controller.ports.builder.built == []


def test_builder_serve_says_once_why_it_skips_a_build(
    world: dict[str, Any], tmp_path: Path
) -> None:
    from piceli.infra.builder_serve import BuilderServe

    controller, channel = world["controller"], world["channel"]
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    channel.add_request(*build_claim_request(wanted["id"], "builder-2"))
    controller.poll_once()
    lines: list[str] = []
    worker = BuilderServe(
        host="builder-1",
        image="ghcr.io/x/builder@sha256:" + "e" * 64,
        channel=channel,
        container=lambda *_a: 0,
        push=lambda *_a: "",
        index=lambda *_a: "",
        root=tmp_path / "builder",
        say=lines.append,
        sleep=lambda _s: controller.poll_once(),
    )
    assert worker.once() is None and worker.once() is None
    assert lines == [
        f"builder: not building {wanted['id']}: claimed by build host builder-2 "
        f"at {controller.state['host_builds'][wanted['id']]['claimed']['at']}"
    ]
    # A claim the controller refuses is said with its reason.
    entry = dict(wanted, claimed=None)
    assert worker.claim(entry) is False
    assert "claim refused: build host builder-2 already builds it" in lines[-1]


def test_claims_and_reports_carry_the_hosts_piceli_version() -> None:
    from piceli import __version__
    from piceli.gitops.state import build_progress_request

    assert build_claim_request("a" * 20, "builder-1")[1]["version"] == __version__
    body = build_progress_request("a" * 20, "builder-1", "build", "", "t")[1]
    assert body["version"] == __version__


def test_the_controller_publishes_each_build_hosts_last_sighting(
    world: dict[str, Any],
) -> None:
    """0.28.0: ``controller.build_hosts``: version, last seen, last build."""
    from piceli.gitops.state import build_progress_request

    controller, channel, clock = world["controller"], world["channel"], world["clock"]
    status = controller.poll_once()
    assert "build_hosts" not in status["controller"]  # only once a host showed up
    (wanted,) = status["controller"]["builds_wanted"]
    channel.add_request(*build_claim_request(wanted["id"], "builder-1"))
    status = controller.poll_once()
    seen = status["controller"]["build_hosts"]["builder-1"]
    assert seen["version"] and seen["seen"] == "build-claim"
    assert seen["last_build"]["id"] == wanted["id"]
    assert seen["last_build"]["outcome"] == "claimed"
    first = seen["seen_at"]

    clock["now"] += 60
    channel.add_request(
        *build_progress_request(wanted["id"], "builder-1", "build", "", "t")
    )
    channel.add_request(*build_claim_request(wanted["id"], "builder-2"))
    status = controller.poll_once()
    hosts = status["controller"]["build_hosts"]
    assert hosts["builder-1"]["seen_at"] > first
    assert hosts["builder-1"]["seen"] == "build-progress"
    # A refused claim is a sighting, never a build of its own.
    assert hosts["builder-2"]["seen"] == "build-claim"
    assert hosts["builder-2"].get("last_build") is None

    channel.add_request(
        *built_request(wanted["id"], "builder-1", _images(wanted), {"host": "x"})
    )
    status = controller.poll_once()
    last = status["controller"]["build_hosts"]["builder-1"]["last_build"]
    assert last["outcome"] == "built" and last["id"] == wanted["id"]
