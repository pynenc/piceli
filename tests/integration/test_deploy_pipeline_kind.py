"""Opt-in ``piceli deploy`` acceptance of ``examples/shop`` on a disposable kind cluster.

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-p5 --kubeconfig /tmp/p5.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/p5.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-p5 \\
    PICELI_KIND_NODE=piceli-p5-control-plane \\
      uv run pytest tests/integration/test_deploy_pipeline_kind.py

It needs ``docker`` and ``kubectl`` on ``PATH`` and builds
``examples/builds/rust-hello`` (``linux/arm64``, like the example). The test
never reads the ambient kubeconfig. In a uniquely named namespace it:

1. plans the shop pipeline (nothing is executed) and approves its hash;
2. runs it before the cache's existing claim exists, so the apply stage
   fails (not ready) after build and delivery;
3. creates the claim and resumes: the run continues at the apply stage;
4. reruns without changes: a no-op in under 10 s.
"""

from __future__ import annotations

import json
import os
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
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "shop" / "app.py"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(1800),
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT and NODE and shutil.which("docker")),
        reason="set PICELI_KIND_KUBECONFIG, PICELI_KIND_CONTEXT and PICELI_KIND_NODE",
    ),
]


@pytest.fixture
def namespace():
    from kubernetes.client import CoreV1Api

    name = "shop-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    api = CoreV1Api(client)
    api.create_namespace({"metadata": {"name": name}})
    try:
        yield name, api
    finally:
        api.delete_namespace(name)
        client.close()


def _module(tmp_path: Path, namespace: str) -> Path:
    """The example's app with a short readiness window, in a private state dir."""
    module = tmp_path / "deploy_shop.py"
    module.write_text(
        textwrap.dedent(
            f"""
            import importlib.util

            from piceli import NodeLoopbackRegistry, Pipeline

            spec = importlib.util.spec_from_file_location("shop_example", {str(EXAMPLE)!r})
            shop = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(shop)

            pipeline = Pipeline(
                shop.app, shop.target, build=shop.images,
                deliver=NodeLoopbackRegistry(port=5000, storage="1Gi"),
                secrets=shop.secrets, state_dir={str(tmp_path / "state")!r},
                execution={{"readiness_seconds": 60, "max_seconds": 600}},
            )
            """
        )
    )
    return module


def _deploy(
    module: Path, namespace: str, *args: str
) -> tuple[int, list[dict], str, float]:
    env = {
        **os.environ,
        "SHOP_KUBECONFIG": KUBECONFIG,
        "SHOP_CONTEXT": CONTEXT,
        "SHOP_NAMESPACE": namespace,
        "SHOP_NODE": NODE,
    }
    env.pop("KUBECONFIG", None)
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "piceli", "deploy", f"{module}:pipeline", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=1500,
    )
    elapsed = time.monotonic() - started
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return result.returncode, events, result.stderr, elapsed


def _claim(api, namespace: str) -> None:
    api.create_namespaced_persistent_volume_claim(
        namespace,
        {
            "metadata": {"name": "cache-state"},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "64Mi"}},
            },
        },
    )


def test_shop_deploys_resumes_and_reruns_as_noop(namespace, tmp_path) -> None:
    name, api = namespace
    module = _module(tmp_path, name)

    code, events, stderr, _ = _deploy(module, name, "--plan", "--json")
    assert code == 0, stderr
    planned = events[-1]
    assert planned["state"] == "planned"
    assert planned["stages"]["deliver"]["registry"]["changes"], planned
    assert not api.list_namespaced_pod(name).items  # --plan executed nothing

    # Without the cache's claim the apply stage cannot become ready.
    code, events, stderr, _ = _deploy(
        module, name, "--approve", planned["combined_hash"], "--json"
    )
    assert code == 1, stderr
    failed = events[-1]
    assert failed["reason"] == "pipeline-apply-not-ready", failed
    assert failed["stage"] == "apply"
    assert failed["stages"]["build"] in {"done", "skipped"}
    assert failed["stages"]["deliver"] == "done"

    _claim(api, name)
    code, events, stderr, _ = _deploy(module, name, "--resume", "--json")
    assert code == 0, stderr
    final = events[-1]
    assert final["state"] == "ready" and final["run_id"] == failed["run_id"]
    running = [e["stage"] for e in events if e.get("state") == "running"]
    assert running == ["apply", "checks"]
    from kubernetes.client import AppsV1Api

    apps = AppsV1Api(api.api_client)
    for workload in ("web", "api"):
        pod = apps.read_namespaced_deployment(workload, name).spec.template.spec
        assert pod.containers[0].image.startswith(
            "127.0.0.1:5000/shop/rust-hello@sha256:"
        )
        assert pod.node_selector == {"kubernetes.io/hostname": NODE}

    code, events, stderr, elapsed = _deploy(module, name, "--auto-approve", "--json")
    assert code == 0, stderr
    assert events[-1]["stages"] == {
        "inputs": "done",
        "build": "skipped",
        "deliver": "skipped",
        "plan": "done",
        "apply": "skipped",
        "checks": "skipped",
    }
    assert elapsed < 10, f"warm no-op rerun took {elapsed:.1f}s"
