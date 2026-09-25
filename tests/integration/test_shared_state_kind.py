"""Opt-in: shared state across "runners" on a real kind cluster.

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-m1 --kubeconfig /tmp/m1.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/m1.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-m1 \\
    PICELI_KIND_NODE=piceli-m1-control-plane \\
      uv run pytest tests/integration/test_shared_state_kind.py

Never reads the ambient kubeconfig. Each runner is a separate
``python -m piceli`` process with its own state directory; they share only the
cluster (the Lease lock and the state Secrets in the namespace) and the plan
file. In a uniquely named namespace it:

1. plans on runner A (``--plan --out``) and applies the plan file on runner
   B (``--apply``), which has no state of its own;
2. starts a deploy on runner C whose release never becomes ready (it holds
   the lock), and refuses a concurrent deploy on runner D
   (``pipeline-locked``);
3. kills runner C (``SIGKILL``): the lock stays held until its lease expires,
   then runner D takes it over and deploys; the generated secret value is the
   one runner A generated (compared by digest, never printed).

The pipeline has no build (a pinned public image), so Docker is not needed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
NODE = os.environ.get("PICELI_KIND_NODE", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(1200),
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT and NODE),
        reason="set PICELI_KIND_KUBECONFIG, PICELI_KIND_CONTEXT and PICELI_KIND_NODE",
    ),
]

MODULE = """
import os

from piceli import App, Pipeline, Random, Secrets, Target
from piceli.k8s.templates.deployable.node_local_registry import DEFAULT_REGISTRY_IMAGE

target = Target.kubeconfig(
    os.environ["SHARED_KUBECONFIG"],
    context=os.environ["SHARED_CONTEXT"],
    namespace=os.environ["SHARED_NAMESPACE"],
)
app = App("shared")
secrets = Secrets(password=Random(24))
credentials = app.secret("credentials", {"password": secrets.ref("password")})
app.deployment(
    "web",
    image=DEFAULT_REGISTRY_IMAGE,
    ports=[5000],
    env={"PASSWORD": credentials.key("password")},
    ready=app.probe.http(os.environ.get("READY_PATH", "/v2/"), 5000),
)
pipeline = Pipeline(
    app, target, secrets=secrets,
    state_dir=os.environ["DEPLOY_STATE_DIR"], state="cluster",
    state_lease_seconds=int(os.environ.get("LEASE", "30")),
    execution={"readiness_seconds": 300, "max_seconds": 600},
)
"""


@pytest.fixture
def namespace():
    from kubernetes.client import CoreV1Api

    name = "shared-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    api = CoreV1Api(client)
    api.create_namespace({"metadata": {"name": name}})
    try:
        yield name, client
    finally:
        api.delete_namespace(name)
        client.close()


def start(
    root: Path, namespace: str, runner: str, *args: str, **env: str
) -> subprocess.Popen[str]:
    variables = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PICELI_", "KUBECONFIG"))
    }
    variables.update(
        SHARED_KUBECONFIG=KUBECONFIG,
        SHARED_CONTEXT=CONTEXT,
        SHARED_NAMESPACE=namespace,
        DEPLOY_STATE_DIR=str(root / f"runner-{runner}"),
        **env,
    )
    return subprocess.Popen(
        [sys.executable, "-m", "piceli", *args],
        cwd=root,
        env=variables,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish(process: subprocess.Popen[str]) -> tuple[int, dict[str, Any], str]:
    stdout, stderr = process.communicate(timeout=900)
    lines = [line for line in stdout.splitlines() if line.strip()]
    try:
        body = json.loads(lines[-1]) if lines else {}
    except ValueError:
        body = json.loads(stdout)
    return process.returncode, body, stderr


def lease(client: Any, namespace: str) -> dict[str, Any]:
    from kubernetes.client import CoordinationV1Api

    value = CoordinationV1Api(client).read_namespaced_lease(
        "piceli-lock-shared", namespace
    )
    return {
        "holder": value.spec.holder_identity,
        "renewed": value.spec.renew_time.timestamp() if value.spec.renew_time else 0,
        "seconds": value.spec.lease_duration_seconds,
    }


def secret_digest(client: Any, namespace: str) -> str:
    from kubernetes.client import CoreV1Api

    value = CoreV1Api(client).read_namespaced_secret("credentials", namespace)
    return hashlib.sha256(value.data["password"].encode()).hexdigest()


def wait_for(condition: Any, seconds: float) -> None:
    end = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > end:
            raise AssertionError("condition not reached")
        time.sleep(0.5)


def test_plan_apply_lock_and_takeover_across_runners(namespace, tmp_path) -> None:
    name, client = namespace
    (tmp_path / "app.py").write_text(textwrap.dedent(MODULE))
    target = "app.py:pipeline"

    # 1. Plan on runner A, apply the plan file on runner B.
    code, body, stderr = finish(
        start(tmp_path, name, "a", "deploy", target, "--plan", "--out", "plan.json")
    )
    assert code == 0, stderr
    shutil.rmtree(tmp_path / "runner-a")  # nothing is shared through disk
    code, body, stderr = finish(
        start(
            tmp_path,
            name,
            "b",
            "deploy",
            "--apply",
            "plan.json",
            "--approve",
            body["combined_hash"],
            "--json",
        )
    )
    assert code == 0 and body["state"] == "ready", stderr
    digest = secret_digest(client, name)
    assert lease(client, name)["holder"] in (None, "")

    # 2. Runner C deploys a release that never becomes ready: it holds the lock.
    holder = start(
        tmp_path,
        name,
        "c",
        "deploy",
        target,
        "--auto-approve",
        READY_PATH="/never-ready",
    )

    def applying() -> bool:
        assert holder.poll() is None, holder.communicate()
        try:
            current = lease(client, name)["holder"] or ""
        except Exception:
            return False
        return f"/{holder.pid}/" in current

    wait_for(applying, 300)
    time.sleep(3)
    code, body, stderr = finish(
        start(tmp_path, name, "d", "deploy", target, "--auto-approve")
    )
    assert (code, body.get("reason")) == (2, "pipeline-locked"), stderr
    assert f"/{holder.pid}/" in body["lock"]["holder"]

    # 3. Kill the holder: the lock stays until its lease expires, then D takes over.
    holder.kill()
    holder.communicate(timeout=60)
    code, body, stderr = finish(start(tmp_path, name, "d", "deploy", target, "--plan"))
    assert (code, body.get("reason")) == (2, "pipeline-locked"), stderr

    def expired() -> bool:
        current = lease(client, name)
        return current["renewed"] + current["seconds"] < time.time() - 1

    wait_for(expired, 120)
    code, body, stderr = finish(
        start(tmp_path, name, "d", "deploy", target, "--auto-approve", "--json")
    )
    assert code == 0 and body["state"] == "ready", stderr
    assert "took over the expired lock of 'shared'" in stderr
    assert secret_digest(client, name) == digest  # carried over, not regenerated

    # Another runner reads the shared state: no lock held, several generations.
    code, body, stderr = finish(
        start(tmp_path, name, "e", "state", "show", "--spec", target)
    )
    assert code == 0, stderr
    assert body["backend"] == "cluster" and body["lock"] is None
    assert body["shared"]["generation"] > 1
