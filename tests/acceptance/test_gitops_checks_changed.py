"""Acceptance: a GitOps controller verifies a release when only its checks changed.

A local bare repository holds the pipeline; the fake API holds the branch's
namespace; ``env_up`` and the default check runner are real (a ``python``
check reads its outcome from the environment). A push that changes, adds
or removes a check but no manifest re-runs the checks against the running
release: nothing is applied or rolled back, the status reports a
verification, and failing checks mark the environment degraded until a
passing verification (``piceli gitops sync``) clears it.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from piceli.gitops.config import ControllerConfig
from piceli.gitops.controller import Controller
from piceli.gitops.ports import DefaultPorts
from piceli.gitops.repo import GitRemote
from piceli.gitops.state import DirectoryChannel, sync_request
from piceli.k8s.cli.env_push import configmap_name
from piceli.testing import TYPES, FakeAPI, manifest, serve, write_kubeconfig
from tests.unit.test_gitops_controller import Repo

BRANCH = "wp-login"
BRANCH_NS = "shop-wp-login"
DIGEST = "sha256:" + "5" * 64

PIPELINE = """
from piceli import App, Build, Checks, EnvConfig, NodeLoopbackRegistry, Pipeline, Target

images = Build.spec("build.toml")
app = App("shop")
app.deployment("api", image=images["api"], ports=[8080])

pipeline = Pipeline(
    app,
    Target.kubeconfig(
        "kubeconfig", context="author", namespace="shop", transport="loopback-http",
        nodes={"primary": "node-a"},
    ),
    build=images,
    deliver=NodeLoopbackRegistry(),
    checks=[CHECKS],
    rollback_on_failed_checks=True,
    execution={"max_seconds": 30, "readiness_seconds": 1, "poll_seconds": 0.05},
    envs=EnvConfig(prefix="shop-", branches=["wp-*"], auto_approve=True),
)
"""

PROBE = """
import os
from pathlib import Path


def _probe(name):
    with Path(os.environ["PROBE_LOG"]).open("a") as log:
        log.write(name + "\\n")
    mode = os.environ.get("PROBE_MODE", "pass")
    return mode == "pass" or f"GET /login returned 500 ({name})"


def first(context):
    return _probe("first")


def second(context):
    return _probe("second")
"""


def push(repo: Repo, api: FakeAPI, checks: list[str]) -> str:
    """Commit the pipeline with ``checks`` on the branch, as its pushed images."""
    repo.git("checkout", "--quiet", "-B", BRANCH, "main")
    declared = ", ".join(f'Checks.python("checks.py:{name}")' for name in checks)
    (repo.work / "deploy" / "app.py").write_text(
        textwrap.dedent(PIPELINE).replace("[CHECKS]", f"[{declared}]")
    )
    (repo.work / "deploy" / "checks.py").write_text(textwrap.dedent(PROBE))
    repo.git("add", "-A")
    repo.git("commit", "--quiet", "-m", "checks: " + ",".join(checks))
    sha = repo.git("rev-parse", "HEAD")
    repo.git("push", "--quiet", "--force", "origin", BRANCH)
    repo.git("checkout", "--quiet", "main")
    pushed = manifest("ConfigMap", configmap_name(BRANCH))
    pushed["data"] = {"images": json.dumps({"api": {"digest": DIGEST}}), "commit": sha}
    api.put(pushed)
    return sha


def deployment(api: FakeAPI) -> dict[str, Any]:
    found = api.objects[("Deployment", "api")]
    return {
        "image": found["spec"]["template"]["spec"]["containers"][0]["image"],
        "resourceVersion": found["metadata"]["resourceVersion"],
    }


def test_a_changed_check_verifies_the_branch_and_a_failure_degrades_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_log = tmp_path / "probe.log"
    monkeypatch.setenv("PROBE_LOG", str(probe_log))
    monkeypatch.setenv("PROBE_MODE", "pass")
    repo = Repo(tmp_path)
    api = FakeAPI(
        types={**TYPES, "resourcequotas": ("v1", "ResourceQuota", True)},
        namespace=BRANCH_NS,
    )
    del api.objects[("Namespace", BRANCH_NS)]
    api.add_node("node-a")
    first = push(repo, api, ["first"])
    with serve(api) as (api, url):
        kubeconfig = write_kubeconfig(
            url, tmp_path / "controller.kubeconfig", context="fake"
        )
        config = ControllerConfig(
            pipeline="deploy/app.py:pipeline",
            repo=str(repo.remote),
            branches=("main", "wp-*"),
        )
        state = tmp_path / "state"
        channel = DirectoryChannel(state)
        controller = Controller(
            config,
            state_dir=state,
            source=GitRemote(config.repo, state / "mirror"),
            ports=DefaultPorts(
                Path(kubeconfig),
                "fake",
                state,
                namespace="piceli-system",
                config=config,
                transport="loopback-http",
            ),
            channel=channel,
        )
        entry = controller.poll_once()["envs"][BRANCH]
        assert entry["state"] == "deployed", json.dumps(entry)
        assert entry["deployed_commit"] == first
        assert entry["last_action"] == "deployed" and entry["health"] == "healthy"
        assert entry["verification"] is None
        assert probe_log.read_text().split() == ["first"]
        rolled_out = deployment(api)

        # A check added, no manifest changed: a verification, not a deploy.
        second = push(repo, api, ["first", "second"])
        entry = controller.poll_once()["envs"][BRANCH]
        assert entry["state"] == "deployed", json.dumps(entry)
        assert entry["deployed_commit"] == second
        assert entry["last_action"] == "verified" and entry["health"] == "healthy"
        verification = entry["verification"]
        assert verification["state"] == "verified"
        assert verification["trigger"] == "checks-changed"
        assert verification["rolled"] == []
        assert verification["checks_hash"].startswith("sha256:")
        assert probe_log.read_text().split() == ["first", "first", "second"]
        assert deployment(api) == rolled_out

        # A poll without a push runs nothing.
        controller.poll_once()
        assert probe_log.read_text().split() == ["first", "first", "second"]

        # A changed check that fails: degraded, kept running, not rolled back.
        monkeypatch.setenv("PROBE_MODE", "fail")
        third = push(repo, api, ["second"])
        entry = controller.poll_once()["envs"][BRANCH]
        assert entry["state"] == "deployed", json.dumps(entry)
        assert entry["deployed_commit"] == third
        assert entry["health"] == "degraded"
        assert entry["reason"] == "pipeline-checks-failed"
        assert entry["last_action"] == "verified"
        verification = entry["verification"]
        assert verification["state"] == "failed"
        assert verification["trigger"] == "checks-changed"
        assert verification["rolled"] == []
        assert [item["check"] for item in verification["failed"]] == ["python-second"]
        assert verification["failed"][0]["code"] == "check-failed"
        assert "GET /login returned 500" in verification["failed"][0]["detail"]
        assert entry["attempts"] == 0 and entry["next_attempt_at"] is None
        assert deployment(api) == rolled_out
        runs = len(probe_log.read_text().split())

        # No retry loop: the next polls leave it degraded without re-running.
        controller.poll_once()
        assert len(probe_log.read_text().split()) == runs
        assert controller.status()["envs"][BRANCH]["health"] == "degraded"

        # A passing verification (gitops sync) clears it.
        monkeypatch.setenv("PROBE_MODE", "pass")
        channel.add_request(*sync_request(BRANCH))
        entry = controller.poll_once()["envs"][BRANCH]
        assert entry["state"] == "deployed", json.dumps(entry)
        assert entry["health"] == "healthy" and entry["reason"] is None
        assert entry["verification"]["state"] == "verified"
        assert entry["verification"]["trigger"] == "unverified"
        assert len(probe_log.read_text().split()) == runs + 1
        assert deployment(api) == rolled_out
