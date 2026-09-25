"""Shared helpers for the opt-in kind tests (explicit kubeconfig only).

Every helper passes ``--kubeconfig``/``--context`` (or an explicit client) and
never reads the ambient kube context. Tests using it are skipped unless
``PICELI_KIND_KUBECONFIG`` and ``PICELI_KIND_CONTEXT`` name a disposable cluster.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
# nginx:1.27-alpine and nginx:1.26-alpine multi-arch index digests.
DIGEST_1 = "sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
DIGEST_2 = "sha256:1eadbb07820339e8bbfed18c771691970baee292ec4ab2558f1453d26153e22d"

requires_kind = pytest.mark.skipif(
    not (KUBECONFIG and CONTEXT and shutil.which("kubectl")),
    reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT; needs kubectl",
)


def kubectl(
    *args: str, namespace: str | None = None, stdin: str = "", check: bool = True
) -> str:
    command = ["kubectl", "--kubeconfig", KUBECONFIG, "--context", CONTEXT]
    if namespace:
        command += ["--namespace", namespace]
    result = subprocess.run(
        command + list(args),
        input=stdin,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
        env={
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/tmp"),
            "KUBECONFIG": KUBECONFIG,
        },
    )
    if check and result.returncode:
        raise AssertionError(f"kubectl {args[:2]} failed: {result.stderr[-2000:]}")
    return result.stdout


def get(kind: str, name: str, namespace: str | None = None) -> dict[str, Any]:
    return dict(
        json.loads(
            kubectl(
                "get",
                kind,
                name,
                "-o",
                "json",
                "--show-managed-fields",
                namespace=namespace,
            )
        )
    )


def managers(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``manager[/subresource]`` -> managedFields entry."""
    return {
        entry["manager"]
        + (f"/{entry['subresource']}" if entry.get("subresource") else ""): entry
        for entry in value["metadata"].get("managedFields", [])
    }


def wait_for(
    probe: Callable[[], Any], *, seconds: float = 240, message: str = ""
) -> Any:
    end = time.monotonic() + seconds
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting: {message}")
        time.sleep(1)


def write_spec(
    directory: Path,
    namespace: str,
    module: str,
    *,
    digest: str = DIGEST_1,
    owner: str = "m2-e2e",
    extra: str = "",
    name: str = "release.toml",
    prune: bool = False,
) -> Path:
    """A release spec for ``module:build`` (a file in ``directory``)."""
    path = directory / name
    path.write_text(
        textwrap.dedent(
            f"""
            [target]
            kubeconfig = "{KUBECONFIG}"
            context = "{CONTEXT}"
            namespace = "{namespace}"

            [release]
            name = "web"
            owner = "{owner}"
            field_manager = "{owner}"
            composition = "{module}:build"
            state_dir = "state"
            prune = {"true" if prune else "false"}

            [execution]
            max_seconds = 300
            readiness_seconds = 240
            poll_seconds = 1

            [images]
            web = "docker.io/library/nginx@{digest}"
            """
        )
        + textwrap.dedent(extra)
    )
    return path


def cli(spec: Path, *args: str) -> tuple[int, dict[str, Any]]:
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        payload = {"stdout": result.stdout[-2000:]}
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        payload["exception"] = repr(result.exception)
    return result.exit_code, payload


def cli_process(spec: Path, *args: str) -> subprocess.Popen[str]:
    """``piceli release`` in its own process (for kill tests)."""
    env = {key: value for key, value in os.environ.items() if key != "KUBECONFIG"}
    return subprocess.Popen(
        [sys.executable, "-m", "piceli", "release", *args, "--spec", str(spec)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def operations(payload: dict[str, Any]) -> dict[str, str]:
    return {f"{a['kind']}/{a['name']}": a["operation"] for a in payload["actions"]}


_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def node_platform() -> str:
    """``linux/<arch>`` of the kind node (``PICELI_KIND_NODE``), else of this host.

    Images built for the tests must run on the node: CI runners are amd64,
    a Mac with Docker Desktop is arm64.
    """
    import platform

    machine = platform.machine().lower()
    node = os.environ.get("PICELI_KIND_NODE", "")
    docker = shutil.which("docker")
    if node and docker:
        result = subprocess.run(
            [docker, "exec", node, "uname", "-m"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            machine = result.stdout.strip().lower()
    return f"linux/{_ARCH.get(machine, machine)}"
