"""Docker images the tests create, removed on every outcome (0.31.0).

Tests that build real images (``PICELI_DOCKER_TESTS=1``, the kind suites)
tag them under :data:`REPOSITORIES` only. The autouse fixture in
``tests/conftest.py`` lists those repositories before and after each such
test, removes every image the test added (pass, fail or crash of the test
body) and fails the test when one cannot be removed. Images that were there
before the test are never touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess

#: Repositories only tests write to.
REPOSITORIES = ("piceli-e2e/*", "piceli-examples/*")


def enabled() -> bool:
    """Whether this run builds real images (and Docker is installed)."""
    wanted = os.environ.get("PICELI_DOCKER_TESTS") == "1" or bool(
        os.environ.get("PICELI_KIND_KUBECONFIG")
    )
    return wanted and _DOCKER is not None


_DOCKER = shutil.which("docker")  # found once: a test may change PATH


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    if _DOCKER is None:
        return subprocess.CompletedProcess(args, 127, "", "docker not found")
    try:
        return subprocess.run(
            [_DOCKER, *args], capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.SubprocessError) as error:
        return subprocess.CompletedProcess(args, 127, "", type(error).__name__)


def images() -> set[str]:
    """``REPOSITORY:TAG`` (or ``REPOSITORY@ID`` when untagged) of the test
    repositories' images; empty when Docker does not answer."""
    found: set[str] = set()
    for pattern in REPOSITORIES:
        result = _docker(
            "images", "--no-trunc", "--filter", f"reference={pattern}",
            "--format", "{{.Repository}}:{{.Tag}} {{.ID}}",
        )  # fmt: skip
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            ref, _, image_id = line.partition(" ")
            found.add(image_id if ref.endswith(":<none>") else ref)
    return found


def remove(refs: set[str]) -> set[str]:
    """Remove ``refs``; the ones still there afterwards."""
    if refs:
        _docker("rmi", "--force", *sorted(refs))
    return refs & images()
