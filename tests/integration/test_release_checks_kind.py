"""Opt-in: a broken image fails its checks and is rolled back on kind.

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-checks --kubeconfig /tmp/checks.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/checks.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-checks \\
    PICELI_KIND_NODE=piceli-checks-control-plane \\
      uv run pytest tests/integration/test_release_checks_kind.py

It needs ``docker`` and ``kubectl`` on ``PATH`` and never reads the ambient
kubeconfig. In a uniquely named namespace it builds the good and the broken
image of ``examples/checks``, imports each into the node under a content tag
(``piceli artifacts deliver --to docker://…``), releases the good one (its
http, exec and Python checks pass), then releases the broken one: it becomes
ready, fails its checks and the good release is re-applied without operator
action. The result and the history record the failed checks and the rollback.
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

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
NODE = os.environ.get("PICELI_KIND_NODE", "")
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "checks"

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


@pytest.fixture
def namespace():
    from kubernetes.client import CoreV1Api

    name = "checks-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    api = CoreV1Api(client)
    api.create_namespace({"metadata": {"name": name}})
    try:
        yield name
    finally:
        api.delete_namespace(name)
        client.close()


def _spec(directory: Path, namespace: str) -> Path:
    for name in ("composition.py", "checks.py", "Dockerfile"):
        shutil.copy(EXAMPLE / name, directory / name)
    text = (EXAMPLE / "release.toml").read_text()
    _, _, tail = text.partition("[release]")
    target = textwrap.dedent(
        f"""
        [target]
        kubeconfig = "{KUBECONFIG}"
        context = "{CONTEXT}"
        namespace = "{namespace}"

        """
    )
    path = directory / "release.toml"
    path.write_text(target + "[release]" + tail)
    return path


def _build(directory: Path, login: str) -> str:
    tag = f"piceli-e2e/checks-web:{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "build", "-q", "--build-arg", f"LOGIN={login}", "-t", tag]
        + [str(directory)],
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _deliver(directory: Path, image_id: str) -> None:
    receipt = directory / "web.delivery.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "piceli",
            "artifacts",
            "deliver",
            "--image",
            image_id,
            "--approve-digest",
            image_id,
            "--ref",
            f"registry.test/checks/web:sha256-{image_id[7:19]}",
            "--to",
            f"docker://{NODE}?runtime=containerd",
            "--receipt",
            str(receipt),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def _run(spec: Path, *args: str) -> tuple[int, dict, str]:
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}"), result.stderr


def _image(namespace: str) -> str:
    from kubernetes.client import AppsV1Api

    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    try:
        deployment = AppsV1Api(client).read_namespaced_deployment("web", namespace)
        return str(deployment.spec.template.spec.containers[0].image)
    finally:
        client.close()


def test_broken_image_fails_its_checks_and_is_rolled_back(tmp_path, namespace):
    spec = _spec(tmp_path, namespace)
    good = _build(tmp_path, "yes")
    broken = _build(tmp_path, "no")
    assert good != broken

    _deliver(tmp_path, good)
    code, first, stderr = _run(spec, "apply", "--auto-approve")
    assert code == 0, (first, stderr)
    assert first["release_state"] == "ready"
    assert first["checks"]["passed"], first["checks"]
    assert [item["name"] for item in first["checks"]["results"]] == [
        "login",
        "login-file",
        "login-python",
    ]
    assert good[7:19] in _image(namespace)

    _deliver(tmp_path, broken)
    code, failed, stderr = _run(spec, "apply", "--auto-approve")
    assert code == 1, (failed, stderr)
    assert failed["execution"]["state"] == "ready"  # the broken image is ready
    assert failed["release_state"] == "checks-failed"
    assert failed["checks"]["failed"] == ["login", "login-file", "login-python"]
    login = failed["checks"]["results"][0]
    assert login["code"] == "check-failed"
    assert "returned 404" in login["detail"]
    rollback = failed["rollback"]
    assert rollback["state"] == "rolled-back", rollback
    assert rollback["target"] == first["release"]
    assert rollback["checks"]["passed"]
    assert good[7:19] in _image(namespace)

    code, status, _ = _run(spec, "status")
    assert code == 0
    assert status["selected"] == first["release"]
    assert status["deployed"] == first["release"]
    entries = status["history"]
    assert [entry["state"] for entry in entries] == ["ready", "checks-failed", "ready"]
    assert entries[1]["checks"]["failed"] == ["login", "login-file", "login-python"]
    assert entries[1]["rollback"]["state"] == "rolled-back"
    assert entries[2]["trigger"] == "checks-failed"
    assert entries[2]["rolled_back_from"] == failed["release"]
    print(json.dumps({"failed": failed, "status": status}, indent=2, sort_keys=True))
