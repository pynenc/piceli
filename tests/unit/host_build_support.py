"""A tiny host build project and a base image served by the fake registry."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import Any

from piceli.artifacts.node_facts import NodeFacts

FACTS_16K = NodeFacts(
    "worker-1", "linux", "arm64", "6.6.51+rpt-rpi-2712", 16384, "kernel"
)
FACTS_4K = NodeFacts(
    "worker-1", "linux", "arm64", "6.8.0-45-generic", 4096, "architecture"
)
FACTS_AMD64 = NodeFacts(
    "worker-1", "linux", "amd64", "6.8.0-45-generic", 4096, "architecture"
)
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def sha(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def publish_base(
    registry: Any,
    repository: str = "base/runtime",
    architectures: tuple[str, ...] = ("arm64",),
) -> str:
    """A base (one gzip layer per platform) behind an index; its index digest."""
    entries = []
    for arch in architectures:
        plain = io.BytesIO()
        with tarfile.open(fileobj=plain, mode="w") as archive:
            body = b"ID=base\n" if arch == "arm64" else f"ID=base-{arch}\n".encode()
            info = tarfile.TarInfo("etc/os-release")
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
        layer = gzip.compress(plain.getvalue(), mtime=0)
        config = json.dumps(
            {
                "architecture": arch,
                "os": "linux",
                "config": {"Env": ["PATH=/usr/bin:/bin"], "Cmd": ["sh"]},
                "rootfs": {"type": "layers", "diff_ids": [sha(plain.getvalue())]},
                "history": [{"created_by": "base"}],
            }
        ).encode()
        manifest = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": OCI_MANIFEST,
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": sha(config),
                    "size": len(config),
                },
                "layers": [
                    {
                        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                        "digest": sha(layer),
                        "size": len(layer),
                    }
                ],
            }
        ).encode()
        registry.blobs[(repository, sha(layer))] = layer
        registry.blobs[(repository, sha(config))] = config
        registry.manifests[(repository, sha(manifest))] = (manifest, OCI_MANIFEST)
        entries.append(
            {
                "mediaType": OCI_MANIFEST,
                "digest": sha(manifest),
                "size": len(manifest),
                "platform": {"os": "linux", "architecture": arch},
            }
        )
    index = json.dumps(
        {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": entries}
    ).encode()
    registry.manifests[(repository, sha(index))] = (index, OCI_INDEX)
    return sha(index)


HOST_TOML = """
revision = "piceli.host-build.v1"
name = "shop"

[build]
tools = ["sh"]
commands = [
  ["sh", "-c", "mkdir -p \\"$CARGO_TARGET_DIR/{rust_arch}/release\\" && cat src/main.txt > \\"$CARGO_TARGET_DIR/{rust_arch}/release/web\\" && chmod 755 \\"$CARGO_TARGET_DIR/{rust_arch}/release/web\\" && printf %s \\"$LG_PAGE\\" > \\"$CARGO_TARGET_DIR/{rust_arch}/release/page\\""],
]
env = { LG_PAGE = "{page_size_log2}" }

[context.src]
path = "src"
include = ["*.txt"]

[[output.image]]
name = "web"
repository = "example/web"
base = { image = "127.0.0.1:@PORT@/base/runtime", digest = "@BASE@" }
entrypoint = ["/usr/local/bin/web"]
user = "10001:10001"
files = { "src/static.txt" = "/srv/static.txt" }
target_files = { "{rust_arch}/release/web" = "/usr/local/bin/web", "{rust_arch}/release/page" = "/etc/web/page" }
dirs = { "/var/lib/web" = "10001:10001:0700" }
"""


def write_project(root: Path, port: int, base: str) -> Path:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "main.txt").write_text("v1\n")
    (root / "src" / "static.txt").write_text("static 1\n")
    path = root / "host-build.toml"
    path.write_text(HOST_TOML.replace("@PORT@", str(port)).replace("@BASE@", base))
    return path
