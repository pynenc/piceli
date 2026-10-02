"""The composition controller against local bare Git sources (no cluster).

Builds and deployments are fakes; Git, contracts, digests, triggers and the
status are real.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from piceli.gitops.state import DirectoryChannel, request
from piceli.infra.builders import BuildItem, BuiltImage, MirrorItem
from piceli.infra.composition import load_composition
from piceli.infra.controller import CompositionConfig, CompositionController
from piceli.infra.sources import SourceSet
from tests.unit.infra.git_support import Repo, example_files

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "composition"


class FakeBuilder:
    def __init__(self) -> None:
        self.built: list[list[str]] = []
        self.mirrored: list[str] = []

    def build(self, items: Sequence[BuildItem], checkout: Any) -> dict[str, BuiltImage]:
        self.built.append(sorted(item.component for item in items))
        return {
            item.component: BuiltImage(
                f"registry.example:5000/{item.repository}@{item.digest}",
                item.digest,
            )
            for item in items
        }

    def mirror(self, items: Sequence[MirrorItem]) -> dict[str, BuiltImage]:
        self.mirrored += [item.component for item in items]
        digest = "sha256:" + "c" * 64
        return {
            item.component: BuiltImage(
                f"registry.example:5000/{item.repository}@{digest}", digest
            )
            for item in items
        }


class FakePorts:
    def __init__(self, approve: bool = True) -> None:
        self.builder = FakeBuilder()
        self.deployed: list[tuple[str, dict[str, str]]] = []
        self.removed: list[str] = []
        self.approve = approve

    def pipeline(
        self,
        composition: Any,
        env: Any,
        contracts: Mapping[str, Any],
        images: Mapping[str, str],
    ) -> Any:
        return {"env": env.name, "images": dict(images), "contracts": set(contracts)}

    def prepare_env(self, pipeline: Any, name: str) -> str:
        return f"ns-{name}"

    def env_up(self, pipeline: Any, name: str, **kwargs: Any) -> dict[str, Any]:
        if kwargs["approve"] is None:
            return {"state": "approval-required", "plan_hash": "sha256:" + "d" * 64}
        self.deployed.append((name, pipeline["images"]))
        return {"state": "ready", "namespace": f"ns-{name}"}

    def env_down(self, pipeline: Any, name: str) -> None:
        self.removed.append(name)

    def env_stop(self, pipeline: Any, name: str) -> None:
        pass


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    shop = Repo(tmp_path / "remotes", "shop", example_files(EXAMPLE / "shop"))
    catalog = Repo(tmp_path / "remotes", "catalog", example_files(EXAMPLE / "catalog"))
    catalog.tag("v0.1.0")
    monkeypatch.setenv("COMPOSITION_SHOP_URL", shop.url)
    monkeypatch.setenv("COMPOSITION_CATALOG_URL", catalog.url)
    composition = load_composition(EXAMPLE / "infra.py")
    config = CompositionConfig(composition=composition.to_dict(), poll_seconds=10)
    state = tmp_path / "state"
    sources = SourceSet(composition.sources, state / "sources")
    ports = FakePorts()
    channel = DirectoryChannel(state)
    clock = {"now": 1_000_000.0}
    controller = CompositionController(
        config,
        state_dir=state,
        sources=sources,
        ports=ports,
        channel=channel,
        clock=lambda: clock["now"],
    )
    return {
        "shop": shop,
        "catalog": catalog,
        "controller": controller,
        "ports": ports,
        "channel": channel,
        "clock": clock,
    }


def _components(status: Mapping[str, Any], env: str) -> dict[str, str]:
    return {k: v["state"] for k, v in status["envs"][env]["components"].items()}


def test_main_follows_both_sources_and_rolls_only_what_changed(
    world: dict[str, Any],
) -> None:
    controller, ports = world["controller"], world["ports"]
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "deployed"
    assert "rc" not in status["envs"]  # tags present at the first poll are the baseline
    assert ports.builder.built == [["api", "catalog", "web"]]
    assert ports.builder.mirrored == ["cache"]
    main = status["envs"]["main"]
    assert set(main["revision"]) == {"shop", "catalog"}
    assert main["refs"] == {"catalog": "refs/heads/main", "shop": "refs/heads/main"}
    assert set(_components(status, "main").values()) == {"synced"}
    first = {k: v["digest"] for k, v in main["components"].items()}
    assert (
        status["sources"]["shop"]["refs"]["refs/heads/main"] == main["revision"]["shop"]
    )
    assert status["sources"]["catalog"]["refs"]["refs/tags/v0.1.0"]

    # Nothing moved: no build, no deploy.
    controller.poll_once()
    assert len(ports.deployed) == 1

    # A change in web's files rebuilds and rolls only web.
    world["shop"].commit({"web/index.html": "<h1>shop v2</h1>\n"})
    status = controller.poll_once()
    assert ports.builder.built[-1] == ["web"]
    assert _components(status, "main") == {
        "web": "synced",
        "api": "unchanged",
        "catalog": "unchanged",
        "cache": "unchanged",
    }
    after = {k: v["digest"] for k, v in status["envs"]["main"]["components"].items()}
    assert after["web"] != first["web"]
    assert {k: after[k] for k in ("api", "catalog", "cache")} == {
        k: first[k] for k in ("api", "catalog", "cache")
    }
    # A change outside every build's paths rebuilds nothing.
    world["shop"].commit({"README.md": "docs\n"})
    status = controller.poll_once()
    assert ports.builder.built[-1] == ["web"] and len(ports.builder.built) == 2
    assert set(_components(status, "main").values()) == {"unchanged"}


def test_rc_deploys_on_a_new_tag(world: dict[str, Any]) -> None:
    controller = world["controller"]
    controller.poll_once()
    sha = world["shop"].tag("v1.0.0")
    status = controller.poll_once()
    rc = status["envs"]["rc"]
    assert rc["state"] == "deployed" and rc["trigger"] == "tag shop/v1.0.0"
    assert rc["revision"]["shop"] == sha
    assert rc["refs"] == {"catalog": "refs/tags/v0.1.0", "shop": "refs/tags/v1.0.0"}
    # Main's images were built at the same digests: rc builds nothing new.
    assert len(world["ports"].builder.built) == 1


def test_branch_envs_come_and_go_with_their_branch(world: dict[str, Any]) -> None:
    controller, ports, shop = world["controller"], world["ports"], world["shop"]
    controller.poll_once()
    shop.commit({"web/index.html": "<h1>login</h1>\n"}, branch="wp-login")
    status = controller.poll_once()
    env = status["envs"]["wp-login"]
    assert env["state"] == "deployed"
    assert set(env["components"]) == {"web", "catalog"}  # the small stack
    assert env["refs"] == {"catalog": "refs/heads/main", "shop": "refs/heads/wp-login"}
    assert ports.builder.built[-1] == ["web"]
    shop.commit({"other.txt": "x\n"}, branch="feature")  # not wp-*: no env
    status = controller.poll_once()
    assert "feature" not in status["envs"]
    shop.delete_branch("wp-login")
    status = controller.poll_once()
    assert "wp-login" not in status["envs"] and ports.removed == ["wp-login"]


def test_sync_rebuilds_a_component_on_request(world: dict[str, Any]) -> None:
    controller, ports, channel = world["controller"], world["ports"], world["channel"]
    controller.poll_once()
    key, body = request("sync", env="main", component="api")
    channel.add_request(key, body)
    status = controller.poll_once()
    assert ports.builder.built[-1] == ["api"]
    assert status["envs"]["main"]["trigger"] == "sync api"
    key, body = request("sync", env="nope")
    channel.add_request(key, body)
    status = controller.poll_once()
    assert status["rejected_requests"][-1]["reason"] == "gitops-request-invalid"


def test_an_unmet_need_fails_the_environment(world: dict[str, Any]) -> None:
    controller = world["controller"]
    text = (EXAMPLE / "shop" / "piceli.toml").read_text()
    world["shop"].commit(
        {
            "piceli.toml": text.replace(
                'needs = ["catalog"]', 'needs = ["secret:api-token"]'
            )
        }
    )
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "failed"
    assert status["envs"]["main"]["reason"] == "component-need-unmet"
    world["shop"].commit(
        {"piceli.toml": text.replace("[component.api]", "[component.api]\nport = 1")}
    )
    status = controller.poll_once()
    assert status["envs"]["main"]["reason"] == "component-contract-invalid"


def test_without_auto_approve_the_env_waits_for_the_owner(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, channel = world["controller"], world["channel"]
    from piceli.envs import Environment

    composition = controller.composition
    main = composition.environment("main")
    manual = Environment(
        "main",
        namespace=main.namespace,
        stack=main.stack,
        follow={source: list(items) for source, items in main.sources},
    )
    object.__setattr__(
        composition,
        "environments",
        (manual, *composition.environments[1:]),
    )
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "approval-required"
    plan = status["envs"]["main"]["plan_hash"]
    from piceli.gitops.state import approve_request

    channel.add_request(*approve_request("main", plan))
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "deployed"


def test_status_publishes_promote_policy_and_branch_heads(
    world: dict[str, Any],
) -> None:
    """The UI's Promote picker needs the policy and the heads it may pick."""
    controller = world["controller"]
    from piceli.envs import Environment, Promote

    composition = controller.composition
    rc = composition.environment("rc")
    sources = {source.key: source for source, _ in rc.sources}
    promoted = Environment(
        "rc",
        namespace=rc.namespace,
        stack=rc.stack,
        follow={sources["shop"]: Promote(), sources["catalog"]: "main"},
    )
    object.__setattr__(
        composition,
        "environments",
        tuple(promoted if e.name == "rc" else e for e in composition.environments),
    )
    head = world["shop"].commit({"README.md": "feature\n"}, branch="feature")
    status = controller.poll_once()
    policies = {e["name"]: e["promote"] for e in status["controller"]["environments"]}
    assert policies["rc"] is True and policies["main"] is False
    assert status["sources"]["shop"]["refs"]["refs/heads/feature"] == head
