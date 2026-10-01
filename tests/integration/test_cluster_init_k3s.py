"""Opt-in: ``piceli cluster init`` on a disposable multi-node k3s cluster (k3d).

Skipped unless ``PICELI_K3S=1``; it needs ``k3d``, ``docker`` and ``kubectl``
on ``PATH``, creates its own cluster and always deletes it::

    PICELI_K3S=1 nix shell nixpkgs#k3d -c \\
        uv run pytest tests/integration/test_cluster_init_k3s.py

It never reads the ambient kubeconfig: k3d is told not to touch it, and the
cluster's kubeconfig goes to a temporary file behind a credential profile.

1. A composition declares the k3d nodes (one server, two agents) with roles,
   ``Registry.in_cluster(on=AGENT_0)`` (``node_mirror="auto"``) and a
   controller; ``cluster init`` plans (exit 3), applies with the hash, labels
   the nodes (``piceli.io/runtime=k3s``) and every node's k3s agent reports
   its mirror. A re-run is unchanged.
2. ``/etc/rancher/k3s/registries.yaml`` on every node holds the mirror for
   the stable name; k3s's own ``certs.d`` has its ``hosts.toml``.
3. An image pushed by digest from this machine runs on the other agent with
   ``imagePullPolicy: Always`` (the node pulled it through the mirror).
4. Every node's k3s is restarted (the documented procedure, here a container
   restart): k3s loads ``registries.yaml`` and a new pod pulls again.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli import App, Pipeline, Registry, Target
from piceli.k8s.cli import app

NAME = "piceli-wpe"
CONTEXT = f"k3d-{NAME}"
HOST = "piceli-registry.piceli-system.svc:5000"
REGISTRIES = "/etc/rancher/k3s/registries.yaml"
CERTS = f"/var/lib/rancher/k3s/agent/etc/containerd/certs.d/{HOST}/hosts.toml"
SOURCE_IMAGE = "busybox:1.36"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(1800),
    pytest.mark.skipif(
        os.environ.get("PICELI_K3S") != "1"
        or not (
            shutil.which("k3d") and shutil.which("docker") and shutil.which("kubectl")
        ),
        reason="set PICELI_K3S=1 with k3d, docker and kubectl on PATH "
        "(it creates and deletes its own k3d cluster)",
    ),
]


def _run(*command: str, stdin: str | None = None, timeout: float = 600) -> str:
    return subprocess.run(
        command,
        input=stdin,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    ).stdout


@pytest.fixture(scope="module")
def k3s(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A fresh k3d cluster (1 server, 2 agents); deleted however the test ends."""
    subprocess.run(["k3d", "cluster", "delete", NAME], capture_output=True, timeout=300)
    kubeconfig = tmp_path_factory.mktemp("k3s") / "kubeconfig"
    try:
        _run(
            "k3d", "cluster", "create", NAME, "--agents", "2",
            "--kubeconfig-update-default=false",
            "--kubeconfig-switch-context=false",
            "--k3s-arg", "--disable=traefik@server:0",
            "--wait", timeout=900,
        )  # fmt: skip
        kubeconfig.write_text(_run("k3d", "kubeconfig", "get", NAME))
        kubeconfig.chmod(0o600)
        yield kubeconfig
    finally:
        subprocess.run(
            ["k3d", "cluster", "delete", NAME], capture_output=True, timeout=300
        )


def _kubectl(kubeconfig: Path, *args: str, stdin: str | None = None) -> str:
    return _run(
        "kubectl", "--kubeconfig", str(kubeconfig), "--context", CONTEXT, *args,
        stdin=stdin,
    )  # fmt: skip


def _node_file(node: str, path: str) -> str | None:
    result = subprocess.run(
        ["docker", "exec", node, "cat", path],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout if result.returncode == 0 else None


def _cli(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def _wait_ready(kubeconfig: Path, seconds: float = 300) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        nodes = json.loads(_kubectl(kubeconfig, "get", "nodes", "-o", "json"))["items"]
        if nodes and all(
            any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in n["status"].get("conditions", [])
            )
            for n in nodes
        ):
            return
        time.sleep(3)
    raise AssertionError("the k3s nodes did not become Ready")


def _pull_on(kubeconfig: Path, namespace: str, node: str, image: str) -> None:
    name = "pull-" + uuid.uuid4().hex[:6]
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "nodeSelector": {"kubernetes.io/hostname": node},
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "main",
                    "image": image,
                    "imagePullPolicy": "Always",
                    "command": ["sh", "-c", "echo pulled-by-digest"],
                }
            ],
        },
    }
    _kubectl(kubeconfig, "apply", "-f", "-", stdin=json.dumps(pod))
    deadline = time.monotonic() + 240
    live: dict[str, Any] = {}
    while time.monotonic() < deadline:
        live = json.loads(
            _kubectl(kubeconfig, "get", "pod", name, "-n", namespace, "-o", "json")
        )
        if live["status"].get("phase") in {"Succeeded", "Failed"}:
            break
        time.sleep(2)
    assert live["status"].get("phase") == "Succeeded", json.dumps(live["status"])[
        -1500:
    ]
    assert live["spec"]["nodeName"] == node
    assert "pulled-by-digest" in _kubectl(kubeconfig, "logs", name, "-n", namespace)


def test_cluster_init_on_k3s_mirrors_every_node(
    k3s: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.pipeline.backend import Backend
    from piceli.pipeline.runner import PipelineRunner
    from piceli.profiles import save_profile

    monkeypatch.setenv("PICELI_PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.chdir(tmp_path)
    save_profile(NAME, k3s, CONTEXT)
    nodes = json.loads(_kubectl(k3s, "get", "nodes", "-o", "json"))["items"]
    arch = {
        n["metadata"]["name"]: n["status"]["nodeInfo"]["architecture"] for n in nodes
    }
    server = f"k3d-{NAME}-server-0"
    agent_a, agent_b = f"k3d-{NAME}-agent-0", f"k3d-{NAME}-agent-1"
    assert set(arch) == {server, agent_a, agent_b}
    api = next(
        c["cluster"]["server"]
        for c in json.loads(_kubectl(k3s, "config", "view", "--raw", "-o", "json"))[
            "clusters"
        ]
    )
    (tmp_path / "infra.py").write_text(
        f"""
from piceli import Registry
from piceli.infra import Cluster, Controller, Node, Ui

k3s = Cluster(
    "{NAME}",
    api="{api}",
    credentials="{NAME}",
    nodes=[
        Node("{server}", arch="{arch[server]}", roles=["control-plane"]),
        Node("{agent_a}", arch="{arch[agent_a]}", roles=["builder", "registry", "controller"]),
        Node("{agent_b}", arch="{arch[agent_b]}", roles=["workloads"]),
    ],
    registry=Registry.in_cluster(on="{agent_a}", storage="1Gi"),
    controller=Controller(on="{agent_a}"),
    ui=Ui(),
)
"""
    )
    planned = _cli("cluster", "init", "infra.py:k3s")
    assert planned.exit_code == 3, planned.output
    plan = json.loads(planned.stdout)
    done = _cli(
        "cluster",
        "init",
        "infra.py:k3s",
        "--approve",
        plan["plan_hash"],
        "--wait",
        "300",
    )
    assert done.exit_code == 0, done.output
    body = json.loads(done.stdout)
    print(
        "cluster init:", json.dumps({k: body[k] for k in ("mirrors", "restart_needed")})
    )
    assert {m["node"] for m in body["mirrors"]} == set(arch)
    assert all(
        m["runtime"] == "k3s" and m["mirror"] == "ready" for m in body["mirrors"]
    )
    assert body["restart_needed"] == [] and body["unmergeable"] == []
    labels = {
        n["metadata"]["name"]: n["metadata"]["labels"]
        for n in json.loads(_kubectl(k3s, "get", "nodes", "-o", "json"))["items"]
    }
    assert labels[agent_a]["piceli.io/builder"] == "true"
    assert all(labels[n]["piceli.io/runtime"] == "k3s" for n in arch)
    again = _cli("cluster", "init", "infra.py:k3s", "--wait", "0")
    assert again.exit_code == 0 and json.loads(again.stdout)["state"] == "unchanged"

    deadline = time.monotonic() + 300
    while True:
        status = _cli("cluster", "status", "infra.py:k3s", "--json")
        assert status.exit_code == 0, status.output
        report = json.loads(status.stdout)
        if report["registry"]["state"] == "ready" or time.monotonic() > deadline:
            break
        time.sleep(3)
    assert report["registry"]["state"] == "ready", report
    service_ip = report["registry"]["service"]["cluster_ip"]
    for node in arch:
        registries = _node_file(node, REGISTRIES)
        assert registries is not None and HOST in registries, node
        assert f"http://{service_ip}:5000" in registries
        assert _node_file(node, CERTS) is not None, node

    namespace = "e2e-" + uuid.uuid4().hex[:6]
    _kubectl(k3s, "create", "namespace", namespace)
    _run("docker", "pull", "--quiet", SOURCE_IMAGE)
    image_id = _run("docker", "image", "inspect", "--format", "{{.Id}}", SOURCE_IMAGE)
    pipeline = Pipeline(
        App("e2e"),
        Target.kubeconfig(k3s, context=CONTEXT, namespace=namespace),
        deliver=Registry.in_cluster(on=agent_a),
        state_dir=tmp_path / "state",
    )
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.pipeline = pipeline
    receipt = Backend().registry_deliver(
        runner._route(), image_id.strip(), "e2e/busybox"
    )
    assert receipt["state"] == "succeeded", receipt.get("reason")
    pull_ref = receipt["pull_ref"]
    assert pull_ref.startswith(f"{HOST}/e2e/busybox@sha256:")
    _pull_on(k3s, namespace, agent_b, pull_ref)

    # Restart k3s on every node: it loads registries.yaml at start.
    for node in arch:
        _run("docker", "restart", node, timeout=300)
    _wait_ready(k3s)
    for node in arch:
        logs = subprocess.run(
            ["docker", "logs", "--since", "15m", node],
            capture_output=True, text=True, timeout=60,
        )  # fmt: skip
        loaded = [
            line
            for line in (logs.stdout + logs.stderr).splitlines()
            if "registries.yaml" in line
        ]
        print(f"{node} after restart:", loaded[-1:] or "no registries.yaml log line")
        assert HOST in (_node_file(node, REGISTRIES) or ""), node
        assert _node_file(node, CERTS) is not None, node
    _pull_on(k3s, namespace, agent_b, pull_ref)
    _pull_on(k3s, namespace, server, pull_ref)
