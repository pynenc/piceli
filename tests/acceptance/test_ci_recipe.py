"""Acceptance: the CI recipe (``examples/ci/github-actions-deploy.yml``) works.

The test reads the sample workflow and runs, in order, its ``piceli deploy``
command lines (plan the pushed commit, apply the approved hash, resume) and
its kubeconfig step (a real ``bash``), against the fake API server and a git
repository, the way the jobs chain them: the plan job's ``combined_hash``
output feeds the apply job. ``docs/ci.md`` includes the same file, so the
documented commands cannot drift from the CLI.
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
    assert (
        jobs["apply"]["steps"][3]["env"]["COMBINED_HASH"]
        == "${{ needs.plan.outputs.combined_hash }}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_recipe_commands_plan_approve_and_resume(ci_shop, tmp_path, monkeypatch):
    api, repo = ci_shop
    kubeconfig = (repo / "deploy" / "kubeconfig").read_text()
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    github_env = tmp_path / "github-env"
    github_env.write_text("")

    # The kubeconfig step, run as written: a 0600 file, exported by path.
    subprocess.run(
        [
            "bash",
            "-e",
            "-c",
            script("plan", "Write the kubeconfig (mode 0600, never printed)"),
        ],
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "KUBECONFIG_CONTENT": kubeconfig,
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_ENV": str(github_env),
        },
        capture_output=True,
    )
    exported = dict(
        line.split("=", 1) for line in github_env.read_text().splitlines() if line
    )
    written = Path(exported["DEPLOY_KUBECONFIG"])
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert written.read_text().strip() == kubeconfig.strip()
    (repo / "deploy" / "kubeconfig").unlink()  # only the CI copy is used

    sha = git(repo, "rev-parse", "HEAD")
    env = {
        **{k: str(v) for k, v in workflow()["env"].items()},
        **exported,
        "DEPLOY_STATE_DIR": str(tmp_path / "state"),
        "GITHUB_SHA": sha,
    }
    monkeypatch.chdir(repo)

    # plan job
    code, lines = run_command(piceli_command("plan"), env)
    assert code == 0, lines
    result = next(line for line in lines if line.get("event") == "result")
    assert result["state"] == "planned" and result["refs"] == {"shop": sha}
    assert RefBackend.built == [] and ("Deployment", "web") not in api.objects

    # apply job (after approval): the delivery fails once …
    RefBackend.fail_delivery.append("registry-unreachable")
    env["COMBINED_HASH"] = result["combined_hash"]
    code, lines = run_command(piceli_command("apply"), env)
    assert code == 1 and lines[-1]["stage"] == "deliver", lines

    # … and the resume job continues the same run with the same commit.
    code, lines = run_command(piceli_command("resume"), env)
    assert code == 0 and lines[-1]["state"] == "ready", lines
    assert RefBackend.built == ["v1\n"]
    assert api.objects[("Deployment", "web")]["spec"]["template"]["spec"]["containers"][
        0
    ]["image"].startswith("registry.example:5000/shop/web@sha256:")
    run = json.loads(
        sorted((tmp_path / "state" / "runs").glob("*.json"))[-1].read_text()
    )
    assert run["refs"]["shop"]["commit"] == sha
    assert content_id("v1\n") in json.dumps(run)
