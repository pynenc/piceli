"""kind: restore points around a release that changes a StatefulSet's image.

A StatefulSet with two replicas (claim templates) and a Deployment mounting an
existing claim each write a marker. A release with a new image takes a restore
point of all three claims: a watcher confirms that no writer pod exists while
a helper pod copies. The markers are then damaged; ``piceli restore`` brings
them back. Needs network access to pull two public busybox images.
"""

from __future__ import annotations

import json
import os
import threading
import time
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
    App, ClaimTemplate, ExistingClaim, Pipeline, Quiesce, RestorePoints, Target,
)

target = Target.kubeconfig({kubeconfig!r}, context={context!r}, namespace={namespace!r})
app = App("store")
# The first version to start on an empty claim writes its marker.
WRITE = 'test -f /data/marker || echo {version}-$HOSTNAME > /data/marker; trap "exit 0" TERM; while true; do sleep 1; done'
db = app.stateful_set(
    "db", image={image!r}, replicas=2, command=["sh", "-c", WRITE],
    volumes={{"/data": ClaimTemplate("data", size="64Mi")}},
    termination_grace_seconds=5,
)
app.quiesce(db, Quiesce.exec(["sync"]))
app.deployment(
    "cache", image={image!r}, command=["sh", "-c", WRITE],
    volumes={{"/data": ExistingClaim("cache-state")}},
)
pipeline = Pipeline(
    app, target, state_dir="state",
    restore_points=RestorePoints(timeout_seconds=180),
    execution={{"max_seconds": 300, "readiness_seconds": 240}},
)
"""


def _write(path: Path, namespace: str, image: str, version: str) -> None:
    (path / "app.py").write_text(
        PIPELINE.format(
            kubeconfig=os.path.abspath(KUBECONFIG),
            context=CONTEXT,
            namespace=namespace,
            image=image,
            version=version,
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


def _run(path: Path, *args: str) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(cli, list(args))
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    return result.exit_code, json.loads(lines[-1]) if lines else {}, result.stderr


def _marker(namespace: str, pod: str) -> str:
    return kubectl(
        "exec", pod, "--", "cat", "/data/marker", namespace=namespace
    ).strip()


def _rolled(namespace: str, image: str) -> bool:
    value = json.loads(
        kubectl("get", "statefulset", "db", "-o", "json", namespace=namespace)
    )
    status = value.get("status") or {}
    return (
        value["spec"]["template"]["spec"]["containers"][0]["image"] == image
        and status.get("readyReplicas") == 2
        and status.get("updatedReplicas") == 2
    )


class Watcher(threading.Thread):
    """Polls pods; records any moment a writer and a backup helper coexist."""

    def __init__(self, namespace: str) -> None:
        super().__init__(daemon=True)
        self.namespace, self.stop, self.overlap, self.helpers = (
            namespace,
            False,
            [],
            set(),
        )

    def run(self) -> None:
        while not self.stop:
            pods = json.loads(
                kubectl(
                    "get", "pods", "-o", "json", namespace=self.namespace, check=False
                )
                or "{}"
            ).get("items", [])
            names = [pod["metadata"]["name"] for pod in pods]
            helpers = [name for name in names if name.startswith("piceli-backup-")]
            writers = [name for name in names if name.startswith(("db-", "cache-"))]
            self.helpers.update(helpers)
            if helpers and writers:
                self.overlap.append((helpers, writers))
            time.sleep(0.5)


def test_a_release_takes_and_verifies_a_restore_point_and_restore_brings_it_back(
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
    _write(tmp_path, namespace, V1, "v1")
    code, body, stderr = _run(tmp_path, "deploy", target, "--auto-approve")
    assert code == 0, stderr
    assert body["stages"]["backup"] == "skipped"
    wait_for(lambda: _marker(namespace, "db-1").startswith("v1"), message="db-1 v1")

    _write(tmp_path, namespace, V2, "v2")
    code, body, stderr = _run(tmp_path, "deploy", target, "--plan")
    assert code == 0, stderr
    assert "backup   claim data-db-0 of StatefulSet/db" in stderr
    assert "backup   claim cache-state of Deployment/cache" in stderr
    watcher = Watcher(namespace)
    watcher.start()
    try:
        code, body, stderr = _run(
            tmp_path, "deploy", target, "--approve", body["combined_hash"]
        )
    finally:
        watcher.stop = True
        watcher.join(timeout=10)
    assert code == 0, stderr
    assert body["stages"]["backup"] == "done"
    point = body["restore_point"]
    assert watcher.helpers, "no helper pod was seen"
    assert watcher.overlap == [], "a writer pod existed while a helper copied"
    code, listed, stderr = _run(tmp_path, "restore-points", target, "--verify")
    assert code == 0, stderr
    (entry,) = [item for item in listed["points"] if item["id"] == point]
    assert sorted(item["claim"] for item in entry["claims"]) == [
        "cache-state",
        "data-db-0",
        "data-db-1",
    ]
    assert {item["check"] for item in entry["claims"]} == {"verified"}
    assert kubectl("get", "jobs", "-o", "name", namespace=namespace).strip() == ""
    wait_for(lambda: _rolled(namespace, V2), message="db on v2 and ready")
    wait_for(lambda: _marker(namespace, "db-0") == "v1-db-0", message="db-0 v1 data")
    kubectl(
        "exec",
        "db-0",
        "--",
        "sh",
        "-c",
        "echo damaged > /data/marker",
        namespace=namespace,
    )

    code, planned, stderr = _run(tmp_path, "restore", target, "--point", point)
    assert code == 3, stderr
    restore_hash = planned["plan"]["restore_hash"]
    assert planned["plan"]["writers"] == ["Deployment/cache", "StatefulSet/db"]
    code, done, stderr = _run(
        tmp_path, "restore", target, "--point", point, "--approve", restore_hash
    )
    assert code == 0, stderr
    assert done["state"] == "restored"
    assert all(item["verified"] for item in done["receipt"]["claims"])
    assert "v1-db-0" not in stderr + json.dumps(done)
    wait_for(
        lambda: (
            kubectl(
                "get",
                "statefulset",
                "db",
                "-o",
                "jsonpath={.status.readyReplicas}",
                namespace=namespace,
            ).strip()
            == "2"
        ),
        message="db ready again",
    )
    wait_for(lambda: _marker(namespace, "db-0") == "v1-db-0", message="db-0 restored")
