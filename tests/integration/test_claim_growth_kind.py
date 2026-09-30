"""kind: move retained claims to larger ones on a class without expansion.

kind's default class (``standard``, the local-path provisioner) has no
``allowVolumeExpansion``, so a larger claim needs a move. A StatefulSet
replica (claim template ``data``) and a Deployment on an existing claim
write markers; the next release asks for larger claims with
``migrate_from=``. The deploy takes a restore point with the writers
stopped, fills each new claim from it, checks the content digest and the
workloads' ``restore_verify`` commands on the copies, and the apply switches
the workloads (the StatefulSet is recreated, its pods and claims orphaned).
Afterwards the workloads read their markers from the new claims, the old
claims are kept and annotated, and a later release has nothing to move.
Needs network access to pull a public busybox image.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from kind_support import CONTEXT, KUBECONFIG, kubectl, requires_kind, wait_for
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli

pytestmark = [requires_kind, pytest.mark.timeout(900)]

# busybox:1.36 multi-arch index digest.
IMAGE = "docker.io/library/busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"

PIPELINE = """
from piceli import (
    App, ClaimTemplate, ExistingClaim, Pipeline, Quiesce, RestorePoints,
    RestoreVerify, Target,
)

target = Target.kubeconfig({kubeconfig!r}, context={context!r}, namespace={namespace!r})
app = App("store")
WRITE = 'test -f /data/marker || echo v1-$HOSTNAME > /data/marker; trap "exit 0" TERM; while true; do sleep 1; done'
db = app.stateful_set(
    "db", image={image!r}, replicas=1, command=["sh", "-c", WRITE],
    volumes={{"/data": {template}}}, termination_grace_seconds=5,
)
app.quiesce(db, Quiesce.exec(["sync"]))
app.restore_verify(db, RestoreVerify(["sh", "-c", "grep -qx v1-db-0 /data/marker"]))
observer = app.deployment(
    "observer", image={image!r}, replicas=1, command=["sh", "-c", WRITE],
    volumes={{"/data": {claim}}}, termination_grace_seconds=5,
)
app.restore_verify(
    observer, RestoreVerify(["sh", "-c", "grep -q ^v1-observer- /data/marker"])
)
pipeline = Pipeline(
    app, target, state_dir="state", replace=["StatefulSet/db"],
    restore_points=RestorePoints(timeout_seconds=180),
    execution={{"max_seconds": 300, "readiness_seconds": 240}},
)
"""
BEFORE = ('ClaimTemplate("data", size="64Mi")', 'ExistingClaim("observer-state")')
AFTER = (
    'ClaimTemplate("data-2", size="256Mi", migrate_from="data")',
    'ExistingClaim("observer-state-2", size="8Gi", migrate_from="observer-state")',
)


def _write(path: Path, namespace: str, volumes: tuple[str, str]) -> None:
    (path / "app.py").write_text(
        PIPELINE.format(
            kubeconfig=os.path.abspath(KUBECONFIG),
            context=CONTEXT,
            namespace=namespace,
            image=IMAGE,
            template=volumes[0],
            claim=volumes[1],
        )
    )
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


def _claim(namespace: str, name: str) -> dict[str, Any]:
    return json.loads(kubectl("get", "pvc", name, "-o", "json", namespace=namespace))


def _pod(namespace: str, prefix: str) -> dict[str, Any] | None:
    items = json.loads(kubectl("get", "pods", "-o", "json", namespace=namespace))
    ready = [
        pod
        for pod in items["items"]
        if pod["metadata"]["name"].startswith(prefix)
        and not pod["metadata"].get("deletionTimestamp")
        and any(
            item.get("type") == "Ready" and item.get("status") == "True"
            for item in (pod.get("status") or {}).get("conditions") or ()
        )
    ]
    return ready[0] if len(ready) == 1 else None


def _mounted(pod: dict[str, Any]) -> str:
    (claim,) = [
        volume["persistentVolumeClaim"]["claimName"]
        for volume in pod["spec"]["volumes"]
        if "persistentVolumeClaim" in volume
    ]
    return str(claim)


def _marker(namespace: str, pod: str) -> str:
    return kubectl(
        "exec", pod, "--", "cat", "/data/marker", namespace=namespace
    ).strip()


def test_claims_move_to_larger_claims_and_the_workloads_read_their_markers(
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
                "metadata": {"name": "observer-state"},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": "512Mi"}},
                },
            }
        ),
    )
    target = str(tmp_path / "app.py") + ":pipeline"
    _write(tmp_path, namespace, BEFORE)
    code, _body, stderr = _run("deploy", target, "--auto-approve")
    assert code == 0, stderr
    wait_for(lambda: _pod(namespace, "db-") is not None, message="db-0 ready")
    wait_for(lambda: _pod(namespace, "observer-") is not None, message="observer")
    wait_for(lambda: _marker(namespace, "db-0") == "v1-db-0", message="db marker")
    observer = _pod(namespace, "observer-")
    assert observer is not None
    before = _marker(namespace, observer["metadata"]["name"])
    assert before.startswith("v1-observer-")

    _write(tmp_path, namespace, AFTER)
    code, planned, stderr = _run("deploy", target, "--plan")
    assert code == 0, stderr
    assert (
        "move claim data-db-0 of StatefulSet/db (64Mi) to new claim data-2-db-0"
        in stderr
    )
    assert "move claim observer-state of Deployment/observer (512Mi)" in stderr
    code, body, stderr = _run("deploy", target, "--approve", planned["combined_hash"])
    assert code == 0, stderr
    assert body["state"] == "ready", body
    assert body["restore_point"].startswith("rp-")
    assert "v1-db-0" not in stderr + json.dumps(body)

    # The workloads run on the new claims and read the copied markers.
    wait_for(
        lambda: (
            (pod := _pod(namespace, "db-")) is not None
            and _mounted(pod) == "data-2-db-0"
        ),
        message="db-0 on data-2-db-0",
    )
    wait_for(
        lambda: (
            (pod := _pod(namespace, "observer-")) is not None
            and _mounted(pod) == "observer-state-2"
        ),
        message="observer on observer-state-2",
    )
    assert _marker(namespace, "db-0") == "v1-db-0"
    observer = _pod(namespace, "observer-")
    assert observer is not None
    assert _marker(namespace, observer["metadata"]["name"]) == before
    for old, new, size in (
        ("data-db-0", "data-2-db-0", "256Mi"),
        ("observer-state", "observer-state-2", "8Gi"),
    ):
        kept, made = _claim(namespace, old), _claim(namespace, new)
        assert kept["status"]["phase"] == "Bound"
        assert kept["metadata"]["annotations"]["piceli.io/migrated-to"] == new
        assert made["metadata"]["annotations"]["piceli.io/migrated-from"] == old
        assert made["spec"]["resources"]["requests"]["storage"] == size
    assert kubectl("get", "jobs", "-o", "name", namespace=namespace).strip() == ""

    # Nothing left to move: the next release stops nothing.
    code, body, stderr = _run("deploy", target, "--auto-approve")
    assert code == 0, stderr
    assert body["stages"]["backup"] == "skipped", body
