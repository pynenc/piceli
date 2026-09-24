"""Opt-in end-to-end delivery into a disposable kind node's containerd.

Runs only when ``PICELI_KIND_NODE`` names a kind node container, for example::

    kind create cluster --name piceli-deliver --kubeconfig /tmp/deliver.kubeconfig
    PICELI_KIND_NODE=piceli-deliver-control-plane \
      uv run pytest tests/integration/test_node_delivery_kind.py

Optional: ``PICELI_DOCKER`` (docker binary), ``PICELI_DOCKER_SOCKET``. The test
never touches a kubeconfig; it talks to the node container through Docker.
"""

from __future__ import annotations

import io
import os
import shutil
import time
import uuid
from pathlib import Path

import pytest

from piceli.artifacts.delivery import (
    ArchiveSource,
    DeliveryGrant,
    DeliveryRejected,
    NodeDelivery,
    relay_image_stream,
)
from piceli.artifacts.node_transport import NodeTarget, SubprocessRunner, Transport
from piceli.artifacts.process import ProcessLimits, ToolPin
from tests.unit.test_node_delivery import docker_archive, oci_archive

NODE = os.environ.get("PICELI_KIND_NODE", "")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not NODE, reason="set PICELI_KIND_NODE to a kind node"),
]


@pytest.fixture
def delivery() -> NodeDelivery:
    docker = os.environ.get("PICELI_DOCKER") or shutil.which("docker") or ""
    socket = os.environ.get("PICELI_DOCKER_SOCKET", "/var/run/docker.sock")
    return NodeDelivery(
        docker=ToolPin.capture(Path(os.path.realpath(docker))),
        docker_socket=Path(socket),
    )


def _target() -> tuple[str, NodeTarget]:
    url = f"docker://{NODE}?runtime=containerd"
    return url, NodeTarget.parse(url)


def _remove(delivery: NodeDelivery, target: NodeTarget, name: str) -> None:
    argv = Transport(target, delivery.docker, delivery.docker_socket).argv(
        target.runtime_argv("images", "rm", name)
    )
    SubprocessRunner().capture(argv, ProcessLimits(30), {})


@pytest.mark.parametrize("builder", [docker_archive, oci_archive])
def test_archive_delivery_is_verified_idempotent_and_digest_gated(
    delivery, tmp_path, builder
):
    url, target = _target()
    tag = uuid.uuid4().hex[:12]
    name = f"registry.test/piceli-delivery:{tag}"
    archive, digest = builder(tag.encode())
    path = tmp_path / "image.tar"
    path.write_bytes(archive)
    grant = DeliveryGrant(digest, url, time.time() + 120)
    try:
        first = delivery.deliver(ArchiveSource(path), target, grant, reference=name)
        assert first["result"] == "imported", first
        assert first["image"]["config_digest"] == digest
        node = delivery.inspect_node(target, name)
        assert node is not None and node.config_digests == (digest,)

        second = delivery.deliver(ArchiveSource(path), target, grant, reference=name)
        assert second["result"] == "already-present"
        assert second["transfer"]["streamed"] is False

        wrong = "sha256:" + "0" * 64
        other = f"registry.test/piceli-delivery:{tag}-wrong"
        rejected = delivery.deliver(
            ArchiveSource(path),
            target,
            DeliveryGrant(wrong, url, time.time() + 120),
            reference=other,
        )
        assert (rejected["result"], rejected["reason"]) == (
            "rejected",
            "digest-mismatch",
        )
        assert delivery.inspect_node(target, other) is None
    finally:
        _remove(delivery, target, name)


def test_real_runtime_rejects_a_stream_whose_index_was_withheld(delivery):
    """The in-stream guard: layers reach ctr, the index never does."""
    _, target = _target()
    tag = uuid.uuid4().hex[:12]
    name = f"registry.test/piceli-delivery:{tag}-stream"
    archive, _ = docker_archive(tag.encode())
    argv = Transport(target, delivery.docker, delivery.docker_socket).argv(
        target.runtime_argv("images", "import", "-"), stdin=True
    )
    with pytest.raises(DeliveryRejected):
        SubprocessRunner().feed(
            argv,
            lambda sink: relay_image_stream(
                io.BytesIO(archive),
                sink,
                approved_digest="sha256:" + "0" * 64,
                reference=name,
            ),
            ProcessLimits(60),
            {},
        )
    assert delivery.inspect_node(target, name) is None
