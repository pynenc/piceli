"""Acceptance: ``piceli deploy --ref`` builds a commit, not the working tree.

A git repository holds the app's source (``src/main.txt``) and its pipeline
(``deploy/``). The build backend is fake (the image id is the digest of the
staged ``src/main.txt``), so every test can tell which content was built;
source identity, worktrees, planning, apply and resume are real, against the
in-process fake API server.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from piceli.pipeline.backend import Backend
from tests.acceptance.fake_api import TARGET, serve
from tests.acceptance.test_deploy_pipeline import SCHEMA, FakeBackend, image

BUILD_TOML = """
revision = "piceli.build-spec.v1"
name = "shop"
inputs = "inputs.toml"

[builder]
image = "docker.io/library/busybox"
digest = "sha256:{builder}"

[build]
platforms = ["linux/amd64"]
command = ["true"]

[context.src]
source = "shop"
include = ["src/*.txt"]

[[output.image]]
name = "web"
repository = "example/web"
tag = "{{image_id:12}}"
base = {{ image = "docker.io/library/busybox", digest = "sha256:{base}" }}
""".format(builder="b" * 64, base="c" * 64)

INPUTS_TOML = """
[[source]]
name = "shop"
path = ".."
"""

PIPELINE = f"""
import os

from piceli import App, Build, Pipeline, Registry, Target

target = Target.kubeconfig(
    os.environ.get("DEPLOY_KUBECONFIG", "kubeconfig"),
    context="fake",
    namespace="{TARGET.namespace}",
    transport="loopback-http",
)
app = App("shop")
images = Build.spec("build.toml")
app.deployment("web", image=images["web"], ports=[8080])

pipeline = Pipeline(
    app, target, build=images, deliver=Registry("oci://registry.example:5000/shop"),
    state_dir=os.environ.get("DEPLOY_STATE_DIR", "state"),
    execution={{"max_seconds": 30, "readiness_seconds": 1, "poll_seconds": 0.05}},
)
"""


def content_id(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


class RefBackend(FakeBackend):
    """The fake backend, but sources are identified by git and the image id
    is the digest of the staged ``src/main.txt``."""

    built: list[str] = []
    interrupt: list[bool] = []

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.built, cls.interrupt = [], []

    def record_sources(self, inputs: Any, lock: Any) -> dict[str, Any]:
        return Backend.record_sources(self, inputs, lock)

    def build(self, spec, grant, output_dir, *, inputs, lock, log, progress):  # type: ignore[no-untyped-def]
        if self.interrupt:
            raise KeyboardInterrupt
        root = spec.plan(inputs).context_roots["src"]
        text = (root / "src" / "main.txt").read_text()
        type(self).built.append(text)
        type(self).image_id = content_id(text)
        return super().build(
            spec,
            grant,
            output_dir,
            inputs=inputs,
            lock=lock,
            log=log,
            progress=progress,
        )


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def isolate_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Git in tests never reads the developer's configuration or identity."""
    config = tmp_path / "gitconfig"
    config.write_text("")
    for key, value in {
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Example",
        "GIT_AUTHOR_EMAIL": "example@example.com",
        "GIT_COMMITTER_NAME": "Example",
        "GIT_COMMITTER_EMAIL": "example@example.com",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(key, raising=False)


def make_repo(tmp_path: Path, url: str) -> Path:
    """``repo/``: ``src/main.txt`` = v1 and ``deploy/`` committed on ``main``."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "deploy").mkdir()
    (repo / "src" / "main.txt").write_text("v1\n")
    (repo / "deploy" / "build.toml").write_text(BUILD_TOML)
    (repo / "deploy" / "inputs.toml").write_text(INPUTS_TOML)
    (repo / "deploy" / "app.py").write_text(PIPELINE)
    (repo / ".gitignore").write_text("deploy/kubeconfig\ndeploy/state/\n__pycache__/\n")
    (repo / "deploy" / "kubeconfig").write_text(
        textwrap.dedent(
            f"""
            apiVersion: v1
            kind: Config
            current-context: must-not-be-used
            clusters: [{{name: fake, cluster: {{server: "{url}"}}}}]
            users: [{{name: nobody, user: {{}}}}]
            contexts:
            - {{name: fake, context: {{cluster: fake, user: nobody}}}}
            - {{name: must-not-be-used, context: {{cluster: fake, user: nobody}}}}
            """
        )
    )
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "v1")
    return repo


def make_ref_shop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Any, Path]]:
    isolate_git(tmp_path, monkeypatch)
    RefBackend.reset()
    monkeypatch.setattr("piceli.pipeline.runner.Backend", RefBackend)
    with serve() as (api, url):
        yield api, make_repo(tmp_path, url)


@pytest.fixture
def ref_shop(tmp_path, monkeypatch):
    yield from make_ref_shop(tmp_path, monkeypatch)


def deploy(repo: Path, *args: str) -> tuple[int, list[dict[str, Any]], Any]:
    import jsonschema

    result = CliRunner().invoke(
        cli, ["deploy", str(repo / "deploy" / "app.py:pipeline"), *args]
    )
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    for line in lines:
        jsonschema.validate(line, SCHEMA)
    return result.exit_code, lines, result


def worktrees(repo: Path) -> list[str]:
    return [
        line
        for line in git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]


def commit(repo: Path, text: str) -> str:
    (repo / "src" / "main.txt").write_text(text)
    git(repo, "commit", "-q", "-am", text.strip())
    return git(repo, "rev-parse", "HEAD")


def test_ref_deploys_the_commit_while_the_working_tree_is_dirty(ref_shop) -> None:
    api, repo = ref_shop
    first = git(repo, "rev-parse", "HEAD")
    (repo / "src" / "main.txt").write_text("uncommitted\n")

    # Without --ref the dirty checkout is refused (allow_dirty is false).
    code, events, _ = deploy(repo, "--plan", "--json")
    assert code == 2 and events[-1]["reason"] == "source-identity"

    code, events, result = deploy(repo, "--ref", "main", "--plan", "--json")
    assert code == 0, result.stdout + result.stderr
    planned = events[-1]
    inputs = planned["stages"]["inputs"]
    assert inputs["refs"] == {"shop": {"ref": "main", "commit": first}}
    assert inputs["sources"]["shop"] == {
        "commit": first,
        "dirty": False,
        "diff_sha256": None,
    }
    assert inputs["model"] == {"checked_against": "shop"}
    assert planned["refs"] == {"shop": first}
    # The approval command is pinned to the SHA, not to the branch.
    assert f"--ref shop={first} --approve {planned['combined_hash']}" in result.stderr
    assert len(worktrees(repo)) == 1  # the plan's worktree is gone

    code, events, result = deploy(
        repo, "--ref", f"shop={first}", "--approve", planned["combined_hash"], "--json"
    )
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["state"] == "ready"
    assert RefBackend.built == ["v1\n"]  # the committed content, not the edit
    manifest = "sha256:" + hashlib.sha256(content_id("v1\n").encode()).hexdigest()
    assert image(api) == f"registry.example:5000/shop/web@{manifest}"
    assert (repo / "src" / "main.txt").read_text() == "uncommitted\n"
    assert len(worktrees(repo)) == 1

    state = repo / "deploy" / "state"
    receipt = json.loads((state / "builds" / "shop" / "receipt.json").read_text())
    assert receipt["refs"] == {"shop": {"ref": first, "commit": first}}
    run = json.loads(sorted((state / "runs").glob("*.json"))[-1].read_text())
    assert run["refs"] == {"shop": {"ref": first, "commit": first}}
    assert run["stages"]["inputs"]["output"]["refs"]["shop"]["commit"] == first
    assert run["stages"]["inputs"]["output"]["sources"]["shop"]["dirty"] is False

    # The release records its sources (``piceli release status``).
    status = CliRunner().invoke(
        cli,
        ["release", "status", "--spec", str(repo / "deploy" / "app.py:pipeline")],
    )
    assert status.exit_code == 0, status.stdout + status.stderr
    releases = json.loads(status.stdout)["releases"]
    assert releases[-1]["provenance"] == {
        "sources": {"shop": {"commit": first, "dirty": False, "ref": first}}
    }


def test_a_branch_that_moved_after_the_plan_is_not_applied(ref_shop) -> None:
    api, repo = ref_shop
    code, events, _ = deploy(repo, "--ref", "main", "--plan", "--json")
    assert code == 0
    planned = events[-1]["combined_hash"]
    commit(repo, "v2\n")  # main moves between plan and approval

    code, events, result = deploy(repo, "--ref", "main", "--approve", planned)
    assert code == 2 and events[-1]["reason"] == "pipeline-plan-changed"
    # A --ref plan is not approved by a working-tree deploy either.
    code, events, result = deploy(repo, "--approve", planned)
    assert code == 2 and events[-1]["reason"] == "pipeline-plan-changed", result.stderr
    assert RefBackend.built == [] and ("Deployment", "web") not in api.objects
    assert len(worktrees(repo)) == 1


def test_resume_reuses_the_run_commits(ref_shop) -> None:
    api, repo = ref_shop
    first = git(repo, "rev-parse", "HEAD")
    RefBackend.fail_delivery.append("registry-unreachable")
    code, events, result = deploy(repo, "--ref", "main", "--auto-approve", "--json")
    assert code == 1, result.stdout + result.stderr
    assert events[-1]["stage"] == "deliver"
    assert len(worktrees(repo)) == 1  # removed after the failure too

    commit(repo, "v2\n")  # main moves; the run keeps its commit
    code, events, result = deploy(repo, "--resume", "--ref", "main")
    assert code == 2 and events[-1]["reason"] == "deploy-flags-conflict"
    code, events, result = deploy(repo, "--resume", "--json")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["state"] == "ready"
    assert RefBackend.built == ["v1\n"]
    manifest = "sha256:" + hashlib.sha256(content_id("v1\n").encode()).hexdigest()
    assert image(api).endswith(manifest)
    run = json.loads(
        sorted((repo / "deploy" / "state" / "runs").glob("*.json"))[-1].read_text()
    )
    assert run["refs"] == {"shop": {"ref": "main", "commit": first}}
    assert len(worktrees(repo)) == 1


def test_resume_after_an_interrupted_build_rebuilds_the_same_commit(ref_shop) -> None:
    api, repo = ref_shop
    RefBackend.interrupt.append(True)
    code, events, result = deploy(repo, "--ref", "main", "--auto-approve")
    assert code == 1, result.stdout + result.stderr
    assert "--resume" in result.stderr
    assert len(worktrees(repo)) == 1  # cleaned up after the interrupt
    RefBackend.interrupt.clear()
    (repo / "src" / "main.txt").write_text("uncommitted\n")
    code, events, result = deploy(repo, "--resume")
    assert code == 0, result.stdout + result.stderr
    assert RefBackend.built == ["v1\n"]


def test_the_model_must_match_the_commit(ref_shop) -> None:
    api, repo = ref_shop
    app = repo / "deploy" / "app.py"
    app.write_text(app.read_text() + "\n# an uncommitted edit\n")
    code, events, result = deploy(repo, "--ref", "main", "--plan")
    assert code == 2 and events[-1]["reason"] == "deploy-ref-model-differs"
    assert "deploy/app.py" in result.stderr
    # Once committed, the same model deploys from that commit.
    git(repo, "commit", "-q", "-am", "model")
    code, events, result = deploy(repo, "--ref", "main", "--plan")
    assert code == 0, result.stdout + result.stderr


def test_ref_refusals(ref_shop, monkeypatch) -> None:
    api, repo = ref_shop
    cases = [
        (("--ref", "other=main"), "deploy-ref-source-unknown"),
        (("--ref", "no-such-branch"), "deploy-ref-unknown"),
        (("--ref", "shop=main", "--ref", "main"), "deploy-ref-ambiguous"),
        (("--ref", "shop=main", "--ref", "shop=main"), "deploy-ref-invalid"),
        (("--ref", "main..HEAD"), "deploy-ref-invalid"),
        (("--ref", "-x"), "deploy-ref-invalid"),
    ]
    for args, reason in cases:
        code, events, result = deploy(repo, *args, "--plan")
        assert (code, events[-1]["reason"]) == (2, reason), (args, result.stderr)
    monkeypatch.setattr("piceli.pipeline.refs.shutil.which", lambda _name: None)
    code, events, _ = deploy(repo, "--ref", "main", "--plan")
    assert (code, events[-1]["reason"]) == (2, "git-unavailable")
    assert RefBackend.built == [] and len(worktrees(repo)) == 1


def test_a_bare_ref_needs_one_repository(ref_shop, tmp_path) -> None:
    api, repo = ref_shop
    other = tmp_path / "lib"
    other.mkdir()
    (other / "lib.txt").write_text("lib\n")
    git(other, "init", "-q", "-b", "main")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "lib")
    (repo / "deploy" / "inputs.toml").write_text(
        INPUTS_TOML + f'\n[[source]]\nname = "lib"\npath = "{other}"\n'
    )
    git(repo, "commit", "-q", "-am", "two sources")
    code, events, _ = deploy(repo, "--ref", "main", "--plan")
    assert (code, events[-1]["reason"]) == (2, "deploy-ref-ambiguous")
    # Pin one source; the other is read from its (clean) checkout on disk.
    code, events, result = deploy(repo, "--ref", "shop=main", "--plan", "--json")
    assert code == 0, result.stdout + result.stderr
    assert set(events[-1]["stages"]["inputs"]["refs"]) == {"shop"}
    assert set(events[-1]["stages"]["inputs"]["sources"]) == {"shop", "lib"}
