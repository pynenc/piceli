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


def test_a_stale_approval_after_a_partial_apply_asks_for_a_new_one(
    world: dict[str, Any],
) -> None:
    from piceli.envs import Environment
    from piceli.envs.model import EnvError
    from piceli.gitops.state import approve_request
    from piceli.pipeline.errors import PipelineError

    controller, channel, ports = world["controller"], world["channel"], world["ports"]
    composition = controller.composition
    main = composition.environment("main")
    manual = Environment(
        "main",
        namespace=main.namespace,
        stack=main.stack,
        follow={source: list(items) for source, items in main.sources},
    )
    object.__setattr__(
        composition, "environments", (manual, *composition.environments[1:])
    )
    first, second = "sha256:" + "d" * 64, "sha256:" + "e" * 64
    calls: list[str | None] = []

    def env_up(pipeline: Any, name: str, **kwargs: Any) -> dict[str, Any]:
        approve = kwargs["approve"]
        calls.append(approve)
        current = second if len(calls) > 2 else first
        if approve is None:
            return {"state": "approval-required", "plan_hash": current}
        if approve != current:
            raise EnvError("env-plan-changed", "the approved hash is stale")
        if len(calls) == 2:
            raise PipelineError("pipeline-apply-not-ready", "half", failed=True)
        return {"state": "ready", "namespace": "ns-main"}

    ports.env_up = env_up
    status = controller.poll_once()
    assert status["envs"]["main"]["plan_hash"] == first
    channel.add_request(*approve_request("main", first))
    status = controller.poll_once()  # half applied
    assert status["envs"]["main"]["state"] == "retrying"
    world["clock"]["now"] += 10_000
    status = controller.poll_once()
    env = status["envs"]["main"]
    assert env["state"] == "approval-required", env
    assert env["plan_hash"] == second and calls[-1] is None
    channel.add_request(*approve_request("main", second))
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "deployed"


def test_a_failed_component_build_shows_its_log_tail(world: dict[str, Any]) -> None:
    from piceli.infra import CompositionError

    controller, ports = world["controller"], world["ports"]

    def build(items: Any, checkout: Any) -> Any:
        raise CompositionError(
            "component-build-failed",
            "the component build Job ended failed",
            details={
                "outcome": {"state": "failed", "log_tail": "  | error: x"},
                "kept_job": "piceli-component-build-1",
            },
        )

    ports.builder.build = build
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["reason"] == "component-build-failed"
    assert main["failure"] == {
        "log_tail": "  | error: x",
        "kept_job": "piceli-component-build-1",
    }


def _verification_up(
    calls: list[str], fail: bool = False
) -> Any:  # an env_up whose release did not change
    from piceli.pipeline.errors import PipelineError

    def env_up(pipeline: Any, name: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(name)
        verification = {"trigger": "checks-changed", "applied": False}
        if fail:
            raise PipelineError(
                "pipeline-checks-failed",
                "release main failed its checks",
                failed=True,
                details={
                    "run_state": "failed",
                    "output": {
                        "passed": False,
                        "checks_hash": "sha256:" + "f" * 64,
                        "verification": verification,
                        "results": [
                            {"name": "http-web", "passed": True, "detail": "200"},
                            {
                                "name": "http-login",
                                "passed": False,
                                "detail": "GET /login returned 500",
                                "code": "check-failed",
                            },
                        ],
                    },
                },
            )
        return {
            "state": "ready",
            "namespace": f"ns-{name}",
            "result": {
                "state": "ready",
                "stages": {"plan": "done", "apply": "skipped", "checks": "done"},
                "verification": {
                    **verification,
                    "checks_hash": "sha256:" + "e" * 64,
                    "passed": True,
                },
            },
        }

    return env_up


def test_a_changed_check_set_is_a_verification_and_a_failure_degrades(
    world: dict[str, Any],
) -> None:
    from piceli.gitops.state import sync_request

    controller, ports, channel = world["controller"], world["ports"], world["channel"]
    lines: list[str] = []
    controller.log = lines.append
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["last_action"] == "deployed" and main["health"] == "healthy"

    # Only the checks changed: the release is verified, nothing rolls.
    calls: list[str] = []
    ports.env_up = _verification_up(calls)
    channel.add_request(*sync_request("main"))
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "deployed" and main["last_action"] == "verified"
    assert main["health"] == "healthy" and main["reason"] is None
    assert main["verification"] == {
        "state": "verified",
        "trigger": "checks-changed",
        "checks_hash": "sha256:" + "e" * 64,
        "rolled": [],
        "at": main["verification"]["at"],
    }
    assert set(_components(status, "main").values()) == {"unchanged"}
    assert "main: verified (checks changed); rolled nothing" in lines

    # A failing changed check: degraded, still deployed, no retry.
    ports.env_up = _verification_up(calls, fail=True)
    channel.add_request(*sync_request("main"))
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "deployed", main
    assert main["health"] == "degraded"
    assert main["reason"] == "pipeline-checks-failed"
    assert main["attempts"] == 0 and main["next_attempt_at"] is None
    assert main["verification"]["state"] == "failed"
    assert main["verification"]["rolled"] == []
    assert main["verification"]["failed"] == [
        {
            "check": "http-login",
            "code": "check-failed",
            "detail": "GET /login returned 500",
        }
    ]
    assert main["components"]["web"]["health"] == "healthy"
    count = len(calls)
    world["clock"]["now"] += 10_000
    controller.poll_once()
    assert len(calls) == count  # not retried every poll

    # A passing verification clears it.
    ports.env_up = _verification_up(calls)
    channel.add_request(*sync_request("main"))
    main = controller.poll_once()["envs"]["main"]
    assert main["health"] == "healthy" and main["reason"] is None
    assert main["verification"]["state"] == "verified"


def test_failed_checks_of_a_rollout_still_fail_and_retry(world: dict[str, Any]) -> None:
    from piceli.gitops.state import sync_request
    from piceli.pipeline.errors import PipelineError

    controller, ports, channel = world["controller"], world["ports"], world["channel"]
    controller.poll_once()

    def env_up(pipeline: Any, name: str, **kwargs: Any) -> dict[str, Any]:
        raise PipelineError(
            "pipeline-checks-failed",
            "release main failed its checks",
            failed=True,
            details={"run_state": "rolled-back", "output": {"passed": False}},
        )

    ports.env_up = env_up
    channel.add_request(*sync_request("main"))
    main = controller.poll_once()["envs"]["main"]
    assert main["state"] == "retrying" and main["reason"] == "pipeline-checks-failed"
    assert "health" not in main or main["health"] != "degraded"


def test_teardown_and_a_restart_remove_the_state_of_gone_branch_envs(
    world: dict[str, Any],
) -> None:
    from piceli.gitops.state import save_state

    controller, ports, shop = world["controller"], world["ports"], world["shop"]
    controller.poll_once()
    shop.commit({"web/index.html": "<h1>login</h1>\n"}, branch="wp-login")
    shop.commit({"web/index.html": "<h1>cart</h1>\n"}, branch="wp-cart")
    status = controller.poll_once()
    assert status["envs"]["wp-login"]["namespace"] == "ns-wp-login"
    state = controller.state_dir
    branches = state / "pipelines" / "branches" / "branches"
    for namespace in ("ns-wp-login", "ns-wp-cart", "ns-wp-gone", "ns-wp-live"):
        (branches / namespace / "runs").mkdir(parents=True)
    controller.state["rebuild"] = {"wp-login": ["web"]}
    controller.state["forced"]["wp-login"] = {"trigger": "sync"}

    # Deleting the branch removes its pipeline state and what the
    # controller remembers of it.
    shop.delete_branch("wp-login")
    status = controller.poll_once()
    assert ports.removed == ["wp-login"] and "wp-login" not in status["envs"]
    assert not (branches / "ns-wp-login").exists()
    for key in ("env_seen", "rebuild", "forced", "promoted"):
        assert "wp-login" not in (controller.state.get(key) or {}), key
    assert (branches / "ns-wp-cart").is_dir()

    # On start: the stale state of environments that no longer exist goes
    # once; a live namespace (or one that cannot be looked up) keeps it.
    controller.state["env_seen"]["wp-old"] = {}
    controller.state["forced"]["wp-old"] = {"trigger": "sync"}
    save_state(state, controller.state)

    class Ports(FakePorts):
        live: set[str] | None = None

        def namespace_live(self, namespace: str) -> bool | None:
            return None if self.live is None else namespace in self.live

    def restart(live: set[str] | None) -> Any:
        restarted_ports = Ports()
        restarted_ports.live = live
        return CompositionController(
            controller.config,
            state_dir=state,
            sources=controller.sources,
            ports=restarted_ports,
            channel=world["channel"],
            clock=lambda: world["clock"]["now"],
        )

    restart(None).poll_once()
    assert (branches / "ns-wp-gone").is_dir()  # unknown: never deleted
    restarted = restart({"ns-wp-live", "ns-wp-cart"})
    status = restarted.poll_once()
    assert not (branches / "ns-wp-gone").exists()
    assert (branches / "ns-wp-live").is_dir() and (branches / "ns-wp-cart").is_dir()
    assert "wp-old" not in restarted.state["env_seen"]
    assert "wp-old" not in restarted.state["forced"]
    assert status["envs"]["wp-cart"]["state"] == "deployed"
    (branches / "ns-wp-later").mkdir()
    restarted.poll_once()
    assert (branches / "ns-wp-later").is_dir()  # swept once per start


def test_a_reverted_change_does_not_ask_again_for_the_running_plan(
    world: dict[str, Any],
) -> None:
    """rc ran the owner-approved plan A; B was never approved; A again applies.

    Only the plan of the running release counts: a plan never approved (C),
    or A once C runs, waits for the owner.
    """
    from piceli.envs import Environment
    from piceli.gitops.state import approve_request, sync_request

    controller, channel, ports = world["controller"], world["channel"], world["ports"]
    composition = controller.composition
    main = composition.environment("main")
    manual = Environment(
        "main",
        namespace=main.namespace,
        stack=main.stack,
        follow={source: list(items) for source, items in main.sources},
    )
    object.__setattr__(
        composition, "environments", (manual, *composition.environments[1:])
    )
    plan_a, plan_b, plan_c = ("sha256:" + c * 64 for c in "abc")
    current = {"plan": plan_a}
    calls: list[str | None] = []

    def env_up(pipeline: Any, name: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["approve"])
        if kwargs["approve"] != current["plan"]:
            return {"state": "approval-required", "plan_hash": current["plan"]}
        return {"state": "ready", "namespace": f"ns-{name}"}

    ports.env_up = env_up
    lines: list[str] = []
    controller.log = lines.append

    def redeploy(plan: str) -> dict[str, Any]:
        current["plan"] = plan
        channel.add_request(*sync_request("main"))
        env: dict[str, Any] = controller.poll_once()["envs"]["main"]
        return env

    assert controller.poll_once()["envs"]["main"]["plan_hash"] == plan_a
    channel.add_request(*approve_request("main", plan_a))
    assert controller.poll_once()["envs"]["main"]["state"] == "deployed"
    assert redeploy(plan_b)["state"] == "approval-required"  # never approved
    calls.clear()
    env = redeploy(plan_a)  # the revert
    assert env["state"] == "deployed", env
    assert calls == [None, plan_a]
    assert any("already approved" in line for line in lines)
    assert "approved_hash" not in env
    calls.clear()
    env = redeploy(plan_c)
    assert env["state"] == "approval-required" and calls == [None]
    channel.add_request(*approve_request("main", plan_c))
    assert controller.poll_once()["envs"]["main"]["state"] == "deployed"
    calls.clear()
    env = redeploy(plan_a)  # approved once, but C is the running release
    assert env["state"] == "approval-required" and calls == [None]
