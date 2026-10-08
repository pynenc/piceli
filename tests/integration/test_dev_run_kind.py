"""Development runs on kind: upload over exec, the warm lineage, isolation.

A real Job in ``piceli-dev`` (Pod Security ``restricted``), the archive
uploaded over ``pods/exec``, the log followed, the result read; a second run
reuses the warm lineage; the run cannot reach the API server
(NetworkPolicy); a failing command and an artifact come back.

Needs PICELI_KIND_KUBECONFIG, PICELI_KIND_CONTEXT and kubectl; never the
current context. Removes the ``piceli-dev`` namespace it creates.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
KUBECTL = shutil.which("kubectl")
# python:3.12-alpine multi-arch index (python3, sh and busybox head).
IMAGE = (
    "docker.io/library/python"
    "@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4"
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT and KUBECTL),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT, and install kubectl",
    ),
]


def kubectl(*args: str, data: str | None = None, check: bool = True) -> str:
    return subprocess.run(
        [str(KUBECTL), "--kubeconfig", KUBECONFIG, "--context", CONTEXT, *args],
        input=data,
        capture_output=True,
        text=True,
        check=check,
    ).stdout


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(scope="module")
def dev() -> Iterator[tuple[Any, Any]]:
    from piceli.dev.cluster import DevCluster
    from piceli.dev.jobs import install_objects
    from piceli.dev.model import DevBuilds, DevProfile
    from piceli.gitops.install import Api
    from piceli.infra import Cluster, Node
    from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

    node = json.loads(kubectl("get", "nodes", "-o", "json"))["items"][0]
    name = node["metadata"]["labels"]["kubernetes.io/hostname"]
    arch = node["status"]["nodeInfo"]["architecture"]
    cluster = Cluster(
        "kind",
        api="https://127.0.0.1:6443",
        credentials="kind",
        nodes=[Node(name, arch=arch, roles=["builder"])],
        dev=DevBuilds(
            node=name,
            image=IMAGE,
            run_cpu="100m",
            run_memory="256Mi",
            run_storage="1Gi",
            cache_size="2Gi",
            timeout="5m",
            profiles=[
                DevProfile(
                    "sh", tools=["sh", "python3"], toolchain=["python3", "--version"]
                )
            ],
        ),
    )
    objects = install_objects(cluster)
    kubectl(
        "apply",
        "-f",
        "-",
        data=json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}),
    )
    api = Api(api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT))
    port = DevCluster(api, poll_seconds=0.5)
    try:
        assert port.config() is not None
        yield port, port.config()
    finally:
        api.close()
        kubectl(
            "delete",
            "namespace",
            "piceli-dev",
            "--wait=true",
            "--timeout=180s",
            check=False,
        )


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "shop"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "dev@example.com")
    git(repo, "config", "user.name", "Dev")
    for name, text in files.items():
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "files")
    return repo


def test_runs_build_in_a_warm_lineage_and_report_back(dev: Any, tmp_path: Path) -> None:
    from piceli.dev.client import RunRequest, run
    from piceli.dev.pack import SourceRequest

    port, settings = dev
    repo = _repo(
        tmp_path,
        {
            "build.sh": 'echo "built $(cat a.txt)" > "$CARGO_TARGET_DIR/out.txt"; cat "$CARGO_TARGET_DIR/out.txt"',
            "a.txt": "one",
            "fail.sh": "echo going down; exit 7",
        },
    )
    lines: list[str] = []
    first = run(
        port,
        settings,
        RunRequest(
            SourceRequest("shop", repo, "HEAD"),
            ["sh", "build.sh"],
            artifacts=["target/out.txt"],
            artifacts_dir=tmp_path / "artifacts",
            queue_timeout=300,
        ),
        log=lines.append,
    )
    assert first["state"] == "passed", first
    assert "built one" in lines
    assert first["cache"]["warm"] is False
    (artifact,) = first["artifacts"]
    assert Path(artifact).read_text() == "built one\n"
    second = run(
        port,
        settings,
        RunRequest(
            SourceRequest("shop", repo, "HEAD"), ["sh", "build.sh"], queue_timeout=300
        ),
    )
    assert second["state"] == "passed"
    assert second["cache"]["lineage"] == first["cache"]["lineage"]
    assert second["cache"]["warm"] is True
    assert second["sync"]["written"] == 0
    failed = run(
        port,
        settings,
        RunRequest(
            SourceRequest("shop", repo, "HEAD"), ["sh", "fail.sh"], queue_timeout=300
        ),
    )
    assert failed["state"] == "failed" and failed["exit_code"] == 7
    assert "going down" in failed["log_tail"]
    # Every run's Job is gone.
    assert port.jobs() == []


def test_a_run_reaches_neither_the_api_server_nor_a_secret(
    dev: Any, tmp_path: Path
) -> None:
    from piceli.dev.client import RunRequest, run
    from piceli.dev.pack import SourceRequest

    port, settings = dev
    probe = (
        "import os, socket\n"
        "socket.getaddrinfo('kubernetes.default.svc.cluster.local', 443)\n"
        "print('dns ok')\n"
        "print('token', os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/token'))\n"
        "try:\n"
        "    socket.create_connection(('kubernetes.default.svc.cluster.local', 443), timeout=5)\n"
        "    print('api reachable')\n"
        "except OSError:\n"
        "    print('api blocked')\n"
    )
    repo = _repo(tmp_path, {"probe.py": probe})
    lines: list[str] = []
    result = run(
        port,
        settings,
        RunRequest(
            SourceRequest("shop", repo, "HEAD"),
            ["python3", "probe.py"],
            queue_timeout=300,
        ),
        log=lines.append,
    )
    assert result["state"] == "passed", result
    assert "dns ok" in lines and "token False" in lines
    assert "api blocked" in lines
