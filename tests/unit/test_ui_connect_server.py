"""``piceli ui connect --server``: https, or plain http only on loopback."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from piceli.k8s.cli.ui_remote import RemoteClientError, _server_url


@pytest.mark.parametrize(
    "server",
    [
        "http://127.0.0.1:8790/",
        "http://127.0.0.1:8790",
        "http://localhost:8790/",
        "http://[::1]:8790/",
        "https://ui.example/piceli",
    ],
)
def test_https_or_loopback_http_is_accepted(server: str) -> None:
    assert _server_url(server, allow_insecure_loopback_test=False).endswith("/")


@pytest.mark.parametrize(
    "server",
    [
        "http://192.0.2.10:8790/",
        "http://ui.example/",
        "http://localhost.example/",
        "ftp://127.0.0.1/",
        "http://user:pw@127.0.0.1:8790/",
    ],
)
def test_plain_http_elsewhere_is_refused(server: str) -> None:
    with pytest.raises(RemoteClientError) as refused:
        _server_url(server, allow_insecure_loopback_test=False)
    assert refused.value.code == "ui-invalid-request"


def _no_httpx2(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_httpx2(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "httpx2":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_httpx2)


def test_connect_without_the_ui_extra_binds_with_the_standard_library(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Binding a port does not need piceli[ui]: no ui-assets-unavailable refusal."""
    from typer.testing import CliRunner

    from piceli.k8s.cli import app

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n")
    _no_httpx2(monkeypatch)
    result = CliRunner().invoke(
        app,
        ["ui", "connect", "--server", "http://127.0.0.1:8790", "--ticket", "a" * 32,
         "--kubeconfig", str(kubeconfig), "--context", "c", "--local-port", "18081",
         "--kubectl", sys.executable],
        input="too-short\n",
    )  # fmt: skip
    assert result.exit_code != 0
    assert "ui-assets-unavailable" not in result.output
    assert "ui-invalid-request" in result.output  # the short pairing secret


def test_the_standard_library_client_posts_json_without_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from piceli.k8s.cli.ui_remote import _StdlibHttp

    seen: list[tuple[str, dict[str, Any]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, body))
            status = 404 if self.path.endswith("/missing") else 200
            payload = json.dumps({"ok": True}).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            pass

    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.1:9")  # never used
    monkeypatch.setenv("http_proxy", "http://192.0.2.1:9")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/ui/"
        http = _StdlibHttp(base, None)
        response = http.post("api/v1/claim", json={"pairing_secret": "x"})
        assert response.status_code == 200 and response.json() == {"ok": True}
        assert http.post("api/v1/missing", json={}).status_code == 404
        http.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert seen == [
        ("/ui/api/v1/claim", {"pairing_secret": "x"}),
        ("/ui/api/v1/missing", {}),
    ]


def test_sigterm_stops_like_ctrl_c_and_restores_the_handler() -> None:
    from piceli.k8s.cli.ui_remote import _terminate_as_interrupt

    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt), _terminate_as_interrupt():
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)  # the handler interrupts this
    assert signal.getsignal(signal.SIGTERM) == before


def test_connect_on_sigterm_reports_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``ui connect`` turns SIGTERM into its orderly stop (the forward's finally)."""
    from typer.testing import CliRunner

    from piceli.k8s.cli import app, ui_remote

    closed: list[bool] = []

    def connected(*args: Any) -> None:
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
        finally:
            closed.append(True)  # where the supervisor closes kubectl

    monkeypatch.setattr(ui_remote, "_connect", connected)
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n")
    result = CliRunner().invoke(
        app,
        ["ui", "connect", "--server", "http://127.0.0.1:8790", "--ticket", "a" * 32,
         "--kubeconfig", str(kubeconfig), "--context", "c", "--local-port", "18081",
         "--kubectl", sys.executable],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert closed == [True]
    assert '"state": "stopped"' in result.output or '"state":"stopped"' in result.output
