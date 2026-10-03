"""0.14.7 fixes of the composition controller's bookkeeping, each reproduced first.

- the status keeps the last checks of every deploy (a rollout too), not only
  of a checks-only verification;
- a branch environment's run is triggered by its branch's push and, applied
  by policy, records the policy as its approver;
- an approval followed by an unrelated push with the same plan hash is kept;
- a component built from several sources names the one that triggered it;
- the log names only components that actually rolled;
- a failed attempt's log tail survives the retry that succeeds.

Local Git sources and fake deploys (as in ``test_controller.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from piceli.gitops.state import approve_request, sync_request
from tests.unit.infra.test_controller import world
from tests.unit.infra.test_controller_history import (
    PLAN,
    _manual_main,
    _published,
    journaled,
)

__all__ = ["journaled", "world"]


def test_status_shows_the_checks_of_a_rollout(journaled: dict[str, Any]) -> None:
    controller = journaled["controller"]
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["last_action"] == "deployed"
    assert main["verification"] is None  # a rollout, not a verification
    checks = main["checks"]
    assert checks["state"] == "passed"
    assert (checks["passed"], checks["total"]) == (2, 2)
    assert [item["name"] for item in checks["results"]] == ["web-home", "worker-ready"]
    assert all(item["passed"] for item in checks["results"])
    assert checks["failed"] == []
    assert checks["at"] and checks["run_id"] and checks["trigger"].startswith("push ")
    assert checks["action"] == "deployed"


def test_status_shows_failing_checks_of_a_verification(
    journaled: dict[str, Any],
) -> None:
    controller, channel, ports = (
        journaled["controller"],
        journaled["channel"],
        journaled["ports"],
    )
    controller.poll_once()
    ports.fail_check = "deliberate-failure"
    channel.add_request(*sync_request("main"))
    checks = controller.poll_once()["envs"]["main"]["checks"]
    assert checks["state"] == "failed" and checks["trigger"] == "sync"
    assert checks["failed"] == ["deliberate-failure"]
    assert {"name": "deliberate-failure", "passed": False, "code": "check-failed"} in (
        checks["results"]
    )


def test_a_branch_run_records_its_branch_push_and_the_policy(
    world: dict[str, Any],
) -> None:
    controller, channel = world["controller"], world["channel"]
    controller.poll_once()
    world["shop"].commit({"web/index.html": "<h1>login</h1>\n"}, branch="wp-login")
    status = controller.poll_once()
    branch = status["envs"]["wp-login"]
    # The branch is first seen with every followed head new: shop's branch
    # push triggered it, not catalog's main.
    assert branch["trigger"] == "push shop/wp-login"
    run = _published(channel)["envs"]["wp-login"]["runs"][0]
    assert run["trigger"] == "push shop/wp-login"
    assert run["approved_by"] == {"via": "policy", "at": None}


def test_an_approval_survives_an_unrelated_push_with_the_same_plan(
    journaled: dict[str, Any],
) -> None:
    controller, channel = journaled["controller"], journaled["channel"]
    _manual_main(controller)
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "approval-required"
    channel.add_request(*approve_request("main", PLAN, via="cli"))
    # A push to a followed source that changes no image: the plan keeps its hash.
    journaled["shop"].commit({"README.md": "docs\n"})
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "deployed", main.get("reason")
    run = _published(channel)["envs"]["main"]["runs"][0]
    assert run["state"] == "deployed" and run["approved_by"]["via"] == "cli"


def test_a_changed_plan_still_asks_after_an_approval(
    journaled: dict[str, Any],
) -> None:
    controller, channel, ports = (
        journaled["controller"],
        journaled["channel"],
        journaled["ports"],
    )
    _manual_main(controller)
    controller.poll_once()
    channel.add_request(*approve_request("main", PLAN, via="cli"))
    from piceli.envs import EnvError

    original = ports.env_up

    def changed(pipeline: Any, name: str, **kwargs: Any) -> Any:
        if kwargs["approve"] == PLAN:
            raise EnvError("env-plan-changed", "the plan changed")
        return original(pipeline, name, **kwargs)

    ports.env_up = changed
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "approval-required"


def test_the_component_names_the_source_that_triggered_its_build() -> None:
    from piceli.infra.controller import CompositionController
    from piceli.infra.pipelines import ImageKey

    a1, a2, b1, b2 = "a" * 40, "c" * 40, "b" * 40, "d" * 40
    built = CompositionController._built_from
    key = ImageKey("build", "web", "sha256:" + "1" * 64, {"assets": a1, "product": b1})
    # First build: the trigger's source, not the alphabetically first one.
    assert built(None, key, "push product/main", "infra", {}) == ("product", b1)
    before = {
        "source": "product",
        "commit": b1,
        "source_digest": key.key,
        "sources": {"assets": a1, "product": b1},
    }
    # Rebuilt after a change of assets only: assets triggered it.
    moved = ImageKey("build", "web", "sha256:" + "2" * 64, {"assets": a2, "product": b1})
    assert built(before, moved, "push infra/main", "infra", {}) == ("assets", a2)
    # Both moved: the run's trigger decides.
    both = ImageKey("build", "web", "sha256:" + "3" * 64, {"assets": a2, "product": b2})
    assert built(before, both, "push product/main", "infra", {}) == ("product", b2)
    # The key did not change: it keeps the source and commit it was built at.
    same = ImageKey("build", "web", key.key, {"assets": a2, "product": b2})
    assert built(before, same, "push assets/main", "infra", {}) == ("product", b1)


def test_a_contract_component_lists_its_source_commit(world: dict[str, Any]) -> None:
    status = world["controller"].poll_once()
    web = status["envs"]["main"]["components"]["web"]
    assert web["sources"] == {web["source"]: web["commit"]}


def test_the_log_names_only_components_that_rolled(world: dict[str, Any]) -> None:
    controller, channel = world["controller"], world["channel"]
    lines: list[str] = []
    controller.log = lines.append
    controller.poll_once()
    assert any("deployed; rolled api, cache, catalog, web" in line for line in lines)
    # The same digests under another registry name: nothing rolls.
    for path in (controller.state_dir / "components").rglob("*.json"):
        cached = json.loads(path.read_text())
        cached["pull_ref"] = cached["pull_ref"].replace(
            "registry.example:5000", "registry.local:5000"
        )
        path.write_text(json.dumps(cached))
    lines.clear()
    channel.add_request(*sync_request("main"))
    status = controller.poll_once()
    assert any("deployed; rolled nothing" in line for line in lines), lines
    states = {k: v["state"] for k, v in status["envs"]["main"]["components"].items()}
    assert set(states.values()) == {"unchanged"}


def test_a_plan_that_changes_no_workload_rolls_nothing() -> None:
    from piceli.infra.controller import CompositionController

    components = {
        "web": {"state": "rolling", "deployed_image": "r/web@sha256:1", "image": "r/web@sha256:2"},
        "api": {"state": "rolling", "deployed_image": None, "image": "r/api@sha256:3"},
    }  # fmt: skip
    rolled = CompositionController._rolled
    config = {"plan": {"changes": [{"operation": "update", "kind": "ConfigMap", "name": "x"}], "changes_total": 1}}  # fmt: skip
    assert rolled(components, "deployed", config) == set()
    web = {"plan": {"changes": [{"operation": "update", "kind": "Deployment", "name": "web"}], "changes_total": 1}}  # fmt: skip
    assert rolled(components, "deployed", web) == {"web", "api"}
    assert rolled(components, "deployed", None) == {"web", "api"}
    assert rolled(components, "verified", web) == set()


def test_a_failed_attempt_survives_the_retry_that_succeeds(
    world: dict[str, Any], tmp_path: Path
) -> None:
    from piceli.infra import CompositionError

    controller, channel, ports, clock = (
        world["controller"],
        world["channel"],
        world["ports"],
        world["clock"],
    )
    real = ports.builder.build
    attempts: list[int] = []

    def flaky(items: Any, checkout: Any) -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            raise CompositionError(
                "component-build-failed",
                "the component build Job ended failed",
                details={"outcome": {"state": "failed", "log_tail": "cc: error 1"}},
            )
        return real(items, checkout)

    ports.builder.build = flaky
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "retrying"
    assert main["failed_attempts"][0]["log_tail"] == "cc: error 1"
    clock["now"] += 3600
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "deployed" and main["failure"] is None
    runs = _published(channel)["envs"]["main"]["runs"]
    assert runs[0]["state"] == "deployed"
    (attempt,) = runs[0]["failed_attempts"]
    assert attempt["reason"] == "component-build-failed"
    assert attempt["log_tail"] == "cc: error 1" and attempt["attempt"] == 1
    assert runs[1]["state"] == "retrying"
    assert runs[1]["failure"]["log_tail"] == "cc: error 1"
    assert main["failed_attempts"] == []


@pytest.mark.parametrize("attempts", [25, 200])
def test_a_forever_forward_backs_off_capped_and_never_gives_up(attempts: int) -> None:
    from piceli.k8s.observe import ForwardSupervisor, PortForward, _ManagedForward
    from piceli.k8s.ui_config import RestartPolicy

    supervisor = ForwardSupervisor(kubeconfig=Path("/no/config"), context="c")
    forward = PortForward(
        name="ui",
        target="service/piceli-ui",
        namespace="piceli-system",
        local_port=8790,
        remote_port=8790,
    )
    managed = _ManagedForward(forward, policy=RestartPolicy(backoff_max=30.0, forever=True))
    for _ in range(attempts):
        supervisor._schedule_restart_locked(managed, "port-forward exited with 1")
    assert not managed.given_up and managed.health == "restarting"
    assert managed.policy.delay(managed.attempts) == 30.0
    bounded = _ManagedForward(forward, policy=RestartPolicy(max_restarts=20))
    for _ in range(21):
        supervisor._schedule_restart_locked(bounded, "port-forward exited with 1")
    assert bounded.given_up
