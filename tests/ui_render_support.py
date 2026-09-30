"""An opt-in renderer image owned and removed by one acceptance context."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from piceli.artifacts.process import ToolPin
from piceli.services.evaluation import RendererConfig


@contextmanager
def renderer_image(
    *, socket: Path = Path("/var/run/docker.sock")
) -> Iterator[RendererConfig]:
    """Build trusted Piceli only; no consumer source enters the build context.

    Uses the existing python:3.12-slim base by its inspected immutable ID. The
    legacy builder removes intermediate containers; deleting our final image
    removes its unshared layers without pruning another user's Docker state.
    """
    docker = ToolPin.capture(Path(shutil.which("docker") or "/missing/docker"))
    root = Path(__file__).resolve().parents[1]
    tag = "piceli-ui-render-test:" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="piceli-render-image-") as temporary:
        directory = Path(temporary)
        config = directory / "docker-config"
        config.mkdir()
        command = [
            str(docker.path),
            "--host",
            f"unix://{socket}",
            "--config",
            str(config),
        ]
        environment = {
            "PATH": os.defpath,
            "HOME": temporary,
            "DOCKER_CONFIG": str(config),
            "DOCKER_BUILDKIT": "0",
        }

        def run(args: list[str], timeout: int = 180) -> bytes:
            docker.verify()
            completed = subprocess.run(
                command + args,
                env=environment,
                cwd=directory,
                capture_output=True,
                timeout=timeout,
                check=True,
            )
            docker.verify()
            return completed.stdout

        base = json.loads(run(["image", "inspect", "python:3.12-slim"]))[0]
        context = directory / "context"
        context.mkdir()
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("uv required for frozen renderer fixture")
        requirements = subprocess.run(
            [
                uv,
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
            ],
            cwd=root,
            capture_output=True,
            check=True,
        ).stdout
        (context / "requirements.txt").write_bytes(requirements)
        subprocess.run(
            [
                uv,
                "build",
                "--offline",
                "--wheel",
                "--no-build-isolation",
                "--out-dir",
                str(context),
            ],
            cwd=root,
            capture_output=True,
            check=True,
            timeout=60,
        )
        wheel = next(context.glob("*.whl"))
        label = tag.split(":")[1]
        (context / "Dockerfile").write_text(
            f"FROM {base['Id']}\nLABEL piceli.renderer-test={label}\nCOPY . /package\nRUN pip install --no-cache-dir --require-hashes -r /package/requirements.txt && pip install --no-cache-dir --no-deps /package/{wheel.name} && rm -rf /package\n"
        )
        try:
            run(
                [
                    "build",
                    "--rm",
                    "--force-rm",
                    "--pull=false",
                    "--tag",
                    tag,
                    str(context),
                ],
                timeout=240,
            )
            image = json.loads(run(["image", "inspect", tag]))[0]
            yield RendererConfig(
                image["Id"], image["Os"] + "/" + image["Architecture"], docker, socket
            )
        finally:
            try:
                containers = (
                    run(["ps", "--all", "--quiet", "--filter", f"ancestor={tag}"])
                    .decode()
                    .split()
                )
                if containers:
                    run(["rm", "--force", "--volumes", *containers])
            finally:
                # The unique build label also identifies untagged intermediate
                # legacy images, including those left by a failed install.
                images = (
                    run(
                        [
                            "image",
                            "ls",
                            "--all",
                            "--quiet",
                            "--filter",
                            f"label=piceli.renderer-test={label}",
                        ]
                    )
                    .decode()
                    .split()
                )
                if images:
                    run(["image", "rm", "--force", *dict.fromkeys(images)])
