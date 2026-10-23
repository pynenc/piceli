"""A fake Keystone and Glance on a local HTTP server, with fault injection.

Enough of both for :mod:`piceli.infra.machines.glance`: application
credential tokens (issued, revoked), image records (create, get, list by
tags, delete) and ``PUT /v2/images/{id}/file``. Faults: ``drop`` closes the
connection after some bytes of the next uploads (and leaves the image in
``after_drop``: ``saving``, as the real report, or ``queued``), ``corrupt``
makes Glance report a wrong checksum.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

REGION = "region-1"
CRED_ID = "0123456789abcdef0123456789abcdef"
CRED_SECRET = "app-credential-secret-" + "s" * 40


@dataclass
class FakeCloud:
    url: str = ""
    images: dict[str, dict[str, Any]] = field(default_factory=dict)
    issued: list[str] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)
    #: Every token a request carried (X-Auth-Token), and every path.
    seen_tokens: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    #: Close the connection after this many bytes of each of the next ``drops`` uploads.
    drop_after: int = 0
    drops: int = 0
    after_drop: str = "saving"
    corrupt: bool = False
    deleted: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def auth_url(self) -> str:
        return self.url + "/identity/v3"

    def add(self, **record: Any) -> str:
        image_id = str(uuid.uuid4())
        self.images[image_id] = {
            "id": image_id,
            "status": "queued",
            "tags": [],
            **record,
        }
        return image_id


def _handler(cloud: FakeCloud) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:
            pass

        def _reply(
            self, status: int, body: Any = None, headers: dict[str, str] | None = None
        ) -> None:
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            token = self.headers.get("X-Auth-Token") or ""
            cloud.seen_tokens.append(token)
            if token not in cloud.issued or token in cloud.revoked:
                self._reply(401, {"error": "unauthorized"})
                return False
            return True

        def _body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            cloud.requests.append(f"POST {path}")
            if path == "/identity/v3/auth/tokens":
                body = self._body()
                credential = body["auth"]["identity"]["application_credential"]
                if credential != {"id": CRED_ID, "secret": CRED_SECRET}:
                    self._reply(401, {"error": "bad credential"})
                    return
                token = f"tok-{uuid.uuid4().hex}"
                cloud.issued.append(token)
                catalog = [{"type": "image", "endpoints": [
                    {"interface": "public", "region": REGION, "url": cloud.url + "/image"},
                ]}]  # fmt: skip
                self._reply(201, {"token": {"expires_at": "2999-01-01T00:00:00Z",
                                             "catalog": catalog}},
                            {"X-Subject-Token": token})  # fmt: skip
                return
            if path == "/image/v2/images" and self._authorized():
                body = self._body()
                image_id = cloud.add(**body)
                cloud.images[image_id]["status"] = "queued"
                self._reply(201, cloud.images[image_id])
                return

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            cloud.requests.append(f"GET {parts.path}")
            if not self._authorized():
                return
            if parts.path == "/image/v2/images":
                tags = parse_qs(parts.query).get("tag") or []
                found = [
                    i
                    for i in cloud.images.values()
                    if set(tags) <= set(i.get("tags") or ())
                ]
                self._reply(200, {"images": found})
                return
            if parts.path == "/image/v2/info/import":
                self._reply(
                    200,
                    {"import-methods": {"value": ["glance-direct", "web-download"]}},
                )
                return
            image_id = parts.path.rsplit("/", 1)[-1]
            if image_id in cloud.images:
                self._reply(200, cloud.images[image_id])
            else:
                self._reply(404, {"error": "not found"})

        def do_DELETE(self) -> None:
            path = urlsplit(self.path).path
            cloud.requests.append(f"DELETE {path}")
            if path == "/identity/v3/auth/tokens":
                cloud.revoked.append(self.headers.get("X-Subject-Token") or "")
                self._reply(204)
                return
            if not self._authorized():
                return
            image_id = path.rsplit("/", 1)[-1]
            if cloud.images.pop(image_id, None) is None:
                self._reply(404, {"error": "not found"})
                return
            cloud.deleted.append(image_id)
            self._reply(204)

        def do_PUT(self) -> None:
            path = urlsplit(self.path).path
            cloud.requests.append(f"PUT {path}")
            if not self._authorized():
                return
            image_id = path.split("/")[-2]
            record = cloud.images.get(image_id)
            if record is None:
                self._reply(404, {"error": "not found"})
                return
            if record["status"] != "queued":
                self._reply(409, {"error": "conflict"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            record["status"] = "saving"
            with cloud.lock:
                drop = cloud.drops > 0
                if drop:
                    cloud.drops -= 1
            if drop:
                self.rfile.read(min(cloud.drop_after, length))
                record["status"] = cloud.after_drop
                self.close_connection = True
                self.connection.shutdown(2)
                return
            data = b""
            while len(data) < length:
                chunk = self.rfile.read(min(1 << 16, length - len(data)))
                if not chunk:
                    break
                data += chunk
            md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
            record.update(
                status="active", size=len(data),
                checksum="0" * 32 if cloud.corrupt else md5,
                os_hash_algo="sha512", os_hash_value=hashlib.sha512(data).hexdigest(),
            )  # fmt: skip
            self._reply(204)

    return Handler


@contextmanager
def fake_cloud() -> Iterator[FakeCloud]:
    cloud = FakeCloud()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(cloud))
    server.daemon_threads = True
    cloud.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield cloud
    finally:
        server.shutdown()
        server.server_close()
