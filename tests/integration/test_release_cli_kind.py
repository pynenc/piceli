"""Opt-in end-to-end ``piceli release`` run against a disposable kind cluster.

Runs only when both variables name a disposable cluster, for example::

    kind create cluster --name piceli-release --kubeconfig /tmp/release.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/release.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-release \\
      uv run pytest tests/integration/test_release_cli_kind.py

The test never reads the ambient kubeconfig. It creates a uniquely named
namespace, applies the example composition (``examples/release``), applies a
second release with a different image digest, rolls back, and deletes the
namespace.
"""

from __future__ import annotations

import json
import os
import shutil
import textwrap
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
# nginx:1.27-alpine and nginx:1.26-alpine multi-arch index digests.
DIGEST_1 = "sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
DIGEST_2 = "sha256:1eadbb07820339e8bbfed18c771691970baee292ec4ab2558f1453d26153e22d"
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "release"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT",
    ),
]


@pytest.fixture
def namespace():
    from kubernetes.client import CoreV1Api

    name = "release-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    api = CoreV1Api(client)
    api.create_namespace({"metadata": {"name": name}})
    try:
        yield name
    finally:
        api.delete_namespace(name)
        client.close()


def _spec(directory: Path, namespace: str, digest: str) -> Path:
    shutil.copy(EXAMPLE / "composition.py", directory / "composition.py")
    path = directory / "release.toml"
    path.write_text(
        textwrap.dedent(
            f"""
            [target]
            kubeconfig = "{KUBECONFIG}"
            context = "{CONTEXT}"
            namespace = "{namespace}"

            [release]
            name = "web"
            owner = "release-e2e"
            field_manager = "release-e2e"
            composition = "composition.py:build"
            state_dir = "state"

            [execution]
            max_seconds = 300
            readiness_seconds = 240

            [images]
            web = "docker.io/library/nginx@{digest}"

            [secrets.api-token]
            type = "random"

            [secrets.web-tls]
            type = "tls-self-signed"
            dns_names = ["web.{namespace}.svc"]
            """
        )
    )
    return path


def _run(spec: Path, *args: str) -> tuple[int, dict]:
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}")


def _image(namespace: str) -> str:
    from kubernetes.client import AppsV1Api

    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    try:
        deployment = AppsV1Api(client).read_namespaced_deployment("web", namespace)
        return str(deployment.spec.template.spec.containers[0].image)
    finally:
        client.close()


def test_plan_apply_digest_change_and_rollback_on_kind(tmp_path, namespace):
    spec = _spec(tmp_path, namespace, DIGEST_1)
    code, planned = _run(spec, "plan")
    assert code == 0, planned
    code, applied = _run(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, applied
    assert _image(namespace).endswith(DIGEST_1)

    spec = _spec(tmp_path, namespace, DIGEST_2)
    code, second = _run(spec, "apply", "--auto-approve")
    assert code == 0, second
    assert second["release"] != applied["release"]
    assert _image(namespace).endswith(DIGEST_2)

    code, rolled = _run(spec, "rollback", "previous", "--auto-approve")
    assert code == 0, rolled
    assert rolled["release"] == applied["release"]
    assert _image(namespace).endswith(DIGEST_1)

    code, status = _run(spec, "status")
    identities = {item["source"]["identity"] for item in status["releases"]}
    assert identities == {DIGEST_1, DIGEST_2}
