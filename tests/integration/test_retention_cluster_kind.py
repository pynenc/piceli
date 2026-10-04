"""Opt-in: retention of the in-cluster registry works out what to keep on its own.

Against a disposable kind (or k3s) cluster named by an explicit kubeconfig::

    PICELI_KIND_KUBECONFIG=/tmp/it.kubeconfig PICELI_KIND_CONTEXT=kind-piceli-it \\
      uv run pytest tests/integration/test_retention_cluster_kind.py

It needs ``docker`` (three small public images are pulled into the local
engine) and ``kubectl``, and never reads the ambient kubeconfig.

1. The in-cluster registry is installed (``piceli registry install``).
2. Three images are pushed by digest (no tag, no receipt kept): ``app`` v1
   and v2, and an orphan nothing uses.
3. A Deployment runs v1, then rolls to v2: v1 is its rollback target.
4. ``piceli artifacts retention --cluster infra.py:my_cluster`` (the Cluster
   reached through a credential profile) plans exactly the orphan, which
   only the storage listing finds; ``--delete --approve HASH`` deletes it;
   the registry's garbage collector dry run then succeeds.
5. ``registry status infra.py:my_cluster`` reports the claim's use.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from kind_support import kubectl, wait_registry_removed
from typer.testing import CliRunner

from piceli import App, Pipeline, Registry, Target
from piceli.artifacts.cli import main as artifacts_main
from piceli.k8s.cli import app

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
IMAGES = {"v1": "busybox:1.36", "v2": "busybox:1.37", "orphan": "alpine:3.20"}

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (
            KUBECONFIG
            and CONTEXT
            and shutil.which("docker")
            and shutil.which("kubectl")
        ),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT; needs docker",
    ),
]


def _image_id(image: str) -> str:
    subprocess.run(
        ["docker", "pull", "--quiet", image],
        check=True,
        capture_output=True,
        timeout=600,
    )
    return subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()


@pytest.fixture
def declared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """``infra.py`` declaring the cluster, reached through profile ``it``."""
    from piceli.infra.cluster import kubeconfig_server
    from piceli.k8s.ops.provider_factory import _read_kubeconfig
    from piceli.profiles import save_profile

    nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    node = nodes[0]["metadata"]["name"]
    server = kubeconfig_server(_read_kubeconfig(Path(KUBECONFIG)), CONTEXT)
    monkeypatch.setenv("PICELI_PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.chdir(tmp_path)
    save_profile("it", Path(KUBECONFIG), CONTEXT)
    (tmp_path / "infra.py").write_text(
        f"""
from piceli import Registry
from piceli.infra import Cluster

my_cluster = Cluster(
    "it",
    api={server!r},
    credentials="it",
    registry=Registry.in_cluster(on={node!r}, storage="1Gi"),
)
"""
    )
    try:
        yield node
    finally:
        _approved("uninstall", "--delete-storage")
        wait_registry_removed()


def _registry(command: str, *extra: str) -> Any:
    return CliRunner().invoke(app, ["registry", command, "infra.py:my_cluster", *extra])


def _approved(command: str, *extra: str) -> dict[str, Any]:
    planned = _registry(command, *extra)
    assert planned.exit_code in (0, 3), planned.output
    body = json.loads(planned.stdout)
    if body["state"] == "unchanged":
        return dict(body)
    done = _registry(command, *extra, "--approve", body["plan_hash"])
    assert done.exit_code == 0, done.output
    return dict(json.loads(done.stdout))


def _retention(capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, Any]:
    code = artifacts_main(["retention", "--cluster", "infra.py:my_cluster", *extra])
    out = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    return code, json.loads(out[-1])


def _rolled_out(namespace: str, revision: str) -> None:
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        found = json.loads(
            kubectl("get", "deployment", "app", "-o", "json", namespace=namespace)
        )
        status = found.get("status") or {}
        annotations = found["metadata"].get("annotations") or {}
        if (
            annotations.get("deployment.kubernetes.io/revision") == revision
            and status.get("updatedReplicas") == 1
            and status.get("readyReplicas") == 1
            and status.get("replicas") == 1
        ):
            return
        time.sleep(2)
    raise AssertionError(f"deployment app did not roll out revision {revision}")


def test_retention_keeps_live_and_rollback_and_removes_the_orphan(
    declared: str,
    kind_namespace: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from piceli.pipeline.backend import Backend
    from piceli.pipeline.runner import PipelineRunner

    installed = _approved("install")
    assert installed["state"] in {"installed", "unchanged"}
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        status = json.loads(_registry("status", "--json").stdout)
        if status["state"] == "ready":
            break
        time.sleep(3)
    assert status["state"] == "ready", status

    pipeline = Pipeline(
        App("e2e"),
        Target.kubeconfig(KUBECONFIG, context=CONTEXT, namespace=kind_namespace),
        deliver=Registry.in_cluster(on=declared, storage="1Gi"),
        state_dir=tmp_path / "state",
    )
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.pipeline = pipeline
    route = runner._route()
    refs = {}
    for name, image in IMAGES.items():
        repository = "e2e/orphan" if name == "orphan" else "e2e/app"
        receipt = Backend().registry_deliver(route, _image_id(image), repository)
        assert receipt["state"] == "succeeded", receipt.get("reason")
        refs[name] = receipt["pull_ref"]
    digest = {name: ref.rsplit("@", 1)[1] for name, ref in refs.items()}

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "app", "namespace": kind_namespace},
        "spec": {
            "replicas": 1,
            "revisionHistoryLimit": 5,
            "selector": {"matchLabels": {"app": "app"}},
            "template": {
                "metadata": {"labels": {"app": "app"}},
                "spec": {
                    "containers": [
                        {
                            "name": "main",
                            "image": refs["v1"],
                            "command": ["sh", "-c", "sleep 3600"],
                        }
                    ],
                    "terminationGracePeriodSeconds": 1,
                },
            },
        },
    }
    kubectl("apply", "-f", "-", stdin=json.dumps(deployment))
    _rolled_out(kind_namespace, "1")
    deployment["spec"]["template"]["spec"]["containers"][0]["image"] = refs["v2"]
    kubectl("apply", "-f", "-", stdin=json.dumps(deployment))
    _rolled_out(kind_namespace, "2")

    code, report = _retention(capsys)
    assert code == 0, report
    assert report["storage_listing"] == {"read": True}
    deletions = {row["digest"] for row in report["collectable"]["deletions"]}
    assert deletions == {digest["orphan"]}
    rows = {row["digest"]: row for row in report["manifests"]}
    assert "rollback" in rows[digest["v1"]]["reasons"]
    assert "live" in rows[digest["v2"]]["reasons"]

    code, asked = _retention(capsys, "--delete")
    assert code == 3 and asked["approve_command"].endswith(asked["digest"])
    code, done = _retention(capsys, "--delete", "--approve", asked["digest"])
    assert code == 0 and done["state"] == "deleted", done
    assert {row["digest"] for row in done["deleted"]} == {digest["orphan"]}

    gc = kubectl(
        "exec", "deploy/piceli-registry", "--", "registry", "garbage-collect",
        "--dry-run", "/etc/distribution/config.yml", namespace="piceli-system",
    )  # fmt: skip
    assert "blobs marked" in gc

    status = json.loads(_registry("status", "--json").stdout)
    storage = status["storage"]
    assert storage["used_source"] in {"volume-stats", "du"}, storage
    assert isinstance(storage["used_bytes"], int) and storage["used_bytes"] > 0
