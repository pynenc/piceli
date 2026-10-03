"""0.14.7 UI and CLI controls: stop/start a named environment, the last checks.

The UI projection shows the last checks of an environment (after a rollout
too, never their detail lines) and a stopped environment's stop; Stop and
Start write the same request as ``piceli env stop|start``; a declared stop
refuses Start in the UI and in the CLI.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.gitops.state import DirectoryChannel
from piceli.k8s.cli import app
from piceli.services.composition_control import CompositionControl
from piceli.services.named_environment_actions import NamedEnvironmentActions
from piceli.services.query import QueryError
from tests.unit.services.test_ui014_controls import _query

ROOT = Path(__file__).resolve().parents[3]

CHECKS = {
    "state": "passed",
    "passed": 2,
    "total": 2,
    "results": [
        {"name": "web-home", "passed": True, "detail": "never-expose"},
        {"name": "store-ready", "passed": True, "code": None},
    ],
    "failed": [],
    "at": "2026-10-03T10:00:00Z",
    "trigger": "push web/main",
    "run_id": "20261003T100000Z-abc",
    "action": "deployed",
}


def _status(*, declared: bool = False, state: str = "deployed") -> dict[str, Any]:
    return {
        "schema": "piceli.gitops-status.v1",
        "controller": {
            "environments": [
                {"name": "rc", "promote": True, "stopped": declared},
            ]
        },
        "sources": {},
        "envs": {
            "rc": {
                "state": state,
                "reason": ("declared" if declared else "requested")
                if state == "stopped"
                else None,
                "namespace": "shop-rc",
                "revision": {},
                "components": {},
                "checks": CHECKS,
                **(
                    {
                        "stop": {
                            "by": "declared" if declared else "requested",
                            "via": "declaration" if declared else "cli",
                            "at": None if declared else "2026-10-03T10:01:00Z",
                        }
                    }
                    if state == "stopped"
                    else {}
                ),
            },
            "wp-a": {"state": "deployed", "namespace": "shop-wp-a"},
        },
    }


def _control(tmp_path: Path, status: dict[str, Any]) -> tuple[Any, DirectoryChannel]:
    channel = DirectoryChannel(tmp_path / "channel")
    channel.publish(status)

    @contextmanager
    def factory() -> Any:
        yield channel

    return CompositionControl(_query(tmp_path), "cluster", factory), channel


def test_the_environment_view_shows_its_last_checks_without_details(
    tmp_path: Path,
) -> None:
    control, _ = _control(tmp_path, _status())
    env = control.environment("rc")["environment"]
    assert env["checks"] == {
        "state": "passed",
        "passed": 2,
        "total": 2,
        "at": "2026-10-03T10:00:00Z",
        "trigger": "push web/main",
        "run_id": "20261003T100000Z-abc",
        "action": "deployed",
        "results": [
            {"name": "web-home", "passed": True, "code": None},
            {"name": "store-ready", "passed": True, "code": None},
        ],
    }
    assert "never-expose" not in json.dumps(env)
    assert env["stop"] is None


def test_stop_and_start_write_the_cli_requests(tmp_path: Path) -> None:
    control, channel = _control(tmp_path, _status())
    actions = NamedEnvironmentActions(control)
    options = actions.options("rc")
    assert options["stop"] == {"allowed": True, "reason": None}
    assert options["start"]["allowed"] is False
    assert actions.stop("rc") == {"state": "requested", "env": "rc", "action": "stop"}
    (body,) = channel.requests().values()
    assert (body["kind"], body["env"], body["via"]) == ("stop", "rc", "ui")
    with pytest.raises(QueryError) as stale:
        actions.start("rc")  # not stopped yet
    assert stale.value.code == "ui-plan-stale"
    with pytest.raises(QueryError) as branch:
        actions.stop("wp-a")  # a branch environment: idle stop only
    assert branch.value.code == "ui-sync-target-unknown"

    channel.publish(_status(state="stopped"))
    view = control.environment("rc")["environment"]
    assert view["stop"] == {"by": "requested", "via": "cli", "at": "2026-10-03T10:01:00Z"}
    assert actions.options("rc")["start"] == {"allowed": True, "reason": None}
    assert actions.start("rc")["action"] == "start"
    kinds = sorted(body["kind"] for body in channel.requests().values())
    assert kinds == ["start", "stop"]


def test_a_declared_stop_refuses_start_in_the_ui(tmp_path: Path) -> None:
    control, channel = _control(tmp_path, _status(declared=True, state="stopped"))
    actions = NamedEnvironmentActions(control)
    options = actions.options("rc")
    assert options["start"] == {"allowed": False, "reason": "gitops-env-stop-declared"}
    with pytest.raises(QueryError) as refused:
        actions.start("rc")
    assert refused.value.code == "gitops-env-stop-declared"
    assert channel.requests() == {}


def test_env_stop_and_start_record_requests_for_a_local_controller(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    stopped = runner.invoke(app, ["env", "stop", "rc", "--state-dir", str(tmp_path)])
    assert stopped.exit_code == 0, stopped.output
    body = json.loads(stopped.stdout)
    assert body["state"] == "requested" and body["action"] == "stop"
    started = runner.invoke(app, ["env", "start", "rc", "--state-dir", str(tmp_path)])
    assert started.exit_code == 0, started.output
    requests = DirectoryChannel(tmp_path).requests()
    assert sorted(item["kind"] for item in requests.values()) == ["start", "stop"]
    assert all(item["via"] == "cli" and item["at"] for item in requests.values())
    missing = runner.invoke(app, ["env", "stop", "rc"])
    assert missing.exit_code == 2
    assert '"reason": "gitops-target-required"' in missing.stdout


def test_env_start_of_a_declared_stop_is_refused_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "infra.py").write_text(
        "from piceli.envs import Environment, Stack\n"
        "from piceli.infra import Cluster, Component, Source\n"
        "name = 'shop'\n"
        "cluster = Cluster('my-cluster', api='https://127.0.0.1:6443', "
        "credentials='my-cluster')\n"
        "shop = Source('https://example.com/shop.git', name='shop')\n"
        "web = Component('web', source=shop)\n"
        "environments = [Environment('rc', namespace='shop-rc', "
        "stack=Stack('s', [web]), cluster=cluster, follow={shop: 'main'}, "
        "stopped=True)]\n"
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    refused = runner.invoke(app, ["env", "start", "rc", "--cluster", "infra.py:cluster"])
    assert refused.exit_code == 2
    assert '"reason": "gitops-env-stop-declared"' in refused.stdout
    unknown = runner.invoke(app, ["env", "stop", "nope", "--cluster", "infra.py:cluster"])
    assert unknown.exit_code == 2 and '"reason": "env-not-found"' in unknown.stdout
