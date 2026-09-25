"""Acceptance: a pipeline's checks through the DEFAULT check runner.

Builds and deliveries are the fake backend of ``test_deploy_pipeline``; the
checks stage is not replaced: ``piceli.checks.run_checks`` runs a ``python``
check that reads the deployed Deployment through the check context's own API
client (the pipeline target's kubeconfig, context and transport) from the
in-process fake API.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from piceli.pipeline.backend import Backend
from tests.acceptance.test_deploy_pipeline import FakeBackend, deploy, image

PROBE = """
from pathlib import Path

from kubernetes.client import AppsV1Api

HERE = Path(__file__).parent


def web_is_deployed(context):
    mode = (HERE / "mode").read_text().strip()
    if mode == "interrupt":
        (HERE / "mode").write_text("fail")
        raise KeyboardInterrupt
    deployment = AppsV1Api(context.api_client()).read_namespaced_deployment(
        "web", context.namespace
    )
    image = deployment.spec.template.spec.containers[0].image
    (HERE / "seen.json").write_text(
        '{"release": "%s", "image": "%s", "web": "%s"}'
        % (context.release, image, context.images.get("web"))
    )
    return mode == "pass" or f"mode is {mode}"
"""


@pytest.fixture
def probed(shop, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path]:
    api, tmp_path = shop
    monkeypatch.setattr(FakeBackend, "check_runner", Backend.check_runner)
    (tmp_path / "probe.py").write_text(textwrap.dedent(PROBE))
    (tmp_path / "mode").write_text("pass")
    text = (tmp_path / "app.py").read_text()
    (tmp_path / "app.py").write_text(
        text.replace(
            "CHECKS = []",
            'from piceli import Checks\nCHECKS = [Checks.python("probe.py:web_is_deployed")]',
        )
    )
    return api, tmp_path


def test_checks_run_with_the_default_runner_and_the_target_context(probed) -> None:
    api, tmp_path = probed
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    final = events[-1]
    assert final["state"] == "ready" and final["stages"]["checks"] == "done"
    seen = json.loads((tmp_path / "seen.json").read_text())
    assert seen == {"release": final["release"], "image": image(api), "web": image(api)}
    checks = [e for e in events if e.get("stage") == "checks" and e["state"] == "done"]
    assert checks[-1]["detail"]["results"][0]["passed"] is True
    # The build log is shown relative to the working directory or state dir.
    line = next(x for x in result.stderr.splitlines() if "(log: " in x)
    assert line.endswith("(log: <state_dir>/builds/shop/build.log)"), line


def test_resume_at_failed_checks_rolls_back_to_the_previous_release(
    probed,
) -> None:
    api, tmp_path = probed
    code, _, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    good = image(api)

    # A new release whose checks are interrupted, then fail on resume.
    (tmp_path / "src" / "main.txt").write_text("broken\n")
    FakeBackend.image_id = "sha256:" + "3" * 64
    (tmp_path / "mode").write_text("interrupt")
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 1, result.stdout + result.stderr
    assert events[-1]["state"] == "interrupted"
    assert image(api) != good

    code, events, result = deploy(tmp_path, "--resume", "--json")
    assert code == 1, result.stdout + result.stderr
    final = events[-1]
    assert final["reason"] == "pipeline-checks-failed", final
    assert final["state"] == "rolled-back"
    assert image(api) == good
    run = json.loads(
        sorted((tmp_path / "state" / "runs").glob("*.json"))[-1].read_text()
    )
    rollback = run["stages"]["checks"]["output"]["rollback"]
    assert rollback["state"] == "ready"
