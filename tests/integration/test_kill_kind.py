"""Opt-in: ``release apply`` and ``release rollback`` killed mid-rollout (kind).

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-m2 --kubeconfig /tmp/m2.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/m2.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-m2 \\
      uv run pytest tests/integration/test_kill_kind.py

The CLI runs in its own process and is SIGKILLed as soon as its Deployment
write is visible in the cluster (the rollout is in progress and Piceli is
waiting for readiness). ``release resume`` must finish the interrupted apply;
re-running ``release rollback`` must finish an interrupted rollback. Every
step boundary is covered against the fake API server by
``tests/acceptance/test_kill_resume.py``; this test proves the same recovery
with a real API server, controllers and kubelet.
"""

from __future__ import annotations

import signal
from pathlib import Path

import pytest
from kind_support import (
    DIGEST_1,
    DIGEST_2,
    cli,
    cli_process,
    get,
    operations,
    requires_kind,
    wait_for,
    write_spec,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(900), requires_kind]

COMPOSITION = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent


def build(ctx):
    meta = lambda name: {"name": name, "namespace": ctx.namespace}
    labels = {"app": "web"}
    settings = ResourceIntent.from_manifest({
        "apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("web-settings"),
        "data": {"image": ctx.image("web")},
    })
    web = ResourceIntent.from_manifest({
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta("web"),
        "spec": {"replicas": 2, "selector": {"matchLabels": labels},
                 "template": {"metadata": {"labels": labels}, "spec": {
                     "containers": [{"name": "web", "image": ctx.image("web"),
                                     "envFrom": [{"configMapRef": {
                                         "name": "web-settings"}}],
                                     "resources": {"requests": {"cpu": "10m",
                                                                "memory": "16Mi"}}}]}}},
    })
    return DeploymentComposition((
        DeploymentComponent("config", (settings,)),
        DeploymentComponent("web", (web,), dependencies=("config",)),
    ))
"""


def _image(namespace: str) -> str:
    live = get("deployment", "web", namespace)
    return str(live["spec"]["template"]["spec"]["containers"][0]["image"])


def _kill_when_image(spec: Path, namespace: str, digest: str, *args: str) -> None:
    process = cli_process(spec, *args)
    try:
        wait_for(
            lambda: process.poll() is not None or _image(namespace).endswith(digest),
            seconds=300,
            message="the Deployment write",
        )
        assert process.poll() is None, "finished before it could be killed"
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def test_killed_apply_resumes_and_killed_rollback_is_rerun(
    tmp_path: Path, kind_namespace: str
) -> None:
    namespace = kind_namespace
    (tmp_path / "web.py").write_text(COMPOSITION)
    spec = write_spec(tmp_path, namespace, "web.py")
    code, applied = cli(spec, "apply", "--auto-approve")
    assert code == 0 and applied["release_state"] == "ready", applied
    first = applied["release"]

    spec = write_spec(tmp_path, namespace, "web.py", digest=DIGEST_2)
    _kill_when_image(spec, namespace, DIGEST_2, "apply", "--auto-approve")
    code, resumed = cli(spec, "resume")
    assert code == 0 and resumed["release_state"] == "ready", resumed
    code, planned = cli(spec, "plan")
    assert code == 0, planned
    assert set(operations(planned).values()) == {"no-op"}, planned["diffs"]
    second = planned["release"]
    assert second != first

    _kill_when_image(spec, namespace, DIGEST_1, "rollback", first, "--auto-approve")
    code, refused = cli(spec, "resume")
    assert code == 2 and refused["reason"] == "not-resumable", refused
    code, rolled = cli(spec, "rollback", first, "--auto-approve")
    assert code == 0 and rolled["selected"] == first, rolled
    assert _image(namespace).endswith(DIGEST_1)
    code, planned = cli(spec, "rollback", first)
    assert code in {0, 3}, planned
    assert set(operations(planned).values()) == {"no-op"}, planned["diffs"]
    live = get("deployment", "web", namespace)
    assert live["status"].get("updatedReplicas") == 2
