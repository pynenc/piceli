"""Opt-in: development runs on a real docker build host (0.20.0).

Runs the published builder image on this machine's container engine
(Docker Desktop, OrbStack, colima or a Linux engine): a run passes and
streams, a second run is warm, two runs share one slot through the host's
queue. Every container and the test's cache volume are removed at the end::

    PICELI_DOCKER_TESTS=1 uv run pytest tests/integration/test_dev_docker_host.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli.dev.client import RunRequest, run
from piceli.dev.hosts import DockerPort
from piceli.dev.model import BuildHost, DevBuilds, DevProfile
from piceli.dev.pack import SourceRequest
from tests.unit.dev.test_pack import repo

BUILDER = (
    "ghcr.io/pynenc/piceli-builder"
    "@sha256:d27550082d50475303e8be4d439a259e9b622ec611c6c639573e2d9c080e16bf"
)

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("PICELI_DOCKER_TESTS") != "1",
        reason="set PICELI_DOCKER_TESTS=1 (needs a container engine)",
    ),
    pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker"),
    pytest.mark.timeout(900),
]


@pytest.fixture
def host() -> Iterator[tuple[BuildHost, DevBuilds, str]]:
    name = f"t{uuid.uuid4().hex[:8]}"
    volume = f"piceli-dev-test-{name}"
    build_host = BuildHost.docker(name, slots=1, memory="2Gi")
    dev = DevBuilds(
        node=None, image=BUILDER, hosts=[build_host],
        profiles=[DevProfile("sh", tools=["sh"], toolchain=None)],
    )  # fmt: skip
    try:
        yield build_host, dev, volume
    finally:
        listed = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label=piceli.io/dev-host={name}"],
            capture_output=True, text=True, check=False,
        ).stdout.split()  # fmt: skip
        if listed:
            subprocess.run(
                ["docker", "rm", "-f", *listed], capture_output=True, check=False
            )
        subprocess.run(
            ["docker", "volume", "rm", "-f", volume], capture_output=True, check=False
        )


def test_a_run_passes_streams_and_the_second_is_warm(host: Any, tmp_path: Path) -> None:
    build_host, dev, volume = host
    app = repo(tmp_path / "shop", {"t.sh": "uname -m; id -u; echo from-the-container"})
    lines: list[str] = []
    result = run(
        DockerPort(build_host, dev, cache_volume=volume, poll=0.5), dev,
        RunRequest(SourceRequest("shop", app, None), ["sh", "t.sh"]),
        log=lines.append, host=build_host,
    )  # fmt: skip
    assert result["state"] == "passed", result
    assert "from-the-container" in lines and "65532" in lines
    assert result["upload"]["mode"] in {"delta", "tree"}
    again = run(
        DockerPort(build_host, dev, cache_volume=volume, poll=0.5), dev,
        RunRequest(SourceRequest("shop", app, None), ["true"]), host=build_host,
    )  # fmt: skip
    assert again["state"] == "passed" and again["cache"]["warm"] is True
    leftover = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=piceli.io/dev-host={build_host.name}"],
        capture_output=True, text=True, check=False,
    ).stdout.split()  # fmt: skip
    assert leftover == []  # each run's container went with it


def test_two_runs_share_the_one_slot(host: Any, tmp_path: Path) -> None:
    build_host, dev, volume = host
    app = repo(tmp_path / "shop", {"t.sh": "sleep 3; echo done"})
    said: list[str] = []
    results: list[dict[str, Any]] = []

    def one(requester: str) -> None:
        results.append(
            run(
                DockerPort(build_host, dev, cache_volume=volume, poll=0.5),
                dev,
                RunRequest(
                    SourceRequest("shop", app, "HEAD"),
                    ["sh", "t.sh"],
                    requester=requester,
                ),
                say=said.append,
                host=build_host,
            )
        )

    threads = [threading.Thread(target=one, args=(f"agent-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=600)
    assert [r["state"] for r in results] == ["passed", "passed"], results
    assert any("(1/1 busy)" in line for line in said), said


def test_the_cache_of_a_docker_host_is_cleared(host: Any, tmp_path: Path) -> None:
    from piceli.dev.hosts import clear_host_cache

    build_host, dev, volume = host
    app = repo(tmp_path / "shop", {"t.sh": "true"})
    first = run(
        DockerPort(build_host, dev, cache_volume=volume, poll=0.5), dev,
        RunRequest(SourceRequest("shop", app, None), ["sh", "t.sh"]), host=build_host,
    )  # fmt: skip
    assert first["state"] == "passed", first
    cleared = clear_host_cache(build_host, dev, cache_volume=volume)
    assert [item["lineage"] for item in cleared["cleared"]] == [
        first["cache"]["lineage"]
    ]
    again = run(
        DockerPort(build_host, dev, cache_volume=volume, poll=0.5), dev,
        RunRequest(SourceRequest("shop", app, None), ["true"]), host=build_host,
    )  # fmt: skip
    assert again["cache"]["warm"] is False
