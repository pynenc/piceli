"""Acceptance: the CI recipe (``examples/ci/github-actions-deploy.yml``) works.

The test reads the sample workflow and runs, in order, its ``piceli deploy``
command lines (plan the pushed commit, apply the approved plan file, resume)
and its kubeconfig step (a real ``bash``), against the fake API server and a
git repository, the way the jobs chain them: the plan job's
``combined_hash`` output and ``deploy-plan`` artifact feed the apply job.
Every job runs as a different runner: its own clone of the repository, its
own ``$RUNNER_TEMP`` (kubeconfig and state directory) and an empty local
image store, so plan and apply share nothing but the cluster (the shared
state of ``state="cluster"``) and the artifact. ``docs/ci.md`` includes the
same file, so the documented commands cannot drift from the CLI.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from tests.acceptance.test_deploy_ref import (
    RefBackend,
    content_id,
    git,
    make_ref_shop,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / "examples" / "ci" / "github-actions-deploy.yml"


def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def steps(job: str) -> list[dict[str, Any]]:
    return list(workflow()["jobs"][job]["steps"])


def script(job: str, name: str) -> str:
    return next(step["run"] for step in steps(job) if step.get("name") == name)


def piceli_command(job: str) -> str:
    """The one ``piceli deploy`` line of a job (without redirections)."""
    lines = [
        line.strip()
        for step in steps(job)
        for line in str(step.get("run", "")).splitlines()
        if "piceli deploy" in line
    ]
    assert len(lines) == 1, lines
    command = lines[0].split(" > ")[0]
    assert command.startswith("uv run --frozen piceli deploy ")
    return command.removeprefix("uv run --frozen ")


def run_command(command: str, env: dict[str, str]) -> tuple[int, list[dict[str, Any]]]:
    argv = shlex.split(command)
    assert argv[:2] == ["piceli", "deploy"]
    expanded = [re.sub(r"\$(\w+)", lambda m: env[m[1]], item) for item in argv[1:]]
    result = CliRunner().invoke(cli, expanded, env=env)
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return result.exit_code, lines


@pytest.fixture
def ci_shop(tmp_path, monkeypatch):
    yield from make_ref_shop(tmp_path, monkeypatch)


def test_workflow_shape_keeps_approval_and_secrets_safe() -> None:
    document = workflow()
    assert (
        WORKFLOW.parent.name == "ci"
        and not (ROOT / ".github" / "workflows" / WORKFLOW.name).exists()
    )
    assert document["concurrency"]["cancel-in-progress"] is False
    jobs = document["jobs"]
    assert jobs["apply"]["needs"] == "plan"
    assert jobs["apply"]["environment"] == jobs["resume"]["environment"]
    assert "environment" not in jobs["plan"]
    text = WORKFLOW.read_text()
    assert "--auto-approve" not in text and "--allow-exec" not in text
    assert "set -x" not in text
    # The secret only ever reaches a file; nothing echoes or cats it.
    uses = [line for line in text.splitlines() if "KUBECONFIG_CONTENT" in line]
    assert all(
        "printf '%s\\n' \"$KUBECONFIG_CONTENT\" >" in line
        or "KUBECONFIG_CONTENT: ${{ secrets.DEPLOY_KUBECONFIG }}" in line
        for line in uses
    ), uses
    assert 'cat "$RUNNER_TEMP/deploy.kubeconfig"' not in text
    plan = script("plan", "Plan the pushed commit")
    assert "jq -c 'select(.event == \"result\")' plan.jsonl > plan.json" in plan
    assert "jq -r .combined_hash plan.json" in plan
    assert "combined_hash=" in plan
    assert "--out deploy-plan.json" in plan
    apply = next(
        step
        for step in jobs["apply"]["steps"]
        if step.get("name") == "Apply the approved plan"
    )
    assert apply["env"]["COMBINED_HASH"] == "${{ needs.plan.outputs.combined_hash }}"
    assert "--apply deploy-plan.json" in apply["run"]
    download = [
        step
        for step in jobs["apply"]["steps"]
        if str(step.get("uses", "")).startswith("actions/download-artifact")
    ]
    assert download and download[0]["with"]["name"] == "deploy-plan"
    # Nothing is shared through the runner: no fixed state directory.
    assert "DEPLOY_STATE_DIR: " not in text and "/var/lib/" not in text


def shared_state(repo: Path) -> str:
    """The pipeline with ``state="cluster"``, committed (``--ref`` checks it)."""
    module = repo / "deploy" / "app.py"
    module.write_text(
        module.read_text().replace(
            'state_dir=os.environ.get("DEPLOY_STATE_DIR", "state"),',
            'state_dir=os.environ.get("DEPLOY_STATE_DIR", "state"), state="cluster",',
        )
    )
    git(repo, "commit", "-q", "-am", "shared state")
    return git(repo, "rev-parse", "HEAD")


class Job:
    """One job on its own runner: a clone, a runner temp dir, no local images."""

    def __init__(self, tmp_path: Path, repo: Path, name: str, kubeconfig: str) -> None:
        self.root = tmp_path / f"runner-{name}"
        self.temp = self.root / "temp"
        self.temp.mkdir(parents=True)
        self.checkout = self.root / "checkout"
        subprocess.run(
            ["git", "clone", "-q", str(repo), str(self.checkout)],
            check=True,
            capture_output=True,
        )
        github_env = self.root / "github-env"
        github_env.write_text("")
        # The kubeconfig step, run as written: a 0600 file, exported by path.
        subprocess.run(
            [
                "bash",
                "-e",
                "-c",
                script(name, "Write the kubeconfig (mode 0600, never printed)"),
            ],
            check=True,
            env={
                "PATH": os.environ["PATH"],
                "KUBECONFIG_CONTENT": kubeconfig,
                "RUNNER_TEMP": str(self.temp),
                "GITHUB_ENV": str(github_env),
            },
            capture_output=True,
        )
        self.exported = dict(
            line.split("=", 1) for line in github_env.read_text().splitlines() if line
        )

    def run(
        self, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
    ) -> tuple[int, list[dict[str, Any]]]:
        import sys

        RefBackend.local = set()  # another machine: no local images
        for name in [n for n in sys.modules if n.startswith("_piceli_render_")]:
            del sys.modules[name]  # another process
        monkeypatch.chdir(self.checkout)
        job = next(
            name
            for name in ("plan", "apply", "resume")
            if self.root.name == f"runner-{name}"
        )
        return run_command(piceli_command(job), {**env, **self.exported})


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_recipe_runs_each_job_on_another_runner(ci_shop, tmp_path, monkeypatch):
    api, repo = ci_shop
    kubeconfig = (repo / "deploy" / "kubeconfig").read_text()
    sha = shared_state(repo)
    env = {**{k: str(v) for k, v in workflow()["env"].items()}, "GITHUB_SHA": sha}
    jobs = {
        name: Job(tmp_path, repo, name, kubeconfig)
        for name in ("plan", "apply", "resume")
    }
    exported = jobs["plan"].exported
    written = Path(exported["DEPLOY_KUBECONFIG"])
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert written.read_text().strip() == kubeconfig.strip()
    assert exported["DEPLOY_STATE_DIR"] == str(jobs["plan"].temp / "piceli-state")
    (repo / "deploy" / "kubeconfig").unlink()  # only the CI copies are used

    # plan job
    code, lines = jobs["plan"].run(monkeypatch, env)
    assert code == 0, lines
    result = next(line for line in lines if line.get("event") == "result")
    assert result["state"] == "planned" and result["refs"] == {"shop": sha}
    assert RefBackend.built == [] and ("Deployment", "web") not in api.objects
    plan_file = jobs["plan"].checkout / "deploy-plan.json"
    assert json.loads(plan_file.read_text())["refs"] == {"shop": sha}

    # The deploy-plan artifact reaches the apply job's workspace, nothing else.
    shutil.copy(plan_file, jobs["apply"].checkout / "deploy-plan.json")
    shutil.rmtree(jobs["plan"].root)  # the plan runner is gone

    # apply job (after approval): the delivery fails once …
    RefBackend.fail_delivery.append("registry-unreachable")
    env["COMBINED_HASH"] = result["combined_hash"]
    code, lines = jobs["apply"].run(monkeypatch, env)
    assert code == 1 and lines[-1]["stage"] == "deliver", lines
    shutil.rmtree(jobs["apply"].root)  # … and that runner is gone too

    # … and the resume job, on a third runner, continues the same run with
    # the same commit (it rebuilds: the image was only on the apply runner).
    code, lines = jobs["resume"].run(monkeypatch, env)
    assert code == 0 and lines[-1]["state"] == "ready", lines
    assert RefBackend.built == ["v1\n", "v1\n"]
    assert api.objects[("Deployment", "web")]["spec"]["template"]["spec"]["containers"][
        0
    ]["image"].startswith("registry.example:5000/shop/web@sha256:")
    state = jobs["resume"].exported["DEPLOY_STATE_DIR"]
    run = json.loads(sorted((Path(state) / "runs").glob("*.json"))[-1].read_text())
    assert run["refs"]["shop"]["commit"] == sha
    assert content_id("v1\n") in json.dumps(run)
    assert ("Lease", "piceli-lock-shop") in api.objects
    assert "holderIdentity" not in api.objects[("Lease", "piceli-lock-shop")]["spec"]
