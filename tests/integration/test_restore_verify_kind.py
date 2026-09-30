"""kind: prove a restore point in scratch claims without touching live data.

A StatefulSet pair (claim templates) and a Deployment on an existing claim
write markers; a release with a new image takes a restore point.
``piceli restore --to-new-claim`` restores it into scratch claims and runs
each workload's read-only verify command (a marker format check): every claim
passes, the scratch claims are gone afterwards and the writers' pods are the
same pods (UIDs unchanged). A second restore point taken after a marker was
damaged fails for that claim only, and is cleaned up as well. Needs network
access to pull two public busybox images.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from kind_support import CONTEXT, KUBECONFIG, kubectl, requires_kind, wait_for
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli

pytestmark = [requires_kind, pytest.mark.timeout(900)]

# busybox:1.36 and busybox:1.37 multi-arch index digests.
V1 = "docker.io/library/busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
V2 = "docker.io/library/busybox@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"

PIPELINE = """
from piceli import (
    App, ClaimTemplate, ExistingClaim, Pipeline, Quiesce, RestorePoints,
    RestoreVerify, Target,
)

target = Target.kubeconfig({kubeconfig!r}, context={context!r}, namespace={namespace!r})
app = App("store")
WRITE = 'test -f /data/marker || echo v1-$HOSTNAME > /data/marker; trap "exit 0" TERM; while true; do sleep 1; done'
db = app.stateful_set(
    "db", image={image!r}, replicas=2, command=["sh", "-c", WRITE],
    volumes={{"/data": ClaimTemplate("data", size="64Mi")}},
    termination_grace_seconds=5,
)
app.quiesce(db, Quiesce.exec(["sync"]))
app.restore_verify(
    db, RestoreVerify(["sh", "-c", "grep -qE '^v1-db-[0-9]+$' /data/marker"])
)
cache = app.deployment(
    "cache", image={image!r}, command=["sh", "-c", WRITE],
    volumes={{"/data": ExistingClaim("cache-state")}},
)
app.restore_verify(
    cache, RestoreVerify(["sh", "-c", "grep -qE '^v1-cache-' /data/marker"])
)
pipeline = Pipeline(
    app, target, state_dir="state",
    restore_points=RestorePoints(timeout_seconds=180),
    execution={{"max_seconds": 300, "readiness_seconds": 240}},
)
"""


def _write(path: Path, namespace: str, image: str) -> None:
    (path / "app.py").write_text(
        PIPELINE.format(
            kubeconfig=os.path.abspath(KUBECONFIG),
            context=CONTEXT,
            namespace=namespace,
            image=image,
        )
    )
    import hashlib
    import shutil
    import sys

    shutil.rmtree(path / "__pycache__", ignore_errors=True)
    name = (
        "_piceli_render_"
        + hashlib.sha256(str((path / "app.py").resolve()).encode()).hexdigest()[:16]
    )
    sys.modules.pop(name, None)


def _run(*args: str) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(cli, list(args))
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    return result.exit_code, json.loads(lines[-1]) if lines else {}, result.stderr


def _marker(namespace: str, pod: str) -> str:
    return kubectl(
        "exec", pod, "--", "cat", "/data/marker", namespace=namespace
    ).strip()


def _pods(namespace: str) -> dict[str, str]:
    items = json.loads(kubectl("get", "pods", "-o", "json", namespace=namespace))
    return {
        pod["metadata"]["name"]: pod["metadata"]["uid"]
        for pod in items["items"]
        if pod["metadata"]["name"].startswith(("db-", "cache-"))
        and not pod["metadata"].get("deletionTimestamp")
    }


def _ready(namespace: str, image: str) -> bool:
    value = json.loads(
        kubectl("get", "statefulset", "db", "-o", "json", namespace=namespace)
    )
    status = value.get("status") or {}
    return (
        value["spec"]["template"]["spec"]["containers"][0]["image"] == image
        and status.get("readyReplicas") == 2
        and status.get("updatedReplicas") == 2
    )


def _release(path: Path, namespace: str, image: str, target: str) -> str:
    _write(path, namespace, image)
    code, body, stderr = _run("deploy", target, "--auto-approve")
    assert code == 0, stderr
    wait_for(lambda: _ready(namespace, image), message=f"db on {image[-8:]}")
    return str(body.get("restore_point") or "")


def _verify(target: str, point: str) -> tuple[int, dict[str, Any], str]:
    args = ("restore", target, "--point", point, "--to-new-claim")
    code, planned, stderr = _run(*args)
    assert code == 3, stderr
    assert planned["state"] == "approval-required"
    return _run(*args, "--approve", planned["verify_hash"])


def _scratch_left(namespace: str) -> str:
    return kubectl(
        "get", "pvc", "-l", "piceli.io/scratch=true", "-o", "name", namespace=namespace
    ).strip()


def test_restore_into_scratch_claims_verifies_without_touching_live_data(
    kind_namespace: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace = kind_namespace
    monkeypatch.chdir(tmp_path)
    kubectl(
        "apply",
        "-f",
        "-",
        namespace=namespace,
        stdin=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": "cache-state"},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": "64Mi"}},
                },
            }
        ),
    )
    target = str(tmp_path / "app.py") + ":pipeline"
    assert _release(tmp_path, namespace, V1, target) == ""
    wait_for(lambda: _marker(namespace, "db-1") == "v1-db-1", message="db-1 marker")
    good = _release(tmp_path, namespace, V2, target)
    assert good.startswith("rp-")

    pods = _pods(namespace)
    markers = {pod: _marker(namespace, pod) for pod in pods}
    code, body, stderr = _verify(target, good)
    assert code == 0, stderr
    assert body["state"] == "passed"
    (receipt,) = body["receipts"]
    assert {item["claim"]: item["result"] for item in receipt["claims"]} == {
        "cache-state": "PASS",
        "data-db-0": "PASS",
        "data-db-1": "PASS",
    }
    assert {item["verify"] for item in receipt["claims"]} == {"passed"}
    assert "v1-db-0" not in stderr + json.dumps(body)
    assert _scratch_left(namespace) == ""
    assert kubectl("get", "jobs", "-o", "name", namespace=namespace).strip() == ""
    assert _pods(namespace) == pods  # the same pods: writers never stopped
    assert {pod: _marker(namespace, pod) for pod in pods} == markers

    # Damage one replica's data, take a restore point of it, verify: FAIL.
    kubectl(
        "exec",
        "db-0",
        "--",
        "sh",
        "-c",
        "echo damaged > /data/marker",
        namespace=namespace,
    )
    damaged = _release(tmp_path, namespace, V1, target)
    assert damaged.startswith("rp-") and damaged != good
    pods = _pods(namespace)
    code, body, stderr = _verify(target, damaged)
    assert code == 1, stderr
    assert (body["state"], body["reason"]) == ("failed", "restore-verify-failed")
    (receipt,) = body["receipts"]
    results = {item["claim"]: item["result"] for item in receipt["claims"]}
    assert results == {
        "cache-state": "PASS",
        "data-db-0": "FAIL",
        "data-db-1": "PASS",
    }
    assert _scratch_left(namespace) == ""
    assert _pods(namespace) == pods
    assert _marker(namespace, "db-0") == "damaged"
