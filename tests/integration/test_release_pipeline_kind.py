"""Opt-in: ``piceli release … --spec MODULE:ATTR`` on a pipeline deployed to kind.

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-p5 --kubeconfig /tmp/p5.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/p5.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-p5 \\
    PICELI_KIND_NODE=piceli-p5-control-plane \\
      uv run pytest tests/integration/test_release_pipeline_kind.py

Needs ``docker`` and ``kubectl`` on ``PATH`` (it builds
``examples/builds/rust-hello`` like ``test_deploy_pipeline_kind``) and never
reads the ambient kubeconfig. In a uniquely named namespace it:

1. deploys the shop pipeline (plus an HTTP ``probe`` workload whose
   ``Checks.http`` must pass through the default check runner), then a
   changed app (one more ConfigMap), so there are two releases;
2. rolls back to the previous release with ``release rollback previous
   --spec MODULE:ATTR``: approval first, then ``--approve HASH``; nothing is
   built and the recorded image digests (``oci-set``) are re-applied;
3. reads ``release status`` and ``release secret show … --reveal`` for the
   pipeline and compares the generated value with the cluster's Secret
   without printing it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Any

import pytest

from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig
from tests.integration.kind_support import node_platform

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
    api.create_namespaced_persistent_volume_claim(
        name,
        {
            "metadata": {"name": "cache-state"},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "64Mi"}},
            },
        },
    )
    try:
        yield name, api
    finally:
        api.delete_namespace(name)
        client.close()


def _module(tmp_path: Path, *, changed: bool) -> str:
    """The example pipeline in a private state dir; ``changed`` adds a ConfigMap."""
    module = tmp_path / ("shop_changed.py" if changed else "shop_first.py")
    extra = 'shop.app.config("canary", {"release": "second"})' if changed else ""
    module.write_text(
        textwrap.dedent(
            f"""
            import importlib.util

            from piceli import Checks, NodeLoopbackRegistry, Pipeline
            from piceli.k8s.templates.deployable.node_local_registry import (
                DEFAULT_REGISTRY_IMAGE,
            )

            spec = importlib.util.spec_from_file_location("shop_example", {str(EXAMPLE)!r})
            shop = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(shop)
            shop.images.platform = {node_platform()!r}  # build for the kind node
            {extra}
            # An HTTP workload from an image already on the node (the delivery
            # registry's), checked through a real port forward after readiness.
            probe = shop.app.deployment("probe", image=DEFAULT_REGISTRY_IMAGE, ports=[5000], ready=shop.app.probe.http("/v2/", 5000))
            probe_service = shop.app.service(probe, port=5000)
            pipeline = Pipeline(
                shop.app, shop.target, build=shop.images,
                deliver=NodeLoopbackRegistry(port=5000, storage="1Gi"),
                secrets=shop.secrets, state_dir={str(tmp_path / "state")!r},
                execution={{"readiness_seconds": 90, "max_seconds": 600}},
                checks=Checks.http(probe_service, "/v2/", expect=200, retries=5),
                rollback_on_failed_checks=True,
            )
            """
        )
    )
    return f"{module}:pipeline"


def _piceli(namespace: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "SHOP_KUBECONFIG": KUBECONFIG,
        "SHOP_CONTEXT": CONTEXT,
        "SHOP_NAMESPACE": namespace,
        "SHOP_NODE": NODE,
    }
    env.pop("KUBECONFIG", None)
    return subprocess.run(
        [sys.executable, "-m", "piceli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=1500,
    )


def _last(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return json.loads(result.stdout) if len(lines) != 1 else json.loads(lines[0])


def _images(api: Any, namespace: str) -> dict[str, str]:
    from kubernetes.client import AppsV1Api

    apps = AppsV1Api(api.api_client)
    return {
        name: apps.read_namespaced_deployment(name, namespace)
        .spec.template.spec.containers[0]
        .image
        for name in ("web", "api", "cache", "probe")
    }


def test_release_commands_operate_a_deployed_pipeline(namespace, tmp_path) -> None:
    name, api = namespace
    first = _module(tmp_path, changed=False)
    second = _module(tmp_path, changed=True)

    result = _piceli(name, "deploy", first, "--auto-approve")
    assert result.returncode == 0, result.stderr
    deployed = _last(result)
    first_release = deployed["release"]
    # The declared http check ran through the default runner and passed.
    assert deployed["stages"]["checks"] == "done", deployed
    run = json.loads(
        sorted((tmp_path / "state" / "runs").glob("*.json"))[-1].read_text()
    )
    results = run["stages"]["checks"]["output"]["results"]
    assert [item["passed"] for item in results] == [True], results
    images = _images(api, name)
    result = _piceli(name, "deploy", second, "--auto-approve")
    assert result.returncode == 0, result.stderr
    second_release = _last(result)["release"]
    assert second_release != first_release
    assert api.read_namespaced_config_map("canary", name).data == {"release": "second"}
    runs = sorted((tmp_path / "state" / "runs").glob("*.json"))

    # Rollback: approval first, then the approved hash; nothing is built.
    result = _piceli(name, "release", "rollback", "previous", "--spec", second)
    assert result.returncode == 3, result.stderr
    planned = _last(result)
    assert planned["state"] == "approval-required"
    assert planned["release"] == first_release
    assert planned["source"]["kind"] == "oci-set"
    result = _piceli(
        name,
        "release",
        "rollback",
        "previous",
        "--spec",
        second,
        "--approve",
        planned["plan_hash"],
    )
    assert result.returncode == 0, result.stderr
    outcome = _last(result)
    assert outcome["state"] == "succeeded" and outcome["release"] == first_release
    # The rollback ran the pipeline's checks after readiness, as apply does.
    assert outcome["checks"]["passed"] is True, outcome["checks"]
    assert _images(api, name) == images
    assert sorted((tmp_path / "state" / "runs").glob("*.json")) == runs

    result = _piceli(name, "release", "status", "--spec", second)
    assert result.returncode == 0, result.stderr
    status = _last(result)
    assert status["deployed"] == first_release
    assert status["previous"] == second_release

    # The generated secret: compared with the cluster's value, never printed.
    result = _piceli(
        name, "release", "secret", "show", "cache_password", "--spec", second, "--json"
    )
    assert result.returncode == 0, result.stderr
    metadata = _last(result)
    assert metadata["secret"] == "cache_password" and "values" not in metadata
    result = _piceli(
        name,
        "release",
        "secret",
        "show",
        "cache_password",
        "--spec",
        second,
        "--json",
        "--reveal",
    )
    assert result.returncode == 0, "secret show --reveal failed"
    values = _last(result)["values"]
    revealed = next(iter(values.values()), "")
    live = api.read_namespaced_secret("cache-credentials", name).data["password"]
    same = (
        hashlib.sha256(revealed.encode()).digest()
        == hashlib.sha256(base64.b64decode(live)).digest()
    )
    long_enough = len(revealed) >= 32
    del values, revealed, live
    assert same, "the revealed value differs from the deployed Secret"
    assert long_enough, "the revealed value is shorter than generated"
