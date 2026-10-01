"""Run UI acceptance against a disposable kind cluster with explicit credentials.

The pinned kind binary and its upstream checksum are downloaded into a temporary
directory. The cluster, kubeconfig, logs and any newly pulled node reference are
removed on both success and failure. No ambient Kubernetes context is consulted.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    pins = json.loads((root / ".github/kind-nodes.json").read_text())
    machine = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64"}.get(
        platform.machine()
    )
    system = platform.system().lower()
    if system not in {"darwin", "linux"} or machine is None:
        raise SystemExit("Unsupported kind test host")
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Docker is required for disposable kind tests")
    docker_args = [docker, "--host", "unix:///var/run/docker.sock"]
    node = pins["nodes"][0]["image"]
    name = "piceli-ui-" + uuid.uuid4().hex[:10]
    environment = dict(os.environ)
    environment["DOCKER_HOST"] = "unix:///var/run/docker.sock"
    environment.pop("DOCKER_CONTEXT", None)
    environment["KIND_EXPERIMENTAL_PROVIDER"] = "docker"

    def exists(args: list[str]) -> bool:
        return (
            subprocess.run(
                docker_args + args, capture_output=True, check=False
            ).returncode
            == 0
        )

    node_existed = exists(["image", "inspect", node])
    network_existed = exists(["network", "inspect", "kind"])
    with tempfile.TemporaryDirectory(prefix="piceli-ui-kind-") as temporary:
        directory = Path(temporary)
        scratch_home = directory / "home"
        scratch_home.mkdir(mode=0o700)
        environment["HOME"] = str(scratch_home)
        kind = directory / "kind"
        url = (
            "https://github.com/kubernetes-sigs/kind/releases/download/"
            f"{pins['kind']}/kind-{system}-{machine}"
        )
        with urllib.request.urlopen(url, timeout=60) as response:
            binary = response.read(50 * 1024 * 1024)
        with urllib.request.urlopen(url + ".sha256sum", timeout=30) as response:
            checksum = response.read(4096).decode().split()[0]
        if hashlib.sha256(binary).hexdigest() != checksum:
            raise SystemExit("kind checksum mismatch")
        kind.write_bytes(binary)
        kind.chmod(0o700)
        kubeconfig = directory / "kubeconfig"
        environment.update(
            KUBECONFIG=str(kubeconfig),
            PICELI_KIND_KUBECONFIG=str(kubeconfig),
            PICELI_KIND_CONTEXT="kind-" + name,
            PICELI_KIND_NODE=name + "-control-plane",
        )
        try:
            subprocess.run(
                [
                    str(kind),
                    "create",
                    "cluster",
                    "--name",
                    name,
                    "--image",
                    node,
                    "--kubeconfig",
                    str(kubeconfig),
                    "--wait",
                    "180s",
                ],
                env=environment,
                check=True,
                timeout=600,
            )
            command = sys.argv[1:] or [
                "uv",
                "run",
                "--frozen",
                "pytest",
                "tests/integration",
                "-q",
            ]
            return subprocess.run(
                command, cwd=root, env=environment, check=False
            ).returncode
        finally:
            try:
                deletion = subprocess.run(
                    [str(kind), "delete", "cluster", "--name", name],
                    env=environment,
                    check=False,
                    timeout=120,
                )
                if deletion.returncode:
                    print(f"Cluster cleanup failed: {name}", file=sys.stderr)
            except subprocess.TimeoutExpired:
                print(f"Cluster cleanup timed out: {name}", file=sys.stderr)
            finally:
                # Non-forced removal refuses references still used by a node.
                # A failed cluster cleanup must not skip other owned artifacts.
                try:
                    if not node_existed:
                        subprocess.run(
                            docker_args + ["image", "rm", node],
                            check=False,
                            capture_output=True,
                            timeout=60,
                        )
                finally:
                    if not network_existed:
                        subprocess.run(
                            docker_args + ["network", "rm", "kind"],
                            check=False,
                            capture_output=True,
                            timeout=60,
                        )


if __name__ == "__main__":
    raise SystemExit(main())
