"""Opt-in: the in-cluster registry on a disposable MULTI-NODE kind cluster.

Needs at least two nodes (CI's single-node cluster skips it), for example::

    cat > /tmp/kind.yaml <<EOF
    kind: Cluster
    apiVersion: kind.x-k8s.io/v1alpha4
    nodes: [{role: control-plane}, {role: worker}, {role: worker}]
    EOF
    kind create cluster --name piceli-wpc --config /tmp/kind.yaml \\
        --kubeconfig /tmp/wpc.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/wpc.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-wpc \\
    PICELI_KIND_NODE=piceli-wpc-worker \\
      uv run pytest tests/integration/test_cluster_registry_kind.py

It needs ``docker`` (``busybox:1.36`` is pulled into the local engine) and
``kubectl``, and never reads the ambient kubeconfig.

1. ``piceli registry install --on NODE_A`` plans, then installs with the hash;
   ``status`` becomes ``ready`` with the mirror ready on every node.
2. An image is pushed by digest from this machine (``registry_deliver``
   through a port-forward to the Service, the route ``piceli deploy`` uses).
3. A pod pinned to another node B runs
   ``piceli-registry.piceli-system.svc:5000/…@sha256:…`` with
   ``imagePullPolicy: Always``: node B pulled it through its containerd
   mirror, not a node loopback.
4. ``uninstall --delete-storage`` removes everything; the node agents remove
   their ``hosts.toml``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from kind_support import kubectl
from typer.testing import CliRunner

from piceli import App, Pipeline, Registry, Target
from piceli.k8s.cli import app

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
NODE = os.environ.get("PICELI_KIND_NODE", "")
SOURCE_IMAGE = "busybox:1.36"
HOST = "piceli-registry.piceli-system.svc:5000"
MIRROR = f"/etc/containerd/certs.d/{HOST}/hosts.toml"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (
            KUBECONFIG
            and CONTEXT
            and NODE
            and shutil.which("docker")
            and shutil.which("kubectl")
        ),
        reason="set PICELI_KIND_KUBECONFIG, PICELI_KIND_CONTEXT and PICELI_KIND_NODE",
    ),
]


def _nodes() -> list[str]:
    found = json.loads(kubectl("get", "nodes", "-o", "json"))
    return sorted(item["metadata"]["name"] for item in found["items"])


@pytest.fixture(scope="module")
def nodes() -> tuple[str, str]:
    names = _nodes()
    if len(names) < 2:
        pytest.skip("the in-cluster registry test needs a cluster with >= 2 nodes")
    others = [name for name in names if name != NODE]
    assert NODE in names, "PICELI_KIND_NODE must name a node of the cluster"
    return NODE, others[-1]


def _registry(command: str, *extra: str) -> Any:
    return CliRunner().invoke(
        app,
        [
            "registry",
            command,
            "--kubeconfig",
            KUBECONFIG,
            "--context",
            CONTEXT,
            *extra,
        ],
    )


def _approved(command: str, *extra: str) -> dict[str, Any]:
    planned = _registry(command, *extra)
    assert planned.exit_code in (0, 3), planned.output
    body = json.loads(planned.stdout)
    if body["state"] == "unchanged":
        return dict(body)
    done = _registry(command, *extra, "--approve", body["plan_hash"])
    assert done.exit_code == 0, done.output
    return dict(json.loads(done.stdout))


def _status() -> dict[str, Any]:
    result = _registry("status", "--json")
    assert result.exit_code == 0, result.output
    return dict(json.loads(result.stdout))


def _node_file(node: str, path: str) -> str | None:
    result = subprocess.run(
        ["docker", "exec", node, "cat", path],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout if result.returncode == 0 else None


def _image_id() -> str:
    subprocess.run(
        ["docker", "pull", "--quiet", SOURCE_IMAGE],
        check=True,
        capture_output=True,
        timeout=600,
    )
    return subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", SOURCE_IMAGE],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()


def test_every_node_pulls_from_the_in_cluster_registry(
    nodes: tuple[str, str], kind_namespace: str, tmp_path: Path
) -> None:
    from piceli.pipeline.backend import Backend
    from piceli.pipeline.runner import PipelineRunner

    node_a, node_b = nodes
    try:
        installed = _approved("install", "--on", node_a, "--storage", "1Gi")
        assert installed["state"] in {"installed", "unchanged"}
        deadline = time.monotonic() + 300
        status = _status()
        while status["state"] != "ready" and time.monotonic() < deadline:
            time.sleep(3)
            status = _status()
        assert status["state"] == "ready", status
        assert {m["node"] for m in status["mirrors"]} == set(_nodes())
        assert status["registry"]["pods"][0]["node"] == node_a
        service_ip = status["service"]["cluster_ip"]
        mirror = _node_file(node_b, MIRROR)
        assert mirror is not None and f'"http://{service_ip}:5000"' in mirror

        # Push by digest from this machine, as `piceli deploy` does.
        pipeline = Pipeline(
            App("e2e"),
            Target.kubeconfig(KUBECONFIG, context=CONTEXT, namespace=kind_namespace),
            deliver=Registry.in_cluster(on=node_a),
            state_dir=tmp_path / "state",
        )
        runner = PipelineRunner.__new__(PipelineRunner)
        runner.pipeline = pipeline
        route = runner._route()
        assert route.forward == "service/piceli-registry"
        receipt = Backend().registry_deliver(route, _image_id(), "e2e/busybox")
        assert receipt["state"] == "succeeded", receipt.get("reason")
        pull_ref = receipt["pull_ref"]
        assert pull_ref.startswith(f"{HOST}/e2e/busybox@sha256:")

        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "pull-on-b", "namespace": kind_namespace},
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": node_b},
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "main",
                        "image": pull_ref,
                        "imagePullPolicy": "Always",
                        "command": ["sh", "-c", "echo pulled-by-digest"],
                    }
                ],
            },
        }
        kubectl("apply", "-f", "-", stdin=json.dumps(pod))
        deadline = time.monotonic() + 180
        phase = ""
        while time.monotonic() < deadline:
            live = json.loads(
                kubectl(
                    "get", "pod", "pull-on-b", "-o", "json", namespace=kind_namespace
                )
            )
            phase = live["status"].get("phase", "")
            if phase in {"Succeeded", "Failed"}:
                break
            time.sleep(2)
        assert phase == "Succeeded", json.dumps(live["status"])[-1500:]
        assert live["spec"]["nodeName"] == node_b
        logs = kubectl("logs", "pull-on-b", namespace=kind_namespace)
        assert "pulled-by-digest" in logs
    finally:
        removed = _approved("uninstall", "--delete-storage")
        assert removed["state"] in {"uninstalled", "unchanged"}
    deadline = time.monotonic() + 120
    while _node_file(node_b, MIRROR) is not None and time.monotonic() < deadline:
        time.sleep(2)
    assert _node_file(node_b, MIRROR) is None
    assert _status()["state"] == "not-installed"
