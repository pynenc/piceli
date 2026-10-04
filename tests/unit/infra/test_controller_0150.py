"""0.15.0 fixes of the composition controller's status, each reproduced first.

- the status is published while a step runs, not only when the poll ends;
- after a failed release is rolled back and a revert makes the next deploy a
  no-op, the status no longer shows the failed run's checks as the checks of
  the healthy running release.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from piceli.pipeline import PipelineError
from tests.gitops_history_fixture import write_run
from tests.unit.infra.test_controller import world
from tests.unit.infra.test_controller_history import JournalPorts, journaled

__all__ = ["journaled", "world"]


def _published(state_dir: Path) -> dict[str, Any]:
    return json.loads((state_dir / "status.json").read_text())


def test_the_status_shows_a_step_while_it_runs(journaled: dict[str, Any]) -> None:
    controller, ports = journaled["controller"], journaled["ports"]
    seen: list[dict[str, Any]] = []
    original = ports.env_up

    def watching(pipeline: Any, name: str, **kwargs: Any) -> Any:
        seen.append(_published(controller.state_dir)["envs"].get(name) or {})
        return original(pipeline, name, **kwargs)

    ports.env_up = watching
    status = controller.poll_once()
    running = [item for item in seen if item.get("in_progress")]
    assert running, "no status was published during the deploy"
    assert running[0]["in_progress"]["action"] == "deploy"
    assert running[0]["in_progress"]["since"]
    assert all("in_progress" not in env for env in status["envs"].values())
    assert "in_progress" not in _published(controller.state_dir)["envs"]["main"]


class RevertPorts(JournalPorts):
    """Pass, then fail the checks and roll back, then a no-op deploy."""

    def __init__(self, state_dir: Path, clock: dict[str, float]) -> None:
        super().__init__(state_dir, clock)
        self.mode = "pass"

    def env_up(self, pipeline: Any, name: str, **kwargs: Any) -> dict[str, Any]:
        if kwargs["approve"] is None or self.mode == "pass":
            return super().env_up(pipeline, name, **kwargs)
        if self.mode == "noop":
            return {
                "state": "ready",
                "namespace": f"ns-{name}",
                "result": {
                    "state": "ready",
                    "stages": {"plan": "done", "apply": "skipped", "checks": "skipped"},
                },
            }
        write_run(
            self._runs(name),
            combined_hash="sha256:" + "8" * 64,
            release="shop-ffffffffffff",
            sources={"shop": {"ref": "refs/heads/main", "commit": "b" * 40}},
            applied=True,
            failed_check="smoke",
        )
        raise PipelineError(
            "pipeline-checks-failed",
            "release shop-ffffffffffff failed its checks",
            failed=True,
            details={
                "run_state": "rolled-back",
                "output": {
                    "passed": False,
                    "results": [{"name": "smoke", "passed": False}],
                    "rollback": {"state": "ready", "release": "shop-0123456789ab"},
                },
            },
        )


def test_a_no_op_revert_does_not_keep_the_failed_checks(
    journaled: dict[str, Any],
) -> None:
    controller, shop = journaled["controller"], journaled["shop"]
    ports = RevertPorts(controller.state_dir, journaled["clock"])
    controller.ports = ports
    main = controller.poll_once()["envs"]["main"]
    assert main["state"] == "deployed" and main["checks"]["state"] == "passed"
    passed = main["checks"]

    ports.mode = "fail"
    shop.commit({"web/index.html": "<h1>broken</h1>\n"})
    main = controller.poll_once()["envs"]["main"]
    assert main["state"] == "failed", main
    assert main["reason"] == "checks-failed-rolled-back"
    assert main["checks"]["state"] == "failed"

    ports.mode = "noop"
    shop.commit({"web/index.html": "<h1>shop</h1>\n"})  # the revert
    main = controller.poll_once()["envs"]["main"]
    assert main["state"] == "deployed" and main["health"] == "healthy", main
    assert main["last_action"] == "unchanged"
    # The running release is the one that passed, not the rolled-back one.
    assert main["checks"] == passed
