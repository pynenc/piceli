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

0. plans the shop pipeline while an unmanaged ``Service/web`` exists: before
   anything is built, the placeholder preview refuses with the blocking
   object and the flags that unblock it, and nothing is written;
1. plans the shop pipeline (nothing is executed; the release preview uses
   placeholder images) and approves its hash;
2. runs it before the cache's existing claim exists, so the apply stage
   fails (not ready) after build and delivery;
3. creates the claim and resumes: the run continues at the apply stage;
4. reruns without changes: a no-op in under 10 s;
5. deploys a changed app whose check fails: the previous release is
   re-applied (``rolled-back``);
6. reruns the original pipeline: the rolled-back release is deployed, so the
   apply is skipped again.
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


def _module(tmp_path: Path, namespace: str, *, failing_check: bool = False) -> Path:
    """The example's app with a short readiness window, in a private state dir.

    ``failing_check`` changes the app (one more ConfigMap, so a new release)
    and declares a check that cannot pass, with rollback on failed checks.
    """
    module = tmp_path / (
        "deploy_shop_failing.py" if failing_check else "deploy_shop.py"
    )
    extra = (
        """
            shop.app.config("canary", {"release": "failing"})
            checks = dict(
                checks=Checks.http(shop.web, "/", expect=418, retries=0),
                rollback_on_failed_checks=True,
            )
            """
        if failing_check
        else "checks = {}"
    )
    module.write_text(
        textwrap.dedent(
            f"""
            import importlib.util

            from piceli import Checks, NodeLoopbackRegistry, Pipeline

            spec = importlib.util.spec_from_file_location("shop_example", {str(EXAMPLE)!r})
            shop = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(shop)
            {extra}
            pipeline = Pipeline(
                shop.app, shop.target, build=shop.images,
                deliver=NodeLoopbackRegistry(port=5000, storage="1Gi"),
                secrets=shop.secrets, state_dir={str(tmp_path / "state")!r},
                execution={{"readiness_seconds": 60, "max_seconds": 600}},
                **checks,
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

    # An object with an app name that the release does not manage blocks the
    # first plan, before any build or registry write (placeholder preview).
    api.create_namespaced_service(
        name,
        {
            "metadata": {"name": "web"},
            "spec": {"selector": {"app": "legacy"}, "ports": [{"port": 80}]},
        },
    )
    code, events, stderr, _ = _deploy(module, name, "--plan", "--json")
    assert code == 2, stderr
    blocked = events[-1]
    assert blocked["reason"] == "resource-requires-adoption", blocked
    assert blocked["stage"] == "plan"
    assert blocked["preview"] == {
        "approvable": False,
        "placeholders": {"rust-hello": "pending-build"},
    }
    assert [(b["kind"], b["name"], b["suggest"]) for b in blocked["blocking"]] == [
        (
            "Service",
            "web",
            ['Pipeline(adopt=["Service/web"])', 'Pipeline(replace=["Service/web"])'],
        )
    ]
    assert "blocking Service/web" in stderr, stderr
    assert not (tmp_path / "state" / "builds").exists()  # nothing was built
    from kubernetes.client import AppsV1Api

    assert not AppsV1Api(api.api_client).list_namespaced_deployment(name).items
    service = api.read_namespaced_service("web", name)
    assert service.spec.selector == {"app": "legacy"}
    assert "piceli.io/owner" not in (service.metadata.annotations or {})
    api.delete_namespaced_service("web", name)

    code, events, stderr, _ = _deploy(module, name, "--plan", "--json")
    assert code == 0, stderr
    planned = events[-1]
    assert planned["state"] == "planned"
    assert planned["stages"]["deliver"]["registry"]["changes"], planned
    preview = planned["stages"]["plan"]["preview"]
    assert preview["approvable"] is False and preview["state"] == "previewed"
    assert {"operation": "create", "kind": "Service", "name": "web"} in preview[
        "changes"
    ]
    assert "not approvable" in stderr
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

    # A changed release whose check fails is rolled back to the ready one.
    ready_release = final["release"]
    failing = _module(tmp_path, name, failing_check=True)
    code, events, stderr, _ = _deploy(failing, name, "--auto-approve", "--json")
    assert code == 1, stderr
    rolled = events[-1]
    assert rolled["reason"] == "pipeline-checks-failed", rolled
    assert rolled["state"] == "rolled-back", rolled
    assert rolled["stage"] == "checks"
    assert rolled["stages"]["apply"] == "done"
    assert rolled["release"] != ready_release

    code, events, stderr, _ = _deploy(module, name, "--auto-approve", "--json")
    assert code == 0, stderr
    assert events[-1]["release"] == ready_release
    assert events[-1]["stages"]["apply"] == "skipped", events[-1]
