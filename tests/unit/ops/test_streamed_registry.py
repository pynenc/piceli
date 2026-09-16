"""Unit tests for streamed OCI distribution registry client."""

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pytest

from piceli.artifacts.registry import RegistryEndpoint, StreamedOciRegistryClient


class FakeOciRegistryHandler(BaseHTTPRequestHandler):
    blobs: dict[str, bytes] = {}
    manifests: dict[str, bytes] = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/v2/":
            self.send_response(200)
            self.send_header("Docker-Distribution-API-Version", "registry/2.0")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        if self.path.startswith("/v2/test/blobs/"):
            digest = self.path.split("/")[-1]
            if digest in self.blobs:
                self.send_response(200)
                self.end_headers()
                return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/v2/test/blobs/uploads/":
            self.send_response(202)
            self.send_header("Location", "/v2/test/blobs/uploads/session-12345")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if "/v2/test/blobs/uploads/" in self.path:
            digest = self.path.split("digest=")[-1]
            self.blobs[digest] = body
            self.send_response(201)
            self.end_headers()
            return
        if self.path.startswith("/v2/test/manifests/"):
            ref = self.path.split("/")[-1]
            self.manifests[ref] = body
            self.send_response(201)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()


def test_streamed_oci_client_upload_and_check() -> None:
    FakeOciRegistryHandler.blobs = {}
    FakeOciRegistryHandler.manifests = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOciRegistryHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        client = StreamedOciRegistryClient(RegistryEndpoint("127.0.0.1", port=port))
        assert client.check_v2_support() is True

        # Check absent blob
        digest = "sha256:" + "a" * 64
        assert client.has_blob("test", digest) is False

        # Stream blob
        data = b"test layer bytes 12345"
        client.push_blob_stream("test", io.BytesIO(data), digest)
        assert client.has_blob("test", digest) is True

        # Push manifest
        manifest_data = json.dumps({"schemaVersion": 2, "config": {"digest": digest}}).encode()
        m_digest = client.push_manifest("test", "latest", manifest_data, "application/vnd.oci.image.manifest.v1+json")
        assert m_digest.startswith("sha256:")
        assert "latest" in FakeOciRegistryHandler.manifests
    finally:
        server.shutdown()
        server.server_close()
