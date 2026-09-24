"""Opt-in two-image build → deliver → release acceptance on a disposable kind cluster.

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-p2 --kubeconfig /tmp/p2.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/p2.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-p2 \\
    PICELI_KIND_NODE=piceli-p2-control-plane \\
      uv run pytest tests/integration/test_two_images_kind.py

It needs ``docker`` (classic or containerd image store) and ``kubectl`` on
``PATH``. The test never reads the ambient kubeconfig. In a uniquely named
namespace it releases the node-local registry (``examples/two-images``),
builds two tiny images, delivers both with ``piceli artifacts deliver --to
oci://… --via-forward deployment/registry`` and releases them from their
delivery receipts. It then rebuilds and re-delivers one image and checks that
the next plan changes exactly one Deployment.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
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
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "two-images"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT and NODE and shutil.which("docker")),
        reason="set PICELI_KIND_KUBECONFIG, PICELI_KIND_CONTEXT and PICELI_KIND_NODE",
    ),
]


@pytest.fixture
def namespace():
    from kubernetes.client import CoreV1Api

    name = "two-images-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    api = CoreV1Api(client)
    api.create_namespace({"metadata": {"name": name}})
    try:
        yield name
    finally:
        api.delete_namespace(name)
        client.close()


def _target(namespace: str) -> str:
    return textwrap.dedent(
        f"""
        [target]
        kubeconfig = "{KUBECONFIG}"
        context = "{CONTEXT}"
        namespace = "{namespace}"
        [target.nodes.primary]
        name = "{NODE}"
        """
    )


def _specs(directory: Path, namespace: str) -> tuple[Path, Path]:
    for name in ("composition.py", "registry.py", "Dockerfile"):
        shutil.copy(EXAMPLE / name, directory / name)
    registry = directory / "registry.toml"
    registry.write_text(
        _target(namespace)
        + textwrap.dedent(
            """
            [release]
            name = "registry"
            owner = "two-images-registry-e2e"
            field_manager = "two-images-registry-e2e"
            composition = "registry.py:build"
            state_dir = "registry-state"
            [execution]
            readiness_seconds = 240
            [images]
            registry = "docker.io/library/registry:3.1.1@sha256:325b4b29b041e82803abeb703e201655e4e23ab83264ec1a7c9ddb0a5b14a6e0"
            [values]
            registry_storage = "1Gi"
            """
        )
    )
    release = directory / "release.toml"
    release.write_text(
        _target(namespace)
        + textwrap.dedent(
            """
            [release]
            name = "apps"
            owner = "two-images-e2e"
            field_manager = "two-images-e2e"
            composition = "composition.py:build"
            state_dir = "apps-state"
            [execution]
            readiness_seconds = 240
            [images.alpha]
            receipt = "alpha.delivery.json"
            [images.beta]
            receipt = "beta.delivery.json"
            """
        )
    )
    return registry, release


def _run(spec: Path, *args: str) -> tuple[int, dict]:
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}")


def _apply(spec: Path) -> dict:
    code, applied = _run(spec, "apply", "--auto-approve")
    if code != 0:
        # A WaitForFirstConsumer claim binds after the first apply; the next
        # plan sees it bound and completes (docs/node_local_registry.md).
        code, applied = _run(spec, "apply", "--auto-approve")
    assert code == 0, applied
    return applied


def _build(directory: Path, name: str, message: str) -> str:
    tag = f"piceli-e2e/{name}:{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "build", "-q", "--build-arg", f"MESSAGE={message}"]
        + ["-t", tag, str(directory)],
        check=True,
        capture_output=True,
    )
    image_id = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return image_id


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _deliver(directory: Path, namespace: str, name: str, image_id: str) -> dict:
    receipt = directory / f"{name}.delivery.json"
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
            "--to",
            f"oci://127.0.0.1:{_free_port()}/two-images/{name}",
            "--node-registry",
            "127.0.0.1:5000",
            "--via-forward",
            "deployment/registry",
            "--namespace",
            namespace,
            "--kubeconfig",
            KUBECONFIG,
            "--context",
            CONTEXT,
            "--receipt",
            str(receipt),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    document = json.loads(receipt.read_text())
    assert document["result"] in {"pushed", "already-present"}, document
    return document


def _deployments(namespace: str) -> dict[str, tuple[str, int]]:
    """Deployment name -> (container image, metadata.generation)."""
    from kubernetes.client import AppsV1Api

    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    try:
        return {
            item.metadata.name: (
                item.spec.template.spec.containers[0].image,
                item.metadata.generation,
            )
            for item in AppsV1Api(client).list_namespaced_deployment(namespace).items
        }
    finally:
        client.close()


def _desired(plan: dict) -> dict[str, str]:
    """Kind/name -> digest of the desired manifest in a plan."""
    return {
        f"{action['kind']}/{action['name']}": action["artifact_digest"]
        for action in plan["actions"]
    }


def test_two_images_change_one_deployment(tmp_path, namespace):
    registry_spec, release_spec = _specs(tmp_path, namespace)
    _apply(registry_spec)

    tag = uuid.uuid4().hex[:8]
    alpha = _deliver(
        tmp_path, namespace, "alpha", _build(tmp_path, "a", f"alpha-{tag}")
    )
    beta = _deliver(tmp_path, namespace, "beta", _build(tmp_path, "b", f"beta-{tag}"))
    first = _apply(release_spec)
    before = _deployments(namespace)
    assert before["alpha"][0] == alpha["pull_ref"]
    assert before["beta"][0] == beta["pull_ref"]
    assert "@sha256:" in alpha["pull_ref"] and "@sha256:" in beta["pull_ref"]

    # Unchanged receipts: the same release, the same desired manifests.
    code, same = _run(release_spec, "plan")
    assert code == 0, same
    assert same["release"] == first["release"]

    # Change one image and re-deliver it.
    changed = _deliver(
        tmp_path, namespace, "alpha", _build(tmp_path, "a", f"alpha-{tag}-v2")
    )
    assert changed["pull_ref"] != alpha["pull_ref"]
    code, plan = _run(release_spec, "plan")
    assert code == 0, plan
    assert plan["release"] != first["release"]
    # Exactly one desired manifest differs: Deployment/alpha.
    old, new = _desired(same), _desired(plan)
    assert sorted(old) == sorted(new) == ["Deployment/alpha", "Deployment/beta"]
    assert [ref for ref in sorted(new) if new[ref] != old[ref]] == ["Deployment/alpha"]

    code, second = _run(release_spec, "apply", "--approve", plan["plan_hash"])
    assert code == 0, second
    after = _deployments(namespace)
    # alpha rolled to the new digest; beta's spec (generation) did not change.
    assert after["alpha"] == (changed["pull_ref"], before["alpha"][1] + 1)
    assert after["beta"] == before["beta"]
