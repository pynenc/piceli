"""An in-process registry with the retention-relevant API: catalog, tag listing
(paginated), manifests with DELETE, and a garbage collector.

Blobs are stored once for the whole registry (as a real registry does), so the
bytes a garbage collection frees can be compared with what retention reported.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"


def sha(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


class RetentionRegistry:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.tags: dict[tuple[str, str], str] = {}
        self.delete_enabled = True
        self.deleted_manifest_bytes = 0  # stored as blobs, freed by the next GC
        self.catalog = True
        self.page = 1000
        self.requests: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        registry = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def _any(self) -> None:
                registry.handle(self)

            do_GET = do_HEAD = do_DELETE = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(
            target=self.server.serve_forever, args=(0.05,), daemon=True
        )
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # --- content -------------------------------------------------------------

    def add_blob(self, data: bytes) -> dict[str, Any]:
        self.blobs[sha(data)] = data
        return {"digest": sha(data), "size": len(data)}

    def add_image(
        self,
        repository: str,
        tag: str | None,
        layers: list[bytes],
        *,
        arch: str = "arm64",
        subject: str | None = None,
    ) -> str:
        config = self.add_blob(
            json.dumps({"architecture": arch, "os": "linux"}).encode() + layers[-1][:8]
        )
        document: dict[str, Any] = {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                **config,
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    **self.add_blob(x),
                }
                for x in layers
            ],
        }
        if subject is not None:
            document["subject"] = {
                "mediaType": OCI_MANIFEST,
                "digest": subject,
                "size": 1,
            }
        return self._store(repository, tag, document, OCI_MANIFEST)

    def add_sparse_image(
        self, repository: str, tag: str | None, layers: list[tuple[str, int]]
    ) -> str:
        """An image whose layers only *declare* their size (nothing is stored):
        to model gigabytes of registry growth without writing any."""
        document = {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": sha(f"config:{repository}:{tag}".encode()),
                "size": 1500,
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    "digest": sha(label.encode()),
                    "size": size,
                }
                for label, size in layers
            ],
        }
        return self._store(repository, tag, document, OCI_MANIFEST)

    def add_index(self, repository: str, tag: str | None, children: list[str]) -> str:
        document = {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": [
                {
                    "mediaType": OCI_MANIFEST,
                    "digest": child,
                    "size": len(self.manifests[(repository, child)][0]),
                }
                for child in children
            ],
        }
        return self._store(repository, tag, document, OCI_INDEX)

    def _store(
        self, repository: str, tag: str | None, document: dict[str, Any], media: str
    ) -> str:
        body = json.dumps(document, sort_keys=True).encode()
        digest = sha(body)
        self.manifests[(repository, digest)] = (body, media)
        if tag is not None:
            self.tags[(repository, tag)] = digest
        return digest

    def attest(self, repository: str, subject: str, marker: bytes) -> str:
        """An SBOM-like referrer, tagged with the ``sha256-<hex>`` fallback tag."""
        digest = self.add_image(repository, None, [marker * 50], subject=subject)
        self.tags[(repository, subject.replace(":", "-"))] = digest
        return digest

    def garbage_collect(self) -> int:
        """Delete every blob no manifest references (and the deleted manifests' own
        bytes, which a real registry stores as blobs); the bytes freed."""
        used: set[str] = set()
        for body, _ in self.manifests.values():
            document = json.loads(body)
            if "config" in document:
                used.add(document["config"]["digest"])
                used.update(item["digest"] for item in document["layers"])
        freed, self.deleted_manifest_bytes = self.deleted_manifest_bytes, 0
        for digest in list(self.blobs):
            if digest not in used:
                freed += len(self.blobs.pop(digest))
        return freed

    def deletes(self) -> list[str]:
        return [path for method, path in self.requests if method == "DELETE"]

    # --- HTTP ----------------------------------------------------------------

    @staticmethod
    def _reply(
        handler: BaseHTTPRequestHandler,
        status: int,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        handler.send_response(status)
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        if handler.command != "HEAD" and body:
            handler.wfile.write(body)

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        method = handler.command
        parts = urlsplit(handler.path)
        path, query = parts.path, parse_qs(parts.query)
        with self.lock:
            self.requests.append((method, handler.path))
        if path == "/v2/":
            return self._reply(handler, 200, b"{}")
        if path == "/v2/_catalog":
            if not self.catalog:
                return self._reply(handler, 404)
            names = sorted({repo for repo, _ in self.manifests})
            return self._page(handler, path, query, "repositories", names)
        tail = path[len("/v2/") :]
        if tail.endswith("/tags/list"):
            repo = tail[: -len("/tags/list")]
            names = sorted(tag for name, tag in self.tags if name == repo)
            if not names and not any(r == repo for r, _ in self.manifests):
                return self._reply(handler, 404)
            return self._page(handler, path, query, "tags", names, repo)
        if "/manifests/" in tail:
            repo, reference = tail.split("/manifests/", 1)
            return self._manifest(handler, method, repo, reference)
        return self._reply(handler, 404)

    def _page(self, handler, path, query, key, names, name=None):  # type: ignore[no-untyped-def]
        last = query.get("last", [None])[0]
        if last is not None:
            names = [item for item in names if item > last]
        chosen = names[: self.page]
        headers = {}
        if len(names) > self.page:
            headers["Link"] = f'<{path}?last={chosen[-1]}&n={self.page}>; rel="next"'
        body = json.dumps({"name": name, key: chosen or None}).encode()
        return self._reply(handler, 200, body, headers)

    def _manifest(self, handler, method, repo, reference):  # type: ignore[no-untyped-def]
        digest = self.tags.get((repo, reference), reference)
        stored = self.manifests.get((repo, digest))
        if method == "DELETE":
            if not self.delete_enabled:
                return self._reply(handler, 405)
            if stored is None or not reference.startswith("sha256:"):
                return self._reply(handler, 404)
            self.deleted_manifest_bytes += len(self.manifests.pop((repo, digest))[0])
            for key in [
                k for k, v in self.tags.items() if k[0] == repo and v == digest
            ]:
                del self.tags[key]
            return self._reply(handler, 202)
        if stored is None:
            return self._reply(handler, 404)
        return self._reply(
            handler,
            200,
            stored[0],
            {"Docker-Content-Digest": digest, "Content-Type": stored[1]}
            | ({"Content-Length": str(len(stored[0]))} if method == "HEAD" else {}),
        )
