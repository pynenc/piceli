"""Opt-in: pods reach the in-cluster registry by its Service name (plain HTTP).

The GitOps controller copies third-party images into ``Registry.in_cluster``
and its build Job pushes built images there, both from inside the cluster at
``piceli-registry.piceli-system.svc:5000``. Runs on the multi-node cluster of
``test_cluster_registry_kind`` (same environment variables)::

    PICELI_KIND_KUBECONFIG=… PICELI_KIND_CONTEXT=… PICELI_KIND_NODE=… \\
      uv run pytest tests/integration/test_registry_from_pods_kind.py

1. ``piceli registry install`` puts the registry on one node.
2. The controller image (``images/Dockerfile``, built from this checkout's
   wheel) runs in a pod on another node and, with the same code the
   controller and the build Job run, mirrors a pinned public image into the
   registry and pushes a built image archive to it.
3. A pod pulls the mirrored image by digest through the node mirror.
4. ``uninstall --delete-storage`` removes everything.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from kind_support import kubectl

from tests.integration.test_cluster_registry_kind import (
    HOST,
    SOURCE_IMAGE,
    _approved,
    _image_id,
    _nodes,
    _status,
    nodes,  # noqa: F401 (fixture)
    pytestmark,  # noqa: F401 (the same opt-in)
)

ROOT = Path(__file__).resolve().parents[2]
# A pinned multi-platform index, copied whole (what Component.image does).
MIRRORED = (
    "docker.io/library/busybox:1.37.0"
    "@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
)

SCRIPT = """
import json, sys, time
from pathlib import Path
from piceli.artifacts.delivery import ArchiveSource, DeliveryGrant
from piceli.artifacts.registry import RegistryTarget
from piceli.artifacts.registry_delivery import RegistryDelivery
from piceli.infra.builders import LocalBuilder, MirrorItem
from piceli.pipeline.backend import RegistryRoute

host, mirrored, image_id = sys.argv[1:4]
# The controller's route in the cluster: the Service name, plain HTTP.
route = RegistryRoute(push=host, node_registry=host, tls=False)
builder = LocalBuilder(route, platform="linux/amd64",
                       cache_dir=Path("/tmp/cache"), work_dir=Path("/tmp/work"))
copied = builder.mirror([MirrorItem("cache", mirrored, "kind/cache")])["cache"]
# The build Job's push of a built archive (cluster_build.run_job_build).
url = f"oci://{host}/kind/built"
pushed = RegistryDelivery().deliver(
    ArchiveSource(Path("/tmp/archive.tar")), RegistryTarget.parse(url),
    DeliveryGrant(image_id, url, time.time() + 3600), node_registry=host)
print(json.dumps({"mirrored": copied.pull_ref, "pushed": pushed.get("state"),
                  "reason": pushed.get("reason"), "pull_ref": pushed.get("pull_ref")}))
"""


def _run(*command: str, stdin: bytes | None = None, timeout: int = 900) -> bytes:
    return subprocess.run(
        command, input=stdin, capture_output=True, check=True, timeout=timeout
    ).stdout


@pytest.fixture(scope="module")
def controller_image() -> Iterator[str]:
    """The controller image of this checkout, loaded into every kind node."""
    tag = f"piceli-controller:kind-{uuid.uuid4().hex[:10]}"
    with tempfile.TemporaryDirectory(prefix="piceli-image-") as context:
        _run("uv", "build", "-q", "--wheel", "--out-dir", f"{context}/dist", str(ROOT))
        _run(
            "docker", "buildx", "build", "-q", "--load", "--target", "controller",
            "-f", str(ROOT / "images" / "Dockerfile"), "-t", tag, context,
        )  # fmt: skip
    try:
        archive = _run("docker", "save", tag)
        for node in _nodes():
            _run(
                "docker", "exec", "-i", node,
                "ctr", "-n", "k8s.io", "images", "import", "-", stdin=archive,
            )  # fmt: skip
        yield f"docker.io/library/{tag}"
    finally:
        subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True)


def test_pods_mirror_and_push_to_the_in_cluster_registry(
    nodes: tuple[str, str],  # noqa: F811
    controller_image: str,
    kind_namespace: str,
    tmp_path: Path,
) -> None:
    node_a, node_b = nodes
    try:
        installed = _approved("install", "--on", node_a, "--storage", "1Gi")
        assert installed["state"] in {"installed", "unchanged"}
        deadline = time.monotonic() + 300
        while _status()["state"] != "ready":
            assert time.monotonic() < deadline, _status()
            time.sleep(3)

        image_id = _image_id()  # pulls SOURCE_IMAGE into the local engine
        archive = tmp_path / "archive.tar"
        archive.write_bytes(_run("docker", "save", SOURCE_IMAGE))
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "controller-like", "namespace": kind_namespace},
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": node_b},
                "restartPolicy": "Never",
                "securityContext": {"runAsUser": 65532, "runAsGroup": 65532},
                "containers": [
                    {
                        "name": "main",
                        "image": controller_image,
                        "imagePullPolicy": "Never",
                        "command": ["sleep", "900"],
                    }
                ],
            },
        }
        kubectl("apply", "-f", "-", stdin=json.dumps(pod))
        kubectl(
            "wait", "--for=condition=Ready", "pod/controller-like",
            "--timeout=180s", namespace=kind_namespace,
        )  # fmt: skip
        kubectl(
            "cp", str(archive), f"{kind_namespace}/controller-like:/tmp/archive.tar"
        )
        output = kubectl(
            "exec", "controller-like", "--", "python", "-c", SCRIPT,
            HOST, MIRRORED, image_id, namespace=kind_namespace,
        )  # fmt: skip
        result = json.loads(output.strip().splitlines()[-1])
        assert result["pushed"] == "succeeded", result
        assert result["pull_ref"].startswith(f"{HOST}/kind/built@sha256:")
        assert result["mirrored"].startswith(f"{HOST}/kind/cache@sha256:")

        # A node pulls the mirrored copy through its mirror, never Docker Hub.
        puller = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "pull-mirrored", "namespace": kind_namespace},
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": node_b},
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "main",
                        "image": result["mirrored"],
                        "imagePullPolicy": "Always",
                        "command": ["sh", "-c", "echo pulled"],
                    }
                ],
            },
        }
        kubectl("apply", "-f", "-", stdin=json.dumps(puller))
        deadline = time.monotonic() + 180
        while True:
            phase = kubectl(
                "get", "pod", "pull-mirrored", "-o", "jsonpath={.status.phase}",
                namespace=kind_namespace,
            )  # fmt: skip
            if phase in {"Succeeded", "Failed"} or time.monotonic() > deadline:
                break
            time.sleep(2)
        assert phase == "Succeeded", kubectl(
            "describe", "pod", "pull-mirrored", namespace=kind_namespace
        )
    finally:
        _approved("uninstall", "--delete-storage")
