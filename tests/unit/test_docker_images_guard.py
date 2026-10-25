"""0.31.0: the images tests build are found and removed (``tests/docker_images.py``)."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from tests import docker_images

pytestmark = pytest.mark.skipif(
    not docker_images.enabled(),
    reason="set PICELI_DOCKER_TESTS=1 (with Docker) to build real images",
)


def test_a_built_test_image_is_listed_and_removed(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM scratch\nCOPY f /f\n")
    (tmp_path / "f").write_text(uuid.uuid4().hex)
    tag = f"piceli-e2e/guard:{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "build", "-q", "-t", tag, str(tmp_path)],
        check=True, capture_output=True, timeout=300,
    )  # fmt: skip
    assert tag in docker_images.images()
    assert docker_images.remove({tag}) == set()
    assert tag not in docker_images.images()
