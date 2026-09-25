"""Opt-in: ``piceli deploy --ref`` deploys the commit, not the dirty working tree.

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-ref --kubeconfig /tmp/ref.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/ref.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-ref \\
    PICELI_KIND_NODE=piceli-ref-control-plane \\
      uv run pytest tests/integration/test_deploy_ref_kind.py

Needs ``docker`` with ``buildx`` and ``git``; never reads the ambient
kubeconfig or the developer's git configuration. In a temporary git
repository it commits an image whose container prints ``committed``, then
edits the file to ``uncommitted`` without committing, and deploys with
``--ref main`` (plan, then the printed approval command) to a unique
namespace through ``NodeImport``. The running pod prints ``committed``, the
real build receipt records the commit as clean, and no worktree is left.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest

from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
NODE = os.environ.get("PICELI_KIND_NODE", "")
BUSYBOX = (
    "docker.io/library/busybox:1.36"
    "@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
)
PLATFORM = (
    "linux/arm64" if platform.machine() in {"arm64", "aarch64"} else "linux/amd64"
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (
            KUBECONFIG
            and CONTEXT
            and NODE
            and shutil.which("docker")
            and shutil.which("git")
        ),
        reason="set PICELI_KIND_KUBECONFIG, PICELI_KIND_CONTEXT and PICELI_KIND_NODE",
    ),
]

DOCKERFILE = """\
ARG BUILDER
FROM ${BUILDER} AS web
COPY message.txt /message
CMD ["sh", "-c", "cat /message && exec sleep 3600"]
"""

PIPELINE = f"""
import os

from piceli import App, Build, NodeImport, Pipeline, Target

target = Target.kubeconfig(
    os.environ["REF_KUBECONFIG"],
    context=os.environ["REF_CONTEXT"],
    namespace=os.environ["REF_NAMESPACE"],
    nodes={{"primary": (os.environ["REF_NODE"], None)}},
)
app = App("refdemo")
images = Build.dockerfile(
    "Dockerfile",
    builder="{BUSYBOX}",
    targets=["web"],
    context="app",
    include=["Dockerfile", "message.txt"],
    sources="inputs.toml",
    source="app",
    platform="{PLATFORM}",
)
app.deployment("web", image=images["web"])
pipeline = Pipeline(
    app, target, build=images, deliver=NodeImport(),
    state_dir=os.environ["REF_STATE_DIR"],
    execution={{"readiness_seconds": 180, "max_seconds": 600}},
)
"""


@pytest.fixture
def namespace():
    from kubernetes.client import CoreV1Api

    name = "ref-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    api = CoreV1Api(client)
    api.create_namespace({"metadata": {"name": name}})
    try:
        yield name, api
    finally:
        api.delete_namespace(name)
        client.close()


def _git_env(tmp_path: Path) -> dict[str, str]:
    config = tmp_path / "gitconfig"
    config.write_text("")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"KUBECONFIG", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
    }
    env.update(
        GIT_CONFIG_GLOBAL=str(config),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Example",
        GIT_AUTHOR_EMAIL="example@example.com",
        GIT_COMMITTER_NAME="Example",
        GIT_COMMITTER_EMAIL="example@example.com",
    )
    return env


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def test_ref_deploys_the_committed_state(namespace, tmp_path) -> None:
    name, api = namespace
    env = _git_env(tmp_path)
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "deploy").mkdir()
    (repo / "app" / "Dockerfile").write_text(DOCKERFILE)
    (repo / "app" / "message.txt").write_text("committed\n")
    (repo / "deploy" / "app.py").write_text(textwrap.dedent(PIPELINE))
    (repo / "deploy" / "inputs.toml").write_text(
        '[[source]]\nname = "app"\npath = ".."\n'
    )
    (repo / ".gitignore").write_text("__pycache__/\n")
    _git(repo, env, "init", "-q", "-b", "main")
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-q", "-m", "committed")
    sha = _git(repo, env, "rev-parse", "HEAD")
    (repo / "app" / "message.txt").write_text("uncommitted\n")  # never committed

    state = tmp_path / "state"
    env.update(
        REF_KUBECONFIG=KUBECONFIG,
        REF_CONTEXT=CONTEXT,
        REF_NAMESPACE=name,
        REF_NODE=NODE,
        REF_STATE_DIR=str(state),
    )
    target = f"{repo / 'deploy' / 'app.py'}:pipeline"

    def deploy(*args: str) -> tuple[int, list[dict], str]:
        result = subprocess.run(
            [sys.executable, "-m", "piceli", "deploy", target, *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=800,
        )
        lines = [json.loads(x) for x in result.stdout.splitlines() if x.strip()]
        return result.returncode, lines, result.stderr

    code, events, stderr = deploy("--ref", "main", "--plan", "--json")
    assert code == 0, stderr
    planned = events[-1]
    assert planned["refs"] == {"app": sha}
    assert planned["stages"]["inputs"]["sources"]["app"]["dirty"] is False
    approve = next(line.strip() for line in stderr.splitlines() if "--approve" in line)
    assert f"--ref app={sha} --approve {planned['combined_hash']}" in approve

    code, events, stderr = deploy(
        "--ref", f"app={sha}", "--approve", planned["combined_hash"], "--json"
    )
    assert code == 0, stderr
    assert events[-1]["state"] == "ready", events[-1]

    receipt = json.loads((state / "builds" / "web" / "receipt.json").read_text())
    [source] = receipt["sources"]
    assert (source["commit"], source["dirty"], source["repository"]) == (
        sha,
        False,
        "repo",
    )
    assert receipt["refs"] == {"app": {"ref": sha, "commit": sha}}
    assert (repo / "app" / "message.txt").read_text() == "uncommitted\n"
    assert _git(repo, env, "worktree", "list", "--porcelain").count("worktree ") == 1

    deadline = time.monotonic() + 120
    logs = ""
    while time.monotonic() < deadline:
        pods = api.list_namespaced_pod(name).items
        running = [p for p in pods if p.status.phase == "Running"]
        if running:
            logs = api.read_namespaced_pod_log(
                running[0].metadata.name, name, _preload_content=False
            ).data.decode()
            if logs.strip():
                break
        time.sleep(2)
    assert logs.strip() == "committed", logs
