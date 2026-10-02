"""The GitOps controller with named environments and idle stop (0.14)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.gitops import GitOpsError
from piceli.gitops.config import ControllerConfig, EnvRule
from piceli.gitops.ports import APPROVE_POLICY
from piceli.gitops.state import approve_request, promote_request
from piceli.k8s.cli import app
from piceli.k8s.cli.gitops import environment_rules
from tests.unit.test_gitops_controller import FakePorts, Repo, make, plan_hash

MAIN = EnvRule("main-env", branches=("main",), auto_approve=True)
RC = EnvRule("rc", tags=("v*-rc*",), promote=True)


class StopPorts(FakePorts):
    def env_stop(self, pipeline: Any, branch: str) -> None:
        self.calls.append(("stop", branch))


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def test_rules_round_trip_and_keep_0_13_config() -> None:
    config = ControllerConfig(
        pipeline="deploy/app.py:pipeline",
        repo="https://example.com/r.git",
        branches=("main", "wp-*"),
        environments=(MAIN, RC),
        idle_stop_seconds=3600,
    )
    assert ControllerConfig.from_dict(config.to_dict()) == config
    assert [rule.name for rule in config.rules()] == ["main", "main-env", "rc"]
    assert config.fixed("rc") and config.fixed("main") and not config.fixed("wp-1")
    plain = ControllerConfig(
        pipeline="deploy/app.py:pipeline",
        repo="https://example.com/r.git",
        branches=("main",),
    )
    assert "environments" not in plain.to_dict()
    assert "idle_stop_seconds" not in plain.to_dict()
    with pytest.raises(GitOpsError):
        ControllerConfig(
            pipeline="deploy/app.py:pipeline",
            repo="https://example.com/r.git",
            branches=("main",),
            environments=(RC, RC),
        )


def test_every_main_push_deploys_its_env_and_tags_deploy_rc(
    tmp_path: Path, repo: Repo
) -> None:
    ports = FakePorts()
    controller, channel, _ = make(tmp_path, repo, ports, environments=(MAIN, RC))
    repo.tag("v0.9.0-rc1")  # present at the first poll: rc's baseline
    status = controller.poll_once()
    first = status["envs"]["main-env"]
    assert first["trigger"] == "push main" and first["state"] == "deployed"
    assert "rc" not in status["envs"]
    # An untagged push to main deploys main-env (auto_approve) and only it.
    second = repo.push_main("change")
    status = controller.poll_once()
    assert status["envs"]["main-env"]["deployed_commit"] == second
    assert ports.kinds("up")[-1] == ("up", "main-env", second, APPROVE_POLICY, None)
    assert "main" not in status["envs"]
    # A matching tag deploys rc (waits for approval) and the implicit main.
    tagged = repo.tag("v1.0.0-rc1")
    status = controller.poll_once()
    rc = status["envs"]["rc"]
    assert rc["commit"] == tagged and rc["trigger"] == "tag v1.0.0-rc1"
    assert rc["state"] == "approval-required"
    assert status["envs"]["main"]["trigger"] == "tag v1.0.0-rc1"
    channel.add_request(*approve_request("rc", plan_hash("rc", tagged)))
    status = controller.poll_once()
    assert status["envs"]["rc"]["state"] == "deployed"
    # main-env was not redeployed by the tag (same head).
    assert [c for c in ports.kinds("up") if c[1] == "main-env"][-1][2] == second
    # The receipt of a commit is reused by another environment.
    built = [call[2] for call in ports.kinds("build")]
    assert len(built) == len(set(built))
    assert status["controller"]["environments"][1]["name"] == "rc"


def test_promote_to_a_named_environment(tmp_path: Path, repo: Repo) -> None:
    ports = FakePorts()
    controller, channel, _ = make(tmp_path, repo, ports, environments=(MAIN, RC))
    sha = repo.push_branch("wp-1", "candidate")
    controller.poll_once()
    channel.add_request(*promote_request(f"wp-1@{sha[:12]}", "main-env"))
    channel.add_request(*promote_request(f"wp-1@{sha[:12]}", "nope"))
    status = controller.poll_once()
    reasons = [item["reason"] for item in status["rejected_requests"]]
    assert reasons == ["gitops-promote-not-allowed"] * 2
    channel.add_request(*promote_request(f"wp-1@{sha[:12]}", "rc"))
    status = controller.poll_once()
    assert status["envs"]["rc"]["commit"] == sha
    assert status["envs"]["rc"]["trigger"].startswith("promote wp-1@")
    # The 0.13 form still targets main.
    channel.add_request(*promote_request(f"wp-1@{sha[:12]}"))
    status = controller.poll_once()
    assert status["envs"]["main"]["commit"] == sha


def test_a_declared_main_replaces_the_implicit_one(tmp_path: Path, repo: Repo) -> None:
    ports = FakePorts()
    declared = EnvRule("main", branches=("main",))
    controller, _, _ = make(tmp_path, repo, ports, environments=(declared,))
    controller.poll_once()
    repo.tag("v2.0.0")
    head = repo.push_main("next")
    status = controller.poll_once()
    assert status["envs"]["main"]["trigger"] == "push main"
    assert status["envs"]["main"]["commit"] == head
    # No hash approval was given and auto_approve is off: it waits.
    assert status["envs"]["main"]["state"] == "approval-required"


def test_idle_branch_env_is_stopped_and_restarted_by_a_push(
    tmp_path: Path, repo: Repo
) -> None:
    ports = StopPorts()
    controller, _, clock = make(tmp_path, repo, ports, idle_stop_seconds=3600)
    repo.push_branch("wp-1", "one")
    controller.poll_once()
    clock.now += 1800
    controller.poll_once()
    assert ports.kinds("stop") == []
    clock.now += 1900
    status = controller.poll_once()
    assert ports.kinds("stop") == [("stop", "wp-1")]
    entry = status["envs"]["wp-1"]
    assert entry["state"] == "stopped" and entry["reason"] == "idle-stop"
    controller.poll_once()
    assert len(ports.kinds("stop")) == 1  # stays stopped, no redeploy
    sha = repo.push_branch("wp-1", "two")
    status = controller.poll_once()
    assert status["envs"]["wp-1"]["state"] == "deployed"
    assert ports.kinds("up")[-1][2] == sha
    repo.delete_branch("wp-1")
    controller.poll_once()
    assert ports.kinds("down") == [("down", "wp-1")]


def test_enable_reads_named_environments(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "app.py").write_text(
        "from pathlib import Path\n"
        "from piceli import App, EnvConfig, Pipeline, Promote, Tag, Target\n"
        "from piceli.envs import Environment\n"
        "app = App('shop')\n"
        "app.deployment('web', image='example/web@sha256:' + '1' * 64)\n"
        "pipeline = Pipeline(app, Target(Path('kc'), context='c', namespace='shop'),\n"
        "    envs=EnvConfig(prefix='shop-', idle_stop='24h', environments=[\n"
        "        Environment('rc', namespace='shop-rc', follow=[Tag('v*-rc*'), Promote()])]))\n"
    )
    rules, idle = environment_rules("deploy/app.py:pipeline", tmp_path, None)
    assert rules == (EnvRule("rc", tags=("v*-rc*",), promote=True),)
    assert idle == 86400
    assert environment_rules("missing.py:pipeline", tmp_path, None) == ((), None)
    (deploy / "broken.py").write_text("raise RuntimeError('boom')\n")
    with pytest.raises(GitOpsError) as error:
        environment_rules("deploy/broken.py:pipeline", tmp_path, None)
    assert error.value.code == "gitops-pipeline-invalid"


def test_promote_cli_takes_an_environment(tmp_path: Path) -> None:
    state = tmp_path / "state"
    result = CliRunner().invoke(
        app, ["promote", "rc", "wp-1@abcdef1", "--state-dir", str(state)]
    )
    assert result.exit_code == 0, result.output
    assert '"env": "rc"' in result.stdout
    result = CliRunner().invoke(
        app, ["promote", "wp-1@abcdef1", "--state-dir", str(state)]
    )
    assert result.exit_code == 0 and '"env"' not in result.stdout
