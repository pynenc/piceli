"""A disposable forward for the browser showcase: a real loopback listener, no kubectl.

The showcase's fake Kubernetes API cannot port-forward. This supervisor
stands in for ``kubectl port-forward``: it binds the requested loopback port
and answers every request with a short fixture page, so the Forwards
workspace shows a working local address. It is closed with its session.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any


class _Page(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"<!doctype html><title>Fixture forward</title><p>Fixture forward: disposable showcase data.</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:
        pass


class FixtureForward:
    """The ``ForwardSupervisor`` surface ``AccessService`` uses, over a local listener."""

    def __init__(self, *, shortcuts: tuple[Any, ...], **_kwargs: Any) -> None:
        self.port = int(shortcuts[0].local_port)
        self.server: ThreadingHTTPServer | None = None
        self.failed = False

    def quick_start(self, _id: str, _namespace: str) -> None:
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", self.port), _Page)
        except OSError:
            self.failed = True
            return
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def statuses(self) -> tuple[SimpleNamespace, ...]:
        if self.failed:
            return (
                SimpleNamespace(state="failed", health="conflict", reachable=False),
            )
        return (SimpleNamespace(state="running", health="healthy", reachable=True),)

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
