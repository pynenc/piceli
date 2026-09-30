"""Publishing to a real ``registry:2`` and signing with a real cosign (opt-in).

Skipped unless ``PICELI_REGISTRY_IT=1``, ``docker`` can run containers and
``cosign`` (3.x) is on ``PATH``; for example::

    PICELI_REGISTRY_IT=1 nix shell nixpkgs#cosign -c \\
        uv run pytest tests/integration/test_publish_registry.py

Each test starts its own registry containers on free loopback ports
(one anonymous, one with basic auth and generated credentials) and removes
them at the end. Nothing is pushed anywhere else.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from piceli.artifacts.build_spec import BuildReceipt
from piceli.artifacts.host_build import HostBuildGrant, HostBuildSpec
from piceli.artifacts.multi_platform import MultiPlatformHostBuild
from piceli.artifacts.process import ToolPin
from piceli.artifacts.publish import Publisher, PublishGrant, PublishPlan, referrers
from piceli.artifacts.registry import (
    RegistryEndpoint,
    StreamedOciRegistryClient,
    docker_config_credentials,
)
from piceli.artifacts.signing import CosignSigner, verify_argv
from tests.unit.host_build_support import publish_base, write_project
from tests.unit.test_host_build import Recorder
from tests.unit.test_registry_delivery import FakeRegistry

DOCKER = shutil.which("docker")
COSIGN = shutil.which("cosign")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PICELI_REGISTRY_IT") != "1" or not DOCKER or not COSIGN,
        reason="set PICELI_REGISTRY_IT=1; needs docker and cosign on PATH",
    ),
    pytest.mark.timeout(600),
]
IMAGE = "registry:2"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _docker(*argv: str) -> str:
    assert DOCKER is not None
    return subprocess.run(
        [DOCKER, *argv], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def start_registry(tmp_path: Path) -> Iterator[object]:
    started: list[str] = []

    def start(htpasswd: str | None = None) -> int:
        port = _free_port()
        name = f"piceli-it-registry-{secrets.token_hex(4)}"
        argv = ["run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:5000"]
        if htpasswd is not None:
            auth = tmp_path / f"auth-{len(started)}.htpasswd"
            auth.write_text(htpasswd)
            argv += [
                "-v",
                f"{auth}:/auth/htpasswd:ro",
                "-e",
                "REGISTRY_AUTH=htpasswd",
                "-e",
                "REGISTRY_AUTH_HTPASSWD_REALM=piceli",
                "-e",
                "REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd",
            ]
        _docker(*argv, IMAGE)
        started.append(name)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/v2/", timeout=1)
                break
            except urllib.error.HTTPError:
                break  # 401: up, with auth
            except OSError:
                time.sleep(0.2)
        return port

    try:
        yield start
    finally:
        for name in started:
            subprocess.run(
                [str(DOCKER), "rm", "-f", name], capture_output=True, check=False
            )


@pytest.fixture
def built(tmp_path: Path) -> Iterator[tuple[BuildReceipt, Path]]:
    base_registry = FakeRegistry()
    try:
        base = publish_base(base_registry, architectures=("amd64", "arm64"))
        path = write_project(tmp_path / "project", base_registry.port, base)
        spec = HostBuildSpec.from_toml(path).with_cache_dir(tmp_path / "cache")
        build = MultiPlatformHostBuild(spec, ("linux/amd64", "linux/arm64"))
        grant = HostBuildGrant(build.plan().plan_hash, time.time() + 600)
        yield build.run(grant, tmp_path / "out", runner=Recorder()), tmp_path / "out"
    finally:
        base_registry.close()


def _publish(plan: PublishPlan, publisher: Publisher) -> dict[str, object]:
    return publisher.publish(plan, PublishGrant(plan.digest, time.time() + 600))


def test_publish_sign_and_verify_on_a_real_registry(
    start_registry, built, tmp_path: Path
) -> None:
    port = start_registry()
    receipt, out = built
    keys = tmp_path / "keys"
    keys.mkdir()
    env = {**os.environ, "COSIGN_PASSWORD": secrets.token_hex(8)}
    assert COSIGN is not None
    subprocess.run(
        [COSIGN, "generate-key-pair"],
        cwd=keys,
        env=env,
        check=True,
        capture_output=True,
    )
    (keys / "cosign.key").chmod(0o600)
    os.environ["COSIGN_PASSWORD"] = env["COSIGN_PASSWORD"]
    try:
        signer = CosignSigner(ToolPin.capture(Path(COSIGN)), keys / "cosign.key")
        plan = PublishPlan.from_receipt(
            receipt, out, f"oci://127.0.0.1:{port}/it", "1.0.0", signing=signer.public()
        )
        result = _publish(plan, Publisher(signer=signer))
    finally:
        os.environ.pop("COSIGN_PASSWORD", None)
    assert result["state"] == "published", result
    web = result["images"]["web"]
    client = StreamedOciRegistryClient(RegistryEndpoint("127.0.0.1", port))
    assert client.manifest_digest("it/web", "1.0.0") == web["digest"]
    index = json.loads(client.get_manifest("it/web", web["digest"])[0])
    assert sorted(item["platform"]["architecture"] for item in index["manifests"]) == [
        "amd64",
        "arm64",
    ]
    for digest in [web["digest"], *(p["digest"] for p in web["platforms"].values())]:
        verified = subprocess.run(
            verify_argv(
                Path(COSIGN),
                keys / "cosign.pub",
                f"127.0.0.1:{port}/it/web@{digest}",
                http=True,
            ),
            capture_output=True,
            check=False,
        )
        assert verified.returncode == 0, verified.stderr[-400:]
    # cosign's signatures did not replace the SBOM and provenance referrers.
    for item in web["attestations"]:
        found = {
            entry["digest"] for entry in referrers(client, "it/web", item["subject"])
        }
        assert item["digest"] in found


def test_a_private_registry_with_basic_auth(start_registry, built, tmp_path) -> None:
    user, password = "piceli", secrets.token_urlsafe(16)
    htpasswd = subprocess.run(
        ["htpasswd", "-Bbn", user, password], capture_output=True, text=True, check=True
    ).stdout
    port = start_registry(htpasswd)
    receipt, out = built
    config = tmp_path / "docker-config.json"
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    config.write_text(json.dumps({"auths": {f"127.0.0.1:{port}": {"auth": auth}}}))
    config.chmod(0o600)
    plan = PublishPlan.from_receipt(receipt, out, f"oci://127.0.0.1:{port}/it", "1.0.0")
    anonymous = _publish(plan, Publisher())
    assert anonymous["reason"] == "registry-unauthorized"
    credentials = docker_config_credentials(config, f"127.0.0.1:{port}")
    first = _publish(plan, Publisher(credentials=credentials))
    assert first["state"] == "published", first
    second = _publish(plan, Publisher(credentials=credentials))
    assert second["images"]["web"]["result"] == "already-present"
    assert second["images"]["web"]["blobs"]["uploaded"] == 0
    assert password not in json.dumps(first) + json.dumps(second)
