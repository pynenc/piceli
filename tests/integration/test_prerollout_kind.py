"""Opt-in: pre-rollout checks gate ``piceli deploy`` on a disposable kind cluster.

Runs only when both variables name a disposable cluster, for example::

    kind create cluster --name piceli-pre --kubeconfig /tmp/pre.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/pre.kubeconfig PICELI_KIND_CONTEXT=kind-piceli-pre \\
      uv run pytest tests/integration/test_prerollout_kind.py

The scenario is shaped like a stateful service with a retained volume and a
mounted Secret. The running workload writes a format marker into its claim.
A release with a compatible new image passes its upgrade check (the new image
opens the claim read-only: a write there fails) and rolls out; a release whose
image expects another format, or whose Secret mount is bad, stops before the
pods change. Each ``piceli deploy`` runs in its own process.
"""

from __future__ import annotations

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

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
# nginx:1.27-alpine and nginx:1.26-alpine multi-arch index digests.
IMAGE_1 = "nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
IMAGE_2 = "nginx:1.26-alpine@sha256:1eadbb07820339e8bbfed18c771691970baee292ec4ab2558f1453d26153e22d"
TOKEN = "tok-9f8e7d6c5b4a3210-not-for-logs"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT",
    ),
    pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl"),
]

APP = textwrap.dedent(
    """\
    import os
    from piceli import App, ClaimTemplate, Pipeline, SecretVolume, Target, UpgradeCheck

    target = Target.kubeconfig(
        os.environ["PICELI_KIND_KUBECONFIG"], context=os.environ["PICELI_KIND_CONTEXT"],
        namespace=os.environ["STORE_NAMESPACE"], request_seconds=30,
    )
    app = App("store-app")
    store = app.stateful_set(
        "store",
        image=os.environ["STORE_IMAGE"],
        command=["sh", "-c", "echo 1 > /var/lib/store/FORMAT; exec sleep 3600"],
        env={"EXPECT_FORMAT": os.environ["STORE_FORMAT"]},
        ready=app.probe.exec(["test", "-s", "/var/lib/store/FORMAT"]),
        volumes={
            "/etc/keys": SecretVolume(os.environ.get("STORE_KEYS", "keys")),
            "/var/lib/store": ClaimTemplate("data", size="64Mi"),
        },
        replicas=1,
    )
    app.pre_rollout(
        store,
        ["sh", "-c", os.environ.get("STORE_CONFIG_CHECK", "test -s /etc/keys/token")],
        upgrade=UpgradeCheck(["sh", "-c",
            'test "$(cat /var/lib/store/FORMAT)" = "$EXPECT_FORMAT" '
            '&& ! touch /var/lib/store/probe 2>/dev/null']),
    )
    pipeline = Pipeline(
        app, target, state_dir=os.environ["STORE_STATE"],
        execution={"readiness_seconds": 240, "max_seconds": 600},
    )
    """
)


@pytest.fixture
def namespace():
    import base64

    from kubernetes.client import CoreV1Api

    name = "pre-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    api = CoreV1Api(client)
    api.create_namespace({"metadata": {"name": name}})
    api.create_namespaced_secret(
        name,
        {
            "metadata": {"name": "keys"},
            "data": {"token": base64.b64encode(TOKEN.encode()).decode()},
        },
    )
    try:
        yield name, client
    finally:
        api.delete_namespace(name)
        client.close()


def deploy(
    tmp_path: Path, namespace: str, *args: str, **env: str
) -> tuple[int, list[dict[str, Any]], str]:
    (tmp_path / "app.py").write_text(APP)
    result = subprocess.run(
        [sys.executable, "-m", "piceli", "deploy", "app.py:pipeline", *args, "--json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "PICELI_KIND_KUBECONFIG": KUBECONFIG,
            "PICELI_KIND_CONTEXT": CONTEXT,
            "STORE_NAMESPACE": namespace,
            "STORE_STATE": str(tmp_path / "state"),
            "STORE_IMAGE": IMAGE_1,
            "STORE_FORMAT": "1",
            **env,
        },
    )
    lines = [json.loads(x) for x in result.stdout.splitlines() if x.strip()]
    return result.returncode, lines, result.stdout + result.stderr


def state(client: Any, namespace: str) -> dict[str, Any]:
    from kubernetes.client import AppsV1Api, BatchV1Api, CoreV1Api

    sts = AppsV1Api(client).read_namespaced_stateful_set("store", namespace)
    pods = CoreV1Api(client).list_namespaced_pod(namespace).items
    return {
        "image": sts.spec.template.spec.containers[0].image,
        "generation": sts.metadata.generation,
        "pod_uids": sorted(
            p.metadata.uid for p in pods if p.metadata.name == "store-0"
        ),
        "jobs": [
            j.metadata.name
            for j in BatchV1Api(client).list_namespaced_job(namespace).items
        ],
        "check_pods": [
            p.metadata.name
            for p in pods
            if "-cfg-" in p.metadata.name or "-up" in p.metadata.name
        ],
    }


def last_run(tmp_path: Path) -> dict[str, Any]:
    return json.loads(
        sorted((tmp_path / "state" / "runs").glob("*.json"))[-1].read_text()
    )


def test_pre_rollout_checks_gate_the_rollout_on_kind(tmp_path: Path, namespace) -> None:
    name, client = namespace

    # 1. First release: config check with the real Secret; no claim yet.
    code, events, out = deploy(tmp_path, name, "--auto-approve")
    assert code == 0, out
    assert events[-1]["stages"]["prerollout"] == "done"
    first = state(client, name)
    assert first["image"] == IMAGE_1 and first["jobs"] == []

    # 2. A compatible new image: the upgrade check opens the claim read-only.
    code, events, out = deploy(tmp_path, name, "--auto-approve", STORE_IMAGE=IMAGE_2)
    assert code == 0, out
    checks = last_run(tmp_path)["stages"]["prerollout"]["output"]["checks"]
    assert [(c["kind"], c["state"]) for c in checks] == [
        ("config", "passed"),
        ("upgrade", "passed"),
    ]
    assert checks[1]["claims"] == ["data-store-0"]
    second = state(client, name)
    assert second["image"] == IMAGE_2 and second["jobs"] == []

    # 3. An image that expects another store format: stopped, pods unchanged.
    code, events, out = deploy(
        tmp_path, name, "--auto-approve", STORE_IMAGE=IMAGE_1, STORE_FORMAT="2"
    )
    assert code == 1, out
    assert events[-1]["reason"] == "prerollout-failed"
    assert events[-1]["stages"]["apply"] == "pending"
    after = state(client, name)
    assert (after["image"], after["generation"], after["pod_uids"]) == (
        second["image"],
        second["generation"],
        second["pod_uids"],
    )
    assert after["jobs"] == [] and after["check_pods"] == []
    failed = last_run(tmp_path)["stages"]["prerollout"]["output"]["checks"][-1]
    assert (failed["kind"], failed["state"], failed["exit_code"]) == (
        "upgrade",
        "failed",
        1,
    )

    # 4. A check that prints the Secret: the summary never holds the value.
    code, events, out = deploy(
        tmp_path,
        name,
        "--auto-approve",
        STORE_IMAGE=IMAGE_1,
        STORE_CONFIG_CHECK="cat /etc/keys/token; echo; exit 7",
    )
    assert code == 1, out
    record = json.dumps(last_run(tmp_path))
    assert "prerollout-failed" in record and "exit_code" in record
    assert TOKEN not in out and TOKEN not in record
    assert state(client, name)["image"] == second["image"]

    # 5. A Secret mount that cannot work fails the plan, not the pods.
    code, events, out = deploy(
        tmp_path, name, "--auto-approve", STORE_IMAGE=IMAGE_1, STORE_KEYS="absent"
    )
    assert code == 2, out
    assert events[-1]["reason"] == "prerollout-mount-missing"
    assert state(client, name)["image"] == second["image"]

    # 6. An image the node cannot pull fails the check as not startable (image),
    #    long before its timeout, and leaves no Job behind.
    code, events, out = deploy(
        tmp_path,
        name,
        "--auto-approve",
        STORE_IMAGE="nginx:1.27-alpine@sha256:" + "0" * 64,
    )
    assert code == 1, out
    assert events[-1]["reason"] == "prerollout-not-startable"
    check = last_run(tmp_path)["stages"]["prerollout"]["output"]["checks"][0]
    assert check["category"] == "image" and check["cleaned"] is True
    unchanged = state(client, name)
    assert unchanged["image"] == second["image"] and unchanged["jobs"] == []
