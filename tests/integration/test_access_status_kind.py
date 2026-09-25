"""Opt-in: ``piceli status`` and ``piceli access`` against a disposable kind cluster.

Runs only when both variables name a disposable cluster, for example::

    kind create cluster --name piceli-access --kubeconfig /tmp/access.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/access.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-access \\
      uv run pytest tests/integration/test_access_status_kind.py

The test never reads the ambient kubeconfig (every command gets the explicit
file and context). In a fresh namespace it checks that ``status`` reports the
app down, releases an nginx Service that declares access, checks that
``status`` reports it up with its image digest, runs ``piceli access`` with the
real kubectl, fetches the page through the forward, checks that a second
``access`` is refused with the owner of the port, and stops everything.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app
from piceli.k8s.observe import local_port_in_use
from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
# nginx:1.27-alpine multi-arch index digest.
DIGEST = "sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
REPO = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT",
    ),
    pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl"),
]

COMPOSITION = textwrap.dedent(
    """\
    from piceli import App


    def build(ctx):
        app = App("shop")
        web = app.deployment(
            "web",
            image=ctx.image("web"),
            ports=[80],
            ready=app.probe.http("/", 80),
        )
        app.service(
            web,
            port=80,
            access=app.access.forward(local=ctx.values["local"], health="/"),
        )
        return app
    """
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def namespace():
    from kubernetes.client import CoreV1Api

    name = "access-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    api = CoreV1Api(client)
    api.create_namespace({"metadata": {"name": name}})
    try:
        yield name
    finally:
        api.delete_namespace(name)
        client.close()


def _status(spec: Path) -> tuple[int, dict[str, Any]]:
    result = CliRunner().invoke(app, ["status", str(spec), "--json"])
    return result.exit_code, json.loads(result.stdout)


def test_status_and_access_on_kind(tmp_path: Path, namespace: str) -> None:
    local = _free_port()
    (tmp_path / "shop_app.py").write_text(COMPOSITION)
    spec = tmp_path / "release.toml"
    spec.write_text(
        textwrap.dedent(
            f"""
            [target]
            kubeconfig = "{KUBECONFIG}"
            context = "{CONTEXT}"
            namespace = "{namespace}"

            [release]
            name = "shop"
            owner = "access-e2e"
            field_manager = "access-e2e"
            composition = "shop_app.py:build"
            state_dir = "state"

            [execution]
            max_seconds = 300
            readiness_seconds = 240

            [images]
            web = "docker.io/library/nginx@{DIGEST}"

            [values]
            local = {local}
            """
        )
    )
    code, before = _status(spec)
    assert code == 1 and before["state"] == "down", before
    assert before["workloads"][0]["health"] == "missing"

    runner = CliRunner()
    planned = runner.invoke(app, ["release", "plan", "--spec", str(spec)])
    assert planned.exit_code == 0, planned.output
    plan_hash = json.loads(planned.stdout)["plan_hash"]
    applied = runner.invoke(
        app, ["release", "apply", "--spec", str(spec), "--approve", plan_hash]
    )
    assert applied.exit_code == 0, applied.output

    code, up = _status(spec)
    assert code == 0 and up["state"] == "up", up
    assert up["release"]["state"] == "ready"
    (web,) = up["workloads"]
    assert web["health"] == "ready"
    assert web["images"][0]["digest"] == DIGEST
    assert up["access"]["forwards"][0]["forward"] == "down"

    process = subprocess.Popen(
        [sys.executable, "-m", "piceli", "access", str(spec), "--json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO,
        env={**os.environ, "KUBECONFIG": KUBECONFIG},
    )
    try:
        assert process.stdout is not None
        deadline = time.monotonic() + 60
        healthy = False
        while time.monotonic() < deadline and not healthy:
            line = process.stdout.readline()
            if not line:
                break
            healthy = json.loads(line).get("health") == "healthy"
        assert healthy, process.stderr.read() if process.poll() is not None else ""
        with urlopen(f"http://127.0.0.1:{local}/", timeout=10) as response:
            assert response.status == 200
            assert b"nginx" in response.read()
        code, reached = _status(spec)
        forward = reached["access"]["forwards"][0]
        assert forward["forward"] == "up", forward
        assert forward["url"] == f"http://127.0.0.1:{local}/"
        second = runner.invoke(app, ["access", str(spec), "--json"])
        assert second.exit_code == 2
        rejection = json.loads(second.stdout)
        assert rejection["reason"] == "access-port-conflict"
        owner = rejection["conflicts"][0]["owner"]
        if owner is not None:
            assert "port-forward" in (owner["command"] or "")
            assert owner["parent"]["pid"] == process.pid
    finally:
        process.send_signal(signal.SIGINT)
        try:
            process.communicate(timeout=20)
        finally:
            if process.poll() is None:
                process.kill()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and local_port_in_use(local):
        time.sleep(0.1)
    assert not local_port_in_use(local)
