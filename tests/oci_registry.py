"""An in-process OCI Distribution v2 registry for tests (no network beyond loopback).

It serves and stores manifests (image manifests and indexes) and blobs, can
require bearer auth, can redirect blob downloads to a second origin (like a
CDN), and validates index pushes like distribution does: every child must be
present unless ``index_platforms`` lists the platforms that must be.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"
USER, PASSWORD = "mirror-user", "mirror-secret-value"


def sha(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


class OciRegistry:
    """Just enough OCI Distribution v2 for copies between two registries."""

    def __init__(self, *, auth: str | None = None) -> None:
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.manifests: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.uploads: dict[str, bytearray] = {}
        self.requests: list[tuple[str, str]] = []
        self.authorizations: list[str | None] = []
        self.auth = auth  # None or "bearer" (token for USER/PASSWORD)
        self.anonymous_token = True
        self.redirect_to: OciRegistry | None = None  # blob GETs redirect there
        self.index_platforms: set[tuple[str, str]] | None = None
        self.tamper: set[str] = set()  # manifest digests served with wrong bytes
        self.lock = threading.Lock()
        registry = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def _any(self) -> None:
                registry.handle(self)

            do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(
            target=self.server.serve_forever, args=(0.05,), daemon=True
        )
        self.thread.start()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # --- content ---------------------------------------------------------------

    def add_image(
        self, repository: str, *, arch: str = "arm64", marker: bytes = b"layer"
    ) -> tuple[str, bytes]:
        """Store a single-platform image; returns (manifest digest, manifest)."""
        config = json.dumps(
            {"architecture": arch, "os": "linux", "rootfs": {"type": "layers"}}
        ).encode()
        layer = marker * 1000
        self.blobs[(repository, sha(config))] = config
        self.blobs[(repository, sha(layer))] = layer
        manifest = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": OCI_MANIFEST,
                "config": {
                    "mediaType": OCI_CONFIG,
                    "digest": sha(config),
                    "size": len(config),
                },
                "layers": [
                    {"mediaType": OCI_LAYER, "digest": sha(layer), "size": len(layer)}
                ],
            }
        ).encode()
        self.manifests[(repository, sha(manifest))] = (manifest, OCI_MANIFEST)
        return sha(manifest), manifest

    def add_index(
        self, repository: str, arches: tuple[str, ...] = ("amd64", "arm64")
    ) -> tuple[str, dict[str, str]]:
        """Store a multi-arch index; returns (index digest, {arch: child digest})."""
        children = {}
        entries = []
        for arch in arches:
            digest, body = self.add_image(
                repository, arch=arch, marker=arch.encode() + b"-layer"
            )
            children[arch] = digest
            platform = {"architecture": arch, "os": "linux"}
            if arch == "arm64":
                platform["variant"] = "v8"
            entries.append(
                {
                    "mediaType": OCI_MANIFEST,
                    "digest": digest,
                    "size": len(body),
                    "platform": platform,
                }
            )
        index = json.dumps(
            {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": entries}
        ).encode()
        self.manifests[(repository, sha(index))] = (index, OCI_INDEX)
        return sha(index), children

    def mutations(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.requests if m in {"POST", "PUT", "PATCH"}]

    # --- HTTP ------------------------------------------------------------------

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
        if "Content-Length" not in (headers or {}):
            handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        if handler.command != "HEAD" and body:
            handler.wfile.write(body)

    def _authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        header = handler.headers.get("Authorization")
        self.authorizations.append(header)
        if self.auth is None or header in {"Bearer issued-token", "Bearer anon-token"}:
            return True
        challenge = f'Bearer realm="http://{self.host}/token",service="fake"'
        self._reply(handler, 401, b"{}", {"WWW-Authenticate": challenge})
        return False

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        method = handler.command
        parts = urlsplit(handler.path)
        path, query = parts.path, parse_qs(parts.query)
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        with self.lock:
            self.requests.append((method, handler.path))
        if path.startswith("/cdn/"):
            # A second origin: no auth, serves blobs by digest.
            if handler.headers.get("Authorization"):
                return self._reply(handler, 400, b"credentials leaked to the CDN")
            digest = path.rsplit("/", 1)[1]
            blob = next(
                (data for (_, d), data in self.blobs.items() if d == digest), None
            )
            return self._reply(handler, 404 if blob is None else 200, blob or b"")
        if path == "/token":
            expected = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
            scope = query.get("scope", [""])[0]
            if handler.headers.get("Authorization") == f"Basic {expected}":
                return self._reply(handler, 200, b'{"token":"issued-token"}')
            if self.anonymous_token and scope.endswith(":pull"):
                return self._reply(handler, 200, b'{"token":"anon-token"}')
            return self._reply(handler, 401)
        if not self._authorized(handler):
            return None
        if path == "/v2/":
            return self._reply(handler, 200, b"{}")
        repo, kind, rest = self._split(path)
        if kind == "blobs" and rest.startswith("uploads/"):
            return self._upload(handler, method, repo, rest[8:], query, body)
        if kind == "blobs":
            blob = self.blobs.get((repo, rest))
            if blob is None:
                return self._reply(handler, 404)
            if method == "GET" and self.redirect_to is not None:
                self.redirect_to.blobs[(repo, rest)] = blob
                location = f"http://{self.redirect_to.host}/cdn/{rest}"
                return self._reply(handler, 307, b"", {"Location": location})
            return self._reply(
                handler,
                200,
                blob if method == "GET" else b"",
                {"Docker-Content-Digest": rest}
                | ({"Content-Length": str(len(blob))} if method == "HEAD" else {}),
            )
        if kind == "manifests":
            return self._manifest(handler, method, repo, rest, body)
        return self._reply(handler, 404)

    @staticmethod
    def _split(path: str) -> tuple[str, str, str]:
        tail = path[len("/v2/") :]
        for kind in ("/blobs/", "/manifests/"):
            if kind in tail:
                repo, rest = tail.split(kind, 1)
                return repo, kind.strip("/"), rest
        return "", "", ""

    def _upload(self, handler, method, repo, session, query, body):  # type: ignore[no-untyped-def]
        if method == "POST":
            new = uuid.uuid4().hex
            self.uploads[new] = bytearray()
            return self._reply(
                handler, 202, b"", {"Location": f"/v2/{repo}/blobs/uploads/{new}"}
            )
        if session not in self.uploads:
            return self._reply(handler, 404)
        if method == "PUT":
            data = bytes(self.uploads.pop(session) + body)
            digest = query["digest"][0]
            if sha(data) != digest:
                return self._reply(
                    handler, 400, b'{"errors":[{"code":"DIGEST_INVALID"}]}'
                )
            self.blobs[(repo, digest)] = data
            return self._reply(handler, 201, b"", {"Docker-Content-Digest": digest})
        if method == "PATCH":
            self.uploads[session].extend(body)
            return self._reply(
                handler, 202, b"", {"Location": f"/v2/{repo}/blobs/uploads/{session}"}
            )
        return self._reply(handler, 405)

    def _manifest(self, handler, method, repo, reference, body):  # type: ignore[no-untyped-def]
        if method == "PUT":
            document = json.loads(body)
            media = handler.headers["Content-Type"]
            if "manifests" in document:
                for entry in document["manifests"]:
                    platform = entry.get("platform") or {}
                    needed = (
                        self.index_platforms is None
                        or (
                            platform.get("os"),
                            platform.get("architecture"),
                        )
                        in self.index_platforms
                    )
                    if needed and (repo, entry["digest"]) not in self.manifests:
                        return self._reply(
                            handler,
                            400,
                            b'{"errors":[{"code":"MANIFEST_BLOB_UNKNOWN"}]}',
                        )
            else:
                for desc in [document["config"], *document["layers"]]:
                    if (repo, desc["digest"]) not in self.blobs:
                        return self._reply(
                            handler, 400, b'{"errors":[{"code":"BLOB_UNKNOWN"}]}'
                        )
            digest = sha(body)
            if reference.startswith("sha256:") and reference != digest:
                return self._reply(
                    handler, 400, b'{"errors":[{"code":"DIGEST_INVALID"}]}'
                )
            self.manifests[(repo, digest)] = (body, media)
            if not reference.startswith("sha256:"):  # a tag points at it too
                self.manifests[(repo, reference)] = (body, media)
            return self._reply(handler, 201, b"", {"Docker-Content-Digest": digest})
        stored = self.manifests.get((repo, reference))
        if stored is None:
            return self._reply(handler, 404)
        served = stored[0] + (b" " if reference in self.tamper else b"")
        return self._reply(
            handler,
            200,
            served,
            {"Docker-Content-Digest": sha(stored[0]), "Content-Type": stored[1]}
            | ({"Content-Length": str(len(served))} if method == "HEAD" else {}),
        )


@contextmanager
def oci_registry(**options: Any) -> Iterator[OciRegistry]:
    registry = OciRegistry(**options)
    try:
        yield registry
    finally:
        registry.close()
