"""``docker image save`` output of Docker's containerd image store, for tests.

Docker 29 uses the containerd image store by default (Docker Desktop offers
it as an option). Its image ID is the digest of the image's *index*, not of
its config, and ``docker image save`` writes an OCI layout whose
``index.json`` names that index. The index lists one manifest per platform
the engine holds plus, for a BuildKit build, an attestation manifest
(platform ``unknown/unknown``). A ``manifest.json`` with one entry per
platform sits beside it. This module writes that layout, byte for byte in
the shape Docker 29.9 produces, so the tests need no Docker.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"


def sha(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


@dataclass(frozen=True)
class SavedImage:
    """One containerd-store ``docker image save`` archive and its identities."""

    archive: bytes
    #: ``docker image inspect --format '{{.Id}}'``: the index digest.
    local_id: str
    #: ``{platform: config digest}``: what Kubernetes and containerd report.
    configs: dict[str, str]
    #: ``{platform: manifest digest}``.
    manifests: dict[str, str]

    def inspect(self, platform: str) -> dict[str, object]:
        """``docker image inspect`` of the image, as the engine reports it."""
        os_name, architecture = platform.split("/")[:2]
        return {
            "Id": self.local_id,
            "Os": os_name,
            "Architecture": architecture,
            "Size": len(self.archive),
            "Descriptor": {
                "mediaType": OCI_INDEX,
                "digest": self.local_id,
                "size": 855,
            },
        }


def _add(archive: tarfile.TarFile, name: str, body: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(body)
    archive.addfile(info, io.BytesIO(body))


def containerd_save(
    platforms: tuple[str, ...] = ("linux/arm64",),
    *,
    tag: str = "example/app:1",
    attestation: bool = True,
    marker: bytes = b"hi",
) -> SavedImage:
    """A containerd-store archive holding ``platforms`` (and an attestation)."""
    blobs: dict[str, bytes] = {}
    children: list[dict[str, object]] = []
    entries: list[dict[str, object]] = []
    configs: dict[str, str] = {}
    manifests: dict[str, str] = {}

    def put(body: bytes) -> str:
        blobs[sha(body)] = body
        return sha(body)

    for platform in platforms:
        os_name, architecture = platform.split("/")[:2]
        plain = io.BytesIO()
        with tarfile.open(fileobj=plain, mode="w") as layer_tar:
            _add(layer_tar, "hello.txt", marker + platform.encode())
        layer = gzip.compress(plain.getvalue(), mtime=0)
        config = json.dumps(
            {
                "architecture": architecture,
                "os": os_name,
                "config": {"WorkingDir": "/"},
                "rootfs": {"type": "layers", "diff_ids": [sha(plain.getvalue())]},
            }
        ).encode()
        manifest = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": OCI_MANIFEST,
                "config": {
                    "mediaType": OCI_CONFIG,
                    "digest": put(config),
                    "size": len(config),
                },
                "layers": [
                    {
                        "mediaType": OCI_LAYER_GZIP,
                        "digest": put(layer),
                        "size": len(layer),
                    }
                ],
            },
            indent=2,
        ).encode()
        configs[platform] = sha(config)
        manifests[platform] = put(manifest)
        children.append(
            {
                "mediaType": OCI_MANIFEST,
                "digest": sha(manifest),
                "size": len(manifest),
                "platform": {"architecture": architecture, "os": os_name},
            }
        )
        entries.append(
            {
                "Config": "blobs/sha256/" + sha(config)[7:],
                "RepoTags": [tag],
                "Layers": ["blobs/sha256/" + sha(layer)[7:]],
            }
        )
    if attestation:
        empty = b"{}"
        statement = json.dumps(
            {"_type": "https://in-toto.io/Statement/v1", "subject": []}
        ).encode()
        attest = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": OCI_MANIFEST,
                "artifactType": "application/vnd.docker.attestation.manifest.v1+json",
                "config": {
                    "mediaType": "application/vnd.oci.empty.v1+json",
                    "digest": put(empty),
                    "size": 2,
                },
                "layers": [
                    {
                        "mediaType": "application/vnd.in-toto+json",
                        "digest": put(statement),
                        "size": len(statement),
                    }
                ],
            }
        ).encode()
        children.append(
            {
                "mediaType": OCI_MANIFEST,
                "digest": put(attest),
                "size": len(attest),
                "annotations": {
                    "vnd.docker.reference.digest": children[0]["digest"],
                    "vnd.docker.reference.type": "attestation-manifest",
                },
                "platform": {"architecture": "unknown", "os": "unknown"},
            }
        )
    index = json.dumps(
        {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": children},
        indent=2,
    ).encode()
    local_id = put(index)
    name = tag if "/" in tag.split(":")[0] else "library/" + tag
    top = {
        "schemaVersion": 2,
        "mediaType": OCI_INDEX,
        "manifests": [
            {
                "mediaType": OCI_INDEX,
                "digest": local_id,
                "size": len(index),
                "annotations": {
                    "io.containerd.image.name": "docker.io/" + name,
                    "org.opencontainers.image.ref.name": tag.rsplit(":", 1)[1],
                },
            }
        ],
    }
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        for directory in ("blobs/", "blobs/sha256/"):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        for digest in sorted(blobs):
            _add(archive, "blobs/sha256/" + digest[7:], blobs[digest])
        _add(archive, "index.json", json.dumps(top).encode())
        _add(archive, "manifest.json", json.dumps(entries).encode())
        _add(archive, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
    return SavedImage(out.getvalue(), local_id, configs, manifests)
