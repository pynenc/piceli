"""Typed egress policies enforced on kind (kindnet enforces NetworkPolicy).

A client pod reaches an allowed workload and not a denied one; DNS keeps
working with the DNS helper and breaks without it; a replica pair is cut
apart by a typed egress policy and healed by removing it.

Needs PICELI_KIND_KUBECONFIG, PICELI_KIND_CONTEXT and kubectl. It never uses
the current context.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
KUBECTL = shutil.which("kubectl")
# nginx:1.27-alpine multi-arch index digest (BusyBox wget and nslookup included).
DIGEST = "sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT and KUBECTL),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT, and install kubectl",
    ),
]

COMPOSITION = """
from piceli import App, NetworkPeer, NetworkRule

MODE = {mode!r}


def build(ctx):
    shop = App("shop")
    image = ctx.image("web")
    web = shop.deployment("web", image=image, ports=[80])
    denied = shop.deployment("denied", image=image, ports=[80])
    client = shop.deployment("client", image=image, ports=[80])
    shop.service(web, 80)
    shop.service(denied, 80)
    if MODE in ("egress", "no-dns"):
        shop.network_policy(
            client, egress=[web], allow_dns=(MODE == "egress"), policy_types=["Egress"],
        )
    replica_labels = {{"net": "replica"}}
    a = shop.stateful_set("replica-a", image=image, ports=[80], labels=replica_labels)
    b = shop.stateful_set("replica-b", image=image, ports=[80], labels=replica_labels)
    if MODE == "cut":
        # Typed replica cut: replicas may only reach the client, plus DNS.
        shop.network_policy(
            selector=replica_labels,
            name="replica-cut",
            egress=[
                NetworkRule(peers=[NetworkPeer.pods(client.selector_labels)]),
                NetworkRule.dns(),
            ],
        )
    return shop
"""


@pytest.fixture
def namespace():
    from kubernetes.client import CoreV1Api

    name = "netpol-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    core = CoreV1Api(client)
    core.create_namespace({"metadata": {"name": name}})
    try:
        yield name
    finally:
        core.delete_namespace(name)
        client.close()


def _spec(directory: Path, namespace: str, mode: str) -> Path:
    module = f"shop_{mode.replace('-', '_')}.py"
    (directory / module).write_text(COMPOSITION.format(mode=mode))
    spec = directory / f"shop-{mode}.toml"
    spec.write_text(
        f"""
[target]
kubeconfig = "{KUBECONFIG}"
context = "{CONTEXT}"
namespace = "{namespace}"
[release]
name = "shop"
owner = "netpol-shop-e2e"
field_manager = "netpol-shop-e2e"
composition = "{module}:build"
state_dir = "state"
prune = true
[execution]
readiness_seconds = 240
[images]
web = "docker.io/library/nginx@{DIGEST}"
"""
    )
    return spec


def _apply(spec: Path) -> None:
    runner = CliRunner()
    planned = runner.invoke(app, ["plan", "--spec", str(spec)])
    assert planned.exit_code in (0, 3), planned.output
    plan_hash = json.loads(planned.stdout)["plan_hash"]
    done = runner.invoke(app, ["apply", "--spec", str(spec), "--approve", plan_hash])
    assert done.exit_code == 0, done.output
    assert json.loads(done.stdout)["execution"]["state"] == "ready"


def _exec(namespace: str, pod: str, *command: str) -> bool:
    result = subprocess.run(  # fixed argv, explicit kubeconfig
        [
            str(KUBECTL),
            "--kubeconfig",
            KUBECONFIG,
            "--context",
            CONTEXT,
            "--namespace",
            namespace,
            "exec",
            pod,
            "--",
            *command,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result.returncode == 0


def _eventually(check, expected: bool, seconds: int = 60) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check() == expected:
            return True
        time.sleep(2)
    return check() == expected


def _get(namespace: str, pod: str, target: str) -> bool:
    return _exec(namespace, pod, "wget", "-q", "-T", "3", "-O", "/dev/null", target)


def _resolves(namespace: str, pod: str, host: str) -> bool:
    return _exec(namespace, pod, "nslookup", "-timeout=3", host)


def _client(namespace: str) -> str:
    result = subprocess.run(  # fixed argv, explicit kubeconfig
        [
            str(KUBECTL),
            "--kubeconfig",
            KUBECONFIG,
            "--context",
            CONTEXT,
            "--namespace",
            namespace,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=client",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout.strip()


def test_typed_egress_dns_and_replica_cut(tmp_path, namespace):
    # Baseline without any policy: everything is reachable.
    _apply(_spec(tmp_path, namespace, "open"))
    client = _client(namespace)
    assert _get(namespace, client, "http://web")
    assert _get(namespace, client, "http://denied")
    assert _get(namespace, "replica-a-0", "http://replica-b-0.replica-b")

    # Typed egress: the allowed workload is reachable, the other is not, and DNS works.
    _apply(_spec(tmp_path, namespace, "egress"))
    assert _eventually(lambda: _get(namespace, client, "http://denied"), False)
    assert _get(namespace, client, "http://web")
    assert _resolves(namespace, client, f"denied.{namespace}.svc.cluster.local")

    # Without the DNS helper a default-deny egress breaks name resolution.
    _apply(_spec(tmp_path, namespace, "no-dns"))
    assert _eventually(
        lambda: _resolves(namespace, client, f"web.{namespace}.svc.cluster.local"),
        False,
    )

    # Replica cut: the replicas cannot reach each other (DNS still resolves) ...
    _apply(_spec(tmp_path, namespace, "cut"))
    assert _eventually(
        lambda: _get(namespace, "replica-a-0", "http://replica-b-0.replica-b"), False
    )
    assert _resolves(
        namespace, "replica-a-0", f"replica-b-0.replica-b.{namespace}.svc.cluster.local"
    )
    assert _get(namespace, "replica-a-0", "http://" + client_ip(namespace, client))

    # ... and healing is removing the policy.
    _apply(_spec(tmp_path, namespace, "open"))
    assert _eventually(
        lambda: _get(namespace, "replica-a-0", "http://replica-b-0.replica-b"), True
    )


def client_ip(namespace: str, pod: str) -> str:
    result = subprocess.run(  # fixed argv, explicit kubeconfig
        [
            str(KUBECTL),
            "--kubeconfig",
            KUBECONFIG,
            "--context",
            CONTEXT,
            "--namespace",
            namespace,
            "get",
            "pod",
            pod,
            "-o",
            "jsonpath={.status.podIP}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout.strip()
