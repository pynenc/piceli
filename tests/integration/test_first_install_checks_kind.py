"""Opt-in: a first install's checks on a disposable kind cluster, without kubectl.

Runs only when both variables name a disposable cluster, for example::

    kind create cluster --name piceli-first --kubeconfig /tmp/first.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/first.kubeconfig \\
      PICELI_KIND_CONTEXT=kind-piceli-first \\
      uv run pytest tests/integration/test_first_install_checks_kind.py

A fresh namespace gets a release whose pre-rollout check reads a Secret and a
ConfigMap the release itself creates (0.14.1 failed ``prerollout-failed``
here), and whose ``Checks.http`` runs with no ``kubectl`` on ``PATH``, as in
the controller image (0.14.1 failed ``check-forward-unavailable``).
"""

from __future__ import annotations

import json
import os
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
# nginx:1.27-alpine multi-arch index digest (as in test_prerollout_kind).
IMAGE = "nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT",
    ),
]

APP = textwrap.dedent(
    """\
    import os
    from piceli import App, Checks, Pipeline, Random, Secrets, Target

    target = Target.kubeconfig(
        os.environ["PICELI_KIND_KUBECONFIG"], context=os.environ["PICELI_KIND_CONTEXT"],
        namespace=os.environ["FIRST_NAMESPACE"], request_seconds=30,
    )
    app = App("first")
    secrets = Secrets(token=Random(24))
    credentials = app.secret("credentials", {"token": secrets.ref("token")})
    settings = app.config("settings", {"mode": "fast"})
    web = app.deployment(
        "web", image=os.environ["FIRST_IMAGE"], ports=[80],
        env={"TOKEN": credentials.key("token"), "MODE": settings.key("mode")},
    )
    service = app.service(web, port=80)
    app.pre_rollout(
        web, ["sh", "-c", 'test ${#TOKEN} -ge 24 && test "$MODE" = fast'],
    )
    pipeline = Pipeline(
        app, target, secrets=secrets, state_dir=os.environ["FIRST_STATE"],
        checks=[Checks.http(service, "/", expect=200, body_contains="nginx")],
        execution={"readiness_seconds": 240, "max_seconds": 600},
    )
    """
)


@pytest.fixture
def namespace():
    from kubernetes.client import CoreV1Api

    name = "first-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    api = CoreV1Api(client)
    api.create_namespace({"metadata": {"name": name}})
    try:
        yield name, client
    finally:
        api.delete_namespace(name)
        client.close()


def _path_without_kubectl() -> str:
    return os.pathsep.join(
        entry
        for entry in os.environ["PATH"].split(os.pathsep)
        if entry and not (Path(entry) / "kubectl").exists()
    )


def test_a_first_install_checks_with_its_own_secrets_and_no_kubectl(
    tmp_path: Path, namespace
) -> None:
    from kubernetes.client import CoreV1Api

    name, client = namespace
    (tmp_path / "app.py").write_text(APP)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "piceli",
            "deploy",
            "app.py:pipeline",
            "--auto-approve",
            "--json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
        env={
            "PATH": _path_without_kubectl(),
            "HOME": str(tmp_path),
            "PICELI_KIND_KUBECONFIG": KUBECONFIG,
            "PICELI_KIND_CONTEXT": CONTEXT,
            "FIRST_NAMESPACE": name,
            "FIRST_STATE": str(tmp_path / "state"),
            "FIRST_IMAGE": IMAGE,
        },
    )
    out = result.stdout + result.stderr
    events: list[dict[str, Any]] = [
        json.loads(line) for line in result.stdout.splitlines() if line.strip()
    ]
    assert result.returncode == 0, out
    final = events[-1]
    assert final["stages"]["prerollout"] == "done", out
    assert final["stages"]["checks"] == "done", out
    record = json.loads(
        sorted((tmp_path / "state" / "runs").glob("*.json"))[-1].read_text()
    )
    (check,) = record["stages"]["prerollout"]["output"]["checks"]
    assert check["state"] == "passed"
    assert check["staged"] == ["ConfigMap/settings", "Secret/credentials"]
    results = record["stages"]["checks"]["output"]["results"]
    assert [item["passed"] for item in results] == [True]
    # No staged copy is left; the release's own objects are in place.
    core = CoreV1Api(client)
    secrets = {item.metadata.name for item in core.list_namespaced_secret(name).items}
    configs = {
        item.metadata.name for item in core.list_namespaced_config_map(name).items
    }
    assert "credentials" in secrets and "settings" in configs
    assert not [n for n in secrets | configs if "-chk" in n]
