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
)
from piceli.infra.composition import load_composition
from piceli.infra.controller import CompositionConfig, CompositionController
from piceli.infra.sources import SourceSet
from tests.unit.infra.git_support import Repo, example_files
from tests.unit.infra.test_controller import EXAMPLE, FakePorts


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
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
            'host_wait="10m")',
        )
    )
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
    return {
        "controller": controller,
        "ports": ports,
        "channel": channel,
        "clock": clock,
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
        line(encode_receipt({"components": {}, "archives": archives}))
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
    assert worker.once() is None  # nothing left to build
    # The Git mirrors and checkouts stay for the next build; nothing else does.
    assert sorted(p.name for p in (tmp_path / "builder").iterdir()) == [
        "checkouts",
        "git",
    ]
