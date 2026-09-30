"""The GitOps controller loop against a local bare Git repository and fake ports.

Every test builds a real repository in ``tmp_path`` (branches, tags,
deletions) and drives :class:`piceli.gitops.controller.Controller` one poll
at a time with a fake clock; builds and environments are fakes recording
their calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.gitops import GitOpsError
from piceli.gitops.config import ControllerConfig, check_repo_url, parse_duration
from piceli.gitops.controller import Controller
from piceli.gitops.ports import APPROVE_POLICY, EnvOutcome
from piceli.gitops.repo import GitRemote, parse_ls_remote
from piceli.gitops.state import (
    DirectoryChannel,
    approve_request,
    controller_lock,
    promote_request,
    push_request,
)
from piceli.k8s.cli import app

SECRET = "hunter2-do-not-print"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Example",
    "GIT_AUTHOR_EMAIL": "example@example.com",
    "GIT_COMMITTER_NAME": "Example",
    "GIT_COMMITTER_EMAIL": "example@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def plan_hash(branch: str, commit: str) -> str:
    return "sha256:" + hashlib.sha256(f"{branch}@{commit}".encode()).hexdigest()


class Repo:
    """A bare remote and a working clone that pushes to it."""

    def __init__(self, root: Path) -> None:
        self.remote = root / "remote.git"
        self.work = root / "work"
        self.git("init", "--bare", "--quiet", "-b", "main", str(self.remote), cwd=root)
        self.git("clone", "--quiet", str(self.remote), str(self.work), cwd=root)
        self.git("checkout", "--quiet", "-b", "main")
        self.commit("first")
        self.git("push", "--quiet", "origin", "main")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.work,
            env={**os.environ, **GIT_ENV},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def commit(self, text: str) -> str:
        (self.work / "deploy").mkdir(exist_ok=True)
        (self.work / "deploy" / "app.py").write_text("pipeline = None\n")
        (self.work / "VERSION").write_text(text)
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", text)
        return self.git("rev-parse", "HEAD")

    def push_branch(self, branch: str, text: str) -> str:
        self.git("checkout", "--quiet", "-B", branch, "main")
        sha = self.commit(text)
        self.git("push", "--quiet", "--force", "origin", branch)
        self.git("checkout", "--quiet", "main")
        return sha

    def push_main(self, text: str) -> str:
        sha = self.commit(text)
        self.git("push", "--quiet", "origin", "main")
        return sha

    def tag(self, name: str, *, annotated: bool = False) -> str:
        if annotated:
            self.git("tag", "-a", name, "-m", name)
        else:
            self.git("tag", name)
        self.git("push", "--quiet", "origin", name)
        return self.git("rev-parse", "HEAD")

    def delete_branch(self, branch: str) -> None:
        self.git("push", "--quiet", "origin", "--delete", branch)


class FakePorts:
    """Records calls; env_up approves by policy or by the exact plan hash."""

    def __init__(self, *, policy: bool = True) -> None:
        self.policy = policy
        self.calls: list[tuple[Any, ...]] = []
        self.fail_builds: set[str] = set()
        self.active = 0
        self.max_active = 0

    def load_pipeline(self, checkout: Path, entry: str, env: str | None) -> Any:
        assert entry == "deploy/app.py:pipeline"
        assert (checkout / "deploy" / "app.py").is_file()
        return SimpleNamespace(
            auto_approve=object() if self.policy else None,
            version=(checkout / "VERSION").read_text(),
        )

    def build(
        self, pipeline: Any, commit: str, *, cache_key: str, platforms: Any
    ) -> Mapping[str, Any]:
        self.calls.append(("build", cache_key, commit))
        if commit in self.fail_builds:
            raise RuntimeError(f"builder said {SECRET}")
        return {"schema": "fake-build", "images": {"web": "sha256:" + "b" * 64}}

    def prepare_env(self, pipeline: Any, branch: str) -> str:
        return "app-" + branch

    def env_up(
        self,
        pipeline: Any,
        branch: str,
        *,
        commit: str,
        receipt: Any,
        digests: Any,
        approve: str | None,
    ) -> EnvOutcome:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append(("up", branch, commit, approve, digests))
            expected = plan_hash(branch, commit)
            if approve == expected or (approve == APPROVE_POLICY and self.policy):
                return EnvOutcome("deployed", namespace="app-" + branch)
            return EnvOutcome("approval-required", plan_hash=expected)
        finally:
            self.active -= 1

    def env_down(self, pipeline: Any, branch: str) -> None:
        self.calls.append(("down", branch))

    def kinds(self, kind: str) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call[0] == kind]


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def make(
    tmp_path: Path, repo: Repo, ports: FakePorts, **settings: Any
) -> tuple[Controller, DirectoryChannel, Clock]:
    config = ControllerConfig(
        pipeline="deploy/app.py:pipeline",
        repo=str(repo.remote),
        branches=("main", "wp-*"),
        **settings,
    )
    state = tmp_path / "state"
    channel = DirectoryChannel(state)
    clock = Clock()
    controller = Controller(
        config,
        state_dir=state,
        source=GitRemote(config.repo, state / "mirror"),
        ports=ports,
        channel=channel,
        clock=clock,
    )
    return controller, channel, clock


def test_branch_push_deploys_and_untagged_main_push_does_not(
    tmp_path: Path, repo: Repo
) -> None:
    ports = FakePorts()
    controller, channel, _ = make(tmp_path, repo, ports)
    sha = repo.push_branch("wp-1", "one")
    repo.push_branch("feature-x", "not watched")
    status = controller.poll_once()
    assert ports.kinds("build") == [("build", "wp-1", sha)]
    assert ports.kinds("up") == [("up", "wp-1", sha, APPROVE_POLICY, None)]
    env = status["envs"]["wp-1"]
    assert env["state"] == "deployed" and env["deployed_commit"] == sha
    assert env["namespace"] == "app-wp-1"
    assert "main" not in status["envs"] and "feature-x" not in status["envs"]
    # An untagged push to main and an unchanged branch: nothing to do.
    repo.push_main("untagged")
    status = controller.poll_once()
    assert len(ports.kinds("up")) == 1 and "main" not in status["envs"]
    # Every push redeploys the branch.
    second = repo.push_branch("wp-1", "two")
    controller.poll_once()
    assert ports.kinds("up")[-1][2] == second
    assert channel.read_status() == controller.status()
    assert ports.max_active == 1


def test_existing_tags_are_the_baseline_and_a_new_tag_needs_approval(
    tmp_path: Path, repo: Repo
) -> None:
    ports = FakePorts()
    controller, channel, _ = make(tmp_path, repo, ports)
    repo.tag("v1.0.0")
    controller.poll_once()
    assert ports.kinds("up") == []  # tags present at enable deploy nothing
    repo.push_main("release")
    tagged = repo.tag("v1.10.0", annotated=True)  # the peeled commit deploys
    repo.push_main("after the tag")
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["commit"] == tagged and main["trigger"] == "tag v1.10.0"
    # Main needs a hash approval even though the pipeline has a policy.
    assert ports.kinds("up") == [("up", "main", tagged, None, None)]
    assert main["state"] == "approval-required"
    assert main["plan_hash"] == plan_hash("main", tagged)
    # A stale hash is dropped and reported, the right one deploys.
    channel.add_request(*approve_request("main", "sha256:" + "0" * 64))
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "approval-required"
    assert status["rejected_requests"][-1]["reason"] == "gitops-approval-stale"
    channel.add_request(*approve_request("main", plan_hash("main", tagged)))
    status = controller.poll_once()
    assert ports.kinds("up")[-1] == (
        "up",
        "main",
        tagged,
        plan_hash("main", tagged),
        None,
    )
    assert status["envs"]["main"]["state"] == "deployed"
    assert channel.requests() == {}


def test_main_auto_approve_is_the_owners_opt_in(tmp_path: Path, repo: Repo) -> None:
    ports = FakePorts()
    controller, _, _ = make(tmp_path, repo, ports, main_auto_approve=True)
    controller.poll_once()
    tagged = repo.tag("v2")
    status = controller.poll_once()
    assert ports.kinds("up") == [("up", "main", tagged, APPROVE_POLICY, None)]
    assert status["envs"]["main"]["state"] == "deployed"


def test_branch_without_policy_waits_for_approval(tmp_path: Path, repo: Repo) -> None:
    ports = FakePorts(policy=False)
    controller, channel, _ = make(tmp_path, repo, ports)
    sha = repo.push_branch("wp-2", "x")
    status = controller.poll_once()
    assert status["envs"]["wp-2"]["state"] == "approval-required"
    controller.poll_once()  # waiting: no new attempt, no new build
    assert len(ports.kinds("up")) == 1 and len(ports.kinds("build")) == 1
    channel.add_request(*approve_request("wp-2", plan_hash("wp-2", sha)))
    status = controller.poll_once()
    assert status["envs"]["wp-2"]["state"] == "deployed"
    assert len(ports.kinds("build")) == 1  # the kept receipt, not a rebuild


def test_promote_a_branch_commit_to_main(tmp_path: Path, repo: Repo) -> None:
    ports = FakePorts()
    controller, channel, _ = make(tmp_path, repo, ports)
    sha = repo.push_branch("wp-3", "candidate")
    controller.poll_once()
    channel.add_request(*promote_request(f"wp-3@{'f' * 12}"))
    status = controller.poll_once()
    assert status["rejected_requests"][-1]["reason"] == "gitops-promote-unknown"
    assert "main" not in status["envs"]
    channel.add_request(*promote_request(f"wp-3@{sha[:12]}"))
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["commit"] == sha and main["trigger"].startswith("promote wp-3@")
    assert main["state"] == "approval-required"
    assert ports.kinds("up")[-1] == ("up", "main", sha, None, None)


def test_deleted_branch_is_torn_down_and_main_never(tmp_path: Path, repo: Repo) -> None:
    ports = FakePorts()
    controller, _, _ = make(tmp_path, repo, ports, main_auto_approve=True)
    controller.poll_once()
    repo.tag("v1")
    repo.push_branch("wp-4", "doomed")
    controller.poll_once()
    repo.delete_branch("wp-4")
    status = controller.poll_once()
    assert ports.kinds("down") == [("down", "wp-4")]
    assert "wp-4" not in status["envs"]
    assert status["envs"]["main"]["state"] == "deployed"
    controller.poll_once()
    assert ports.kinds("down") == [("down", "wp-4")]


def test_failed_build_backs_off_then_gives_up_without_blocking_others(
    tmp_path: Path, repo: Repo
) -> None:
    ports = FakePorts()
    controller, _, clock = make(tmp_path, repo, ports, max_attempts=3)
    bad = repo.push_branch("wp-bad", "broken")
    good = repo.push_branch("wp-good", "fine")
    ports.fail_builds.add(bad)
    status = controller.poll_once()
    assert status["envs"]["wp-good"]["state"] == "deployed"
    record = status["envs"]["wp-bad"]
    assert record["state"] == "retrying" and record["attempts"] == 1
    assert record["reason"] == "gitops-step-failed"
    assert record["next_attempt_at"] == clock.now + 30
    controller.poll_once()  # before the backoff: no new attempt
    assert len([c for c in ports.kinds("build") if c[2] == bad]) == 1
    clock.now += 30
    status = controller.poll_once()
    assert status["envs"]["wp-bad"]["next_attempt_at"] == clock.now + 60
    clock.now += 60
    status = controller.poll_once()
    assert status["envs"]["wp-bad"]["state"] == "failed"
    clock.now += 10_000
    controller.poll_once()  # failed stays failed until the next push
    assert len([c for c in ports.kinds("build") if c[2] == bad]) == 3
    fixed = repo.push_branch("wp-bad", "fixed")
    status = controller.poll_once()
    assert status["envs"]["wp-bad"]["state"] == "deployed"
    assert status["envs"]["wp-bad"]["commit"] == fixed
    assert good == status["envs"]["wp-good"]["deployed_commit"]
    everything = json.dumps(status) + (tmp_path / "state" / "state.json").read_text()
    assert SECRET not in everything


def test_pushed_digest_skips_the_build(tmp_path: Path, repo: Repo) -> None:
    ports = FakePorts()
    controller, channel, _ = make(tmp_path, repo, ports)
    sha = repo.push_branch("wp-5", "laptop")
    digests = {"web": "sha256:" + "c" * 64}
    channel.add_request(*push_request("wp-5", sha, digests=digests))
    controller.poll_once()
    assert ports.kinds("build") == []
    assert ports.kinds("up") == [("up", "wp-5", sha, APPROVE_POLICY, digests)]


def test_git_failure_is_recorded_not_raised(tmp_path: Path, repo: Repo) -> None:
    ports = FakePorts()
    config = ControllerConfig(
        pipeline="deploy/app.py:pipeline",
        repo=str(tmp_path / "missing.git"),
        branches=("wp-*",),
    )
    channel = DirectoryChannel(tmp_path / "state")
    controller = Controller(
        config,
        state_dir=tmp_path / "state",
        source=GitRemote(config.repo, tmp_path / "state" / "mirror"),
        ports=ports,
        channel=channel,
    )
    status = controller.poll_once()
    assert status["controller"]["state"] == "degraded"
    assert status["controller"]["last_error"] == "gitops-git-failed"
    assert status["controller"]["poll_failures"] == 1
    assert ports.calls == []


def test_one_controller_per_state_directory(tmp_path: Path) -> None:
    with controller_lock(tmp_path):
        with pytest.raises(GitOpsError) as error:
            with controller_lock(tmp_path):
                pass
    assert error.value.code == "gitops-controller-locked"


def test_parse_ls_remote_peels_annotated_tags() -> None:
    a, b, c = "a" * 40, "b" * 40, "c" * 40
    refs = parse_ls_remote(
        f"{a}\trefs/heads/main\n{b}\trefs/tags/v1\n{c}\trefs/tags/v1^{{}}\n"
    )
    assert refs.branches == {"main": a} and refs.tags == {"v1": c}


@pytest.mark.parametrize(
    "url",
    [
        "https://user:token@example.com/app.git",
        "https://token@example.com/app.git",
        "ssh://git:pw@example.com/app.git",
        "--upload-pack=evil",
        "ftp://example.com/app.git",
    ],
)
def test_repo_urls_with_credentials_are_refused(url: str) -> None:
    with pytest.raises(GitOpsError) as error:
        check_repo_url(url)
    assert error.value.code == "gitops-repo-invalid"


def test_repo_urls_accepted() -> None:
    for url in (
        "https://example.com/app.git",
        "ssh://git@example.com/app.git",
        "git@example.com:org/app.git",
        "/srv/git/app.git",
    ):
        assert check_repo_url(url) == url
    assert parse_duration("5m") == 300 and parse_duration("60") == 60


def test_https_credentials_reach_git_only_through_files(tmp_path: Path) -> None:
    credentials = tmp_path / "git"
    credentials.mkdir()
    (credentials / "username").write_text("bot")
    (credentials / "password").write_text(SECRET)
    seen: list[dict[str, Any]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append({"argv": argv, "env": kwargs["env"]})
        return subprocess.CompletedProcess(argv, 128, b"", SECRET.encode())

    remote = GitRemote(
        "https://example.com/app.git",
        tmp_path / "mirror",
        credentials_dir=credentials,
        runner=runner,
    )
    with pytest.raises(GitOpsError) as error:
        remote.ls_remote()
    assert error.value.code == "gitops-git-failed"
    assert SECRET not in str(error.value)
    call = seen[0]
    assert SECRET not in " ".join(call["argv"])
    assert SECRET not in json.dumps({k: v for k, v in call["env"].items()})
    assert any("credential.helper=!f()" in arg for arg in call["argv"])


def test_cli_approve_promote_and_status_on_a_local_state_dir(
    tmp_path: Path, repo: Repo
) -> None:
    ports = FakePorts(policy=False)
    controller, _, _ = make(tmp_path, repo, ports)
    sha = repo.push_branch("wp-6", "x")
    controller.poll_once()
    state = str(tmp_path / "state")
    runner = CliRunner()
    shown = runner.invoke(app, ["gitops", "status", "--state-dir", state, "--json"])
    assert shown.exit_code == 0, shown.output
    body = json.loads(shown.stdout)
    assert body["envs"]["wp-6"]["plan_hash"] == plan_hash("wp-6", sha)
    assert body["health"] in {"healthy", "stale"}
    assert "piceli gitops approve wp-6 sha256:" in shown.stderr
    approved = runner.invoke(
        app, ["gitops", "approve", "wp-6", plan_hash("wp-6", sha), "--state-dir", state]
    )
    assert (
        approved.exit_code == 0 and json.loads(approved.stdout)["state"] == "requested"
    )
    promoted = runner.invoke(app, ["promote", f"wp-6@{sha}", "--state-dir", state])
    assert promoted.exit_code == 0, promoted.output
    status = controller.poll_once()
    assert status["envs"]["wp-6"]["state"] == "deployed"
    assert status["envs"]["main"]["commit"] == sha
