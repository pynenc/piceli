"""Registry retention against a real ``registry:2`` with deletes enabled (opt-in).

Skipped unless ``PICELI_REGISTRY_IT=1`` and ``docker`` can run containers::

    PICELI_REGISTRY_IT=1 uv run pytest tests/integration/test_retention_registry.py

The test starts one registry container on a free loopback port, pushes several
releases of three images that share layers (what a node-local registry holds),
runs ``piceli artifacts retention`` with a budget, deletes the approved plan,
runs the registry's own garbage collector and checks the disk the registry
uses, then removes the container. Nothing else is touched.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from piceli.artifacts.cli import main
from piceli.artifacts.registry import RegistryEndpoint, StreamedOciRegistryClient

DOCKER = shutil.which("docker")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PICELI_REGISTRY_IT") != "1" or not DOCKER,
        reason="set PICELI_REGISTRY_IT=1; needs docker on PATH",
    ),
    pytest.mark.timeout(600),
]
IMAGE = os.environ.get("PICELI_RETENTION_REGISTRY_IMAGE", "registry:2")
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
KB = 1024
RELEASES = 8
IMAGES = {"api": 1024 * KB, "worker": 768 * KB, "web": 512 * KB}  # shared base layer
OWN_LAYER = 256 * KB  # the layer each release changes


def _docker(*argv: str) -> str:
    assert DOCKER is not None
    return subprocess.run(
        [DOCKER, *argv], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def registry() -> Iterator[tuple[str, int]]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    name = f"piceli-it-retention-{secrets.token_hex(4)}"
    _docker(
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "-p",
        f"127.0.0.1:{port}:5000",
        "-e",
        "REGISTRY_STORAGE_DELETE_ENABLED=true",
        IMAGE,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/v2/", timeout=1)
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        yield name, port
    finally:
        subprocess.run(
            [str(DOCKER), "rm", "-f", name], capture_output=True, check=False
        )


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _push_image(
    client: StreamedOciRegistryClient,
    repository: str,
    tag: str,
    layers: list[bytes],
    config: bytes,
) -> str:
    for blob in [config, *layers]:
        client.push_blob(repository, _sha(blob), len(blob), io.BytesIO(blob))
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": _sha(config),
                "size": len(config),
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    "digest": _sha(layer),
                    "size": len(layer),
                }
                for layer in layers
            ],
        },
        sort_keys=True,
    ).encode()
    return client.push_manifest(repository, tag, manifest, OCI_MANIFEST)


def _blob_bytes(name: str) -> int:
    """Exact bytes of every blob (layers, configs, manifests) the registry stores."""
    out = _docker(
        "exec",
        name,
        "sh",
        "-c",
        "find /var/lib/registry/docker/registry/v2/blobs -name data -type f "
        "| xargs cat | wc -c",
    )
    return int(out)


def _run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict]:
    code = main(["retention", *args])
    lines = [x for x in capsys.readouterr().out.splitlines() if x.strip()]
    return code, json.loads(lines[-1])


def test_a_real_registry_stays_within_its_budget(
    registry: tuple[str, int], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    name, port = registry
    client = StreamedOciRegistryClient(RegistryEndpoint("127.0.0.1", port, False))
    rng = random.Random(7)
    bases = {image: rng.randbytes(size) for image, size in IMAGES.items()}
    digests: dict[tuple[int, str], str] = {}
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    for release in range(1, RELEASES + 1):
        images = {}
        for image in IMAGES:
            own = rng.randbytes(OWN_LAYER)
            config = json.dumps({"image": image, "release": release}).encode()
            repository = f"shop/{image}"
            digest = _push_image(
                client, repository, f"1.{release}", [bases[image], own], config
            )
            digests[(release, image)] = digest
            images[image] = {
                "repository": f"127.0.0.1:{port}/{repository}",
                "digest": digest,
            }
        (receipts / f"{release:02d}.json").write_text(
            json.dumps(
                {
                    "state": "published",
                    "registry": f"127.0.0.1:{port}",
                    "finished_at": f"2026-09-{release:02d}T12:00:00Z",
                    "images": images,
                }
            )
        )
    before = _blob_bytes(name)
    budget = "5MiB"
    live = tmp_path / "live.txt"
    live.write_text(f"{digests[(2, 'api')]}\n")  # a rollback pod runs release 2's api
    base = [
        "--to",
        f"oci://127.0.0.1:{port}/shop",
        "--receipts",
        str(receipts),
        "--keep",
        "2",
        "--budget",
        budget,
        "--live-file",
        str(live),
    ]
    code, report = _run(capsys, *base)
    assert code == 0
    assert report["kept"]["bytes"] <= 5 * 1024 * KB
    kept_releases = [item for item in report["releases"] if item["kept"]]
    assert 2 <= len(kept_releases) < RELEASES
    assert report["collectable"]["reclaimable_bytes"] > 0

    code, asked = _run(capsys, *base, "--delete")
    assert code == 3 and asked["digest"] == report["digest"]
    code, done = _run(capsys, *base, "--delete", "--approve", report["digest"])
    assert code == 0 and done["state"] == "deleted", done
    collected = _docker(
        "exec", name, "registry", "garbage-collect", "/etc/docker/registry/config.yml"
    )
    assert collected is not None
    after = _blob_bytes(name)
    for item in report["manifests"]:  # kept manifests still have every blob
        if item["kept"]:
            body, _ = client.get_manifest(item["repository"], item["digest"])
            document = json.loads(body)
            for blob in [document["config"], *document["layers"]]:
                assert client.has_blob(item["repository"], blob["digest"]), item
    freed = before - after
    assert freed == done["reclaimed_bytes"]
    evidence = (  # bytes reported, freed, and the registry's size
        json.dumps(
            {
                "releases_pushed": RELEASES,
                "releases_kept": len(kept_releases),
                "stored_before_bytes": before,
                "stored_after_bytes": after,
                "reclaimed_bytes_reported": done["reclaimed_bytes"],
                "freed_bytes_measured": freed,
                "budget_bytes": 5 * 1024 * KB,
                "kept_bytes": report["kept"]["bytes"],
            }
        )
    )
    # Every kept digest, and the live one, still serves its manifest and layers.
    assert after <= 5 * 1024 * KB  # what the registry stores is within the budget
    assert after == report["kept"]["bytes"]
    for item in report["manifests"]:
        present = client.manifest_digest(item["repository"], item["digest"]) is not None
        assert present == item["kept"], item
    assert client.manifest_digest("shop/api", digests[(2, "api")]) is not None
    # A second run has nothing left to collect.
    code, again = _run(capsys, *base)
    assert code == 0 and again["collectable"]["manifests"] == 0
    print(evidence)
