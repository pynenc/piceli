"""Opt-in: an apply whose pods cannot start fails at once on kind, with causes.

``readiness_seconds`` is 240: only the fail-fast path (``apply-crashloop``)
finishes these applies within the asserted time. One Deployment runs an image
that prints a message and exits 1; another names an image that does not exist.
Uses the explicit kubeconfig and context from ``PICELI_KIND_KUBECONFIG`` and
``PICELI_KIND_CONTEXT`` only.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from kind_support import cli, requires_kind, write_spec
from typer.testing import CliRunner

from piceli.k8s.cli.release import app

pytestmark = [requires_kind]

CRASHING = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent


def build(ctx):
    labels = {"app": "crasher"}
    crasher = ResourceIntent.from_manifest({
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "crasher", "namespace": ctx.namespace},
        "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                 "template": {"metadata": {"labels": labels}, "spec": {
                     "containers": [{"name": "app", "image": ctx.image("web"),
                                     "command": ["sh", "-c", "echo starting; "
                                                 "echo password=do-not-print; "
                                                 "echo fatal: settings file missing; "
                                                 "exit 1"],
                                     # Never ready: without a probe a container
                                     # counts as ready for the moment it runs.
                                     "readinessProbe": {"exec": {"command": [
                                         "cat", "/nonexistent"]},
                                         "periodSeconds": 1}}]}}},
    })
    return DeploymentComposition((DeploymentComponent("app", (crasher,)),))
"""

MISSING = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent

IMAGE = "registry.invalid/example/missing@sha256:" + "0" * 64


def build(ctx):
    labels = {"app": "missing"}
    missing = ResourceIntent.from_manifest({
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "missing", "namespace": ctx.namespace},
        "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                 "template": {"metadata": {"labels": labels}, "spec": {
                     "containers": [{"name": "app", "image": IMAGE}]}}},
    })
    return DeploymentComposition((DeploymentComponent("app", (missing,)),))
"""


def _apply(tmp_path: Path, namespace: str, source: str) -> tuple[Any, float]:
    (tmp_path / "diagnosed.py").write_text(source)
    spec = write_spec(tmp_path, namespace, "diagnosed.py", owner="m5-diagnose")
    code, planned = cli(spec, "plan")
    assert code == 0, planned
    started = time.monotonic()
    result = CliRunner().invoke(
        app, ["apply", "--spec", str(spec), "--approve", planned["plan_hash"]]
    )
    return result, time.monotonic() - started


def test_crashing_container_fails_fast_with_its_log(
    tmp_path: Path, kind_namespace: str
) -> None:
    result, elapsed = _apply(tmp_path, kind_namespace, CRASHING)
    assert result.exit_code == 1, result.stdout + result.stderr
    assert elapsed < 150, elapsed  # the image pull; readiness allows 240 s
    applied = json.loads(result.stdout)
    assert applied["reason"] == "apply-crashloop", applied
    [workload] = applied["diagnosis"]["workloads"]
    assert workload["name"] == "crasher"
    [cause] = workload["causes"]
    assert cause["container"] == "app"
    assert cause["exit_code"] == 1
    assert cause["reason"] in {"CrashLoopBackOff", "Error"}
    assert cause["logs"][-1] == "fatal: settings file missing"
    assert "password=[REDACTED]" in cause["logs"]
    assert "do-not-print" not in result.stdout + result.stderr
    assert 'crasher  app  exit 1  "fatal: settings file missing"' in result.stderr


def test_missing_image_fails_fast_naming_the_pull(
    tmp_path: Path, kind_namespace: str
) -> None:
    result, elapsed = _apply(tmp_path, kind_namespace, MISSING)
    assert result.exit_code == 1, result.stdout + result.stderr
    assert elapsed < 90, elapsed
    applied = json.loads(result.stdout)
    assert applied["reason"] == "apply-crashloop", applied
    [cause] = applied["diagnosis"]["workloads"][0]["causes"]
    assert cause["reason"] in {"ErrImagePull", "ImagePullBackOff"}
    assert cause["exit_code"] is None and cause["logs"] == []
    assert "registry.invalid" in json.dumps(cause)
    assert "missing  app" in result.stderr
