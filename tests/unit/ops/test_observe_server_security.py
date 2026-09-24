"""Security baseline for the loopback observe/operator web server (WP0.6)."""

import http.client
import json
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from piceli.k8s.observe import InventoryReport, PreferenceStore
from piceli.k8s.observe_server import MAX_BODY_BYTES, LocalObserveServer
from piceli.k8s.operator_state import FileStateStore, UserStore
from piceli.k8s.ui_config import UiConfig


class _Catalog:
    def selected(self) -> object:
        raise ValueError("none")

    def records(self) -> list[object]:
        return []


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LocalObserveServer]:
    state = FileStateStore(tmp_path / "state")
    instance = LocalObserveServer(
        ("127.0.0.1", 0),
        lambda: InventoryReport("a" * 32, (), ()),
        PreferenceStore(tmp_path / "observe.json"),
        state_store=state,
        kubeconfig=tmp_path / "kubeconfig",
        context="lab",
        kubectl="definitely-not-kubectl",
    )
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)


def _request(
    server: LocalObserveServer,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    token: bool = True,
) -> tuple[int, dict[str, str], bytes]:
    port = server.server_port
    sent = {"Host": f"127.0.0.1:{port}"}
    if token:
        sent["X-Piceli-Local-Token"] = server.local_token
    sent.update(headers or {})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_foreign_host_header_is_rejected(server: LocalObserveServer) -> None:
    for path in ("/", "/healthz", "/v1/status"):
        status, _, body = _request(
            server, "GET", path, headers={"Host": f"evil.example:{server.server_port}"}
        )
        assert status == 403
        assert json.loads(body) == {"error": "forbidden-host"}
    status, _, _ = _request(
        server, "GET", "/healthz", headers={"Host": f"localhost:{server.server_port}"}
    )
    assert status == 200


def test_foreign_origin_post_is_rejected(server: LocalObserveServer) -> None:
    status, _, body = _request(
        server,
        "POST",
        "/v1/artifacts/gc",
        headers={"Origin": "http://evil.example", "Content-Type": "application/json"},
        body=b"{}",
    )
    assert status == 403
    assert json.loads(body) == {"error": "forbidden-origin"}
    status, _, _ = _request(
        server,
        "POST",
        "/v1/artifacts/gc",
        headers={"Origin": f"http://127.0.0.1:{server.server_port}"},
        body=b"{}",
    )
    assert status == 200


def test_api_get_requires_token(server: LocalObserveServer) -> None:
    status, _, body = _request(server, "GET", "/v1/status", token=False)
    assert status == 401
    assert json.loads(body) == {"error": "unauthorized"}
    status, _, _ = _request(
        server,
        "GET",
        "/v1/status",
        token=False,
        headers={"X-Piceli-Local-Token": "wrong"},
    )
    assert status == 401
    status, _, body = _request(server, "GET", "/v1/status")
    assert status == 200
    assert json.loads(body)["session_id"] == "a" * 32


def test_post_without_token_is_unauthorized(server: LocalObserveServer) -> None:
    status, _, _ = _request(server, "POST", "/v1/artifacts/gc", body=b"{}", token=False)
    assert status == 401


def test_oversized_body_is_rejected(server: LocalObserveServer) -> None:
    # The server answers from the declared length without reading the body, so
    # send headers only (streaming 1 MiB would race the server closing the socket).
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.putrequest("POST", "/v1/artifacts/gc", skip_host=True)
        connection.putheader("Host", f"127.0.0.1:{server.server_port}")
        connection.putheader("X-Piceli-Local-Token", server.local_token)
        connection.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 413
        assert json.loads(response.read()) == {"error": "payload-too-large"}
    finally:
        connection.close()
    status, _, body = _request(
        server, "POST", "/v1/artifacts/gc", headers={"Content-Length": "-1"}
    )
    assert status == 400


def test_bad_tail_is_rejected(server: LocalObserveServer) -> None:
    for tail in ("abc", "-5", "0"):
        status, _, body = _request(
            server, "GET", f"/v1/logs/multi?pods=api&tail={tail}"
        )
        assert status == 400
        assert json.loads(body) == {"error": "invalid-tail"}


def test_security_headers_and_csp_nonce(server: LocalObserveServer) -> None:
    status, headers, body = _request(server, "GET", "/", token=False)
    assert status == 200
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert f'<script nonce="{nonce}">'.encode() in body
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    # Inline handlers would be blocked by the nonce CSP; the page uses delegation.
    assert not re.search(rb"\son[a-z]+=", body)
    _, api_headers, _ = _request(server, "GET", "/v1/status")
    assert (
        api_headers["Content-Security-Policy"]
        == "default-src 'none'; frame-ancestors 'none'"
    )
    assert api_headers["X-Content-Type-Options"] == "nosniff"


def test_errors_do_not_leak_exception_detail(tmp_path: Path) -> None:
    def failing_report() -> InventoryReport:
        raise RuntimeError("secret /home/user/.kube/config detail")

    instance = LocalObserveServer(
        ("127.0.0.1", 0), failing_report, PreferenceStore(tmp_path / "observe.json")
    )
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = _request(instance, "GET", "/v1/status")
    finally:
        instance.shutdown()
        instance.server_close()
    assert status == 503
    assert json.loads(body) == {"error": "status-unavailable"}


def test_viewer_bearer_cannot_mutate(server: LocalObserveServer) -> None:
    users = UserStore(server.state_store)
    _, viewer_token = users.create_user("watcher", "viewer", "viewer-token-123")
    _, operator_token = users.create_user("deployer", "operator", "operator-token-123")
    server.catalog = _Catalog()
    viewer = {"Authorization": f"Bearer {viewer_token}"}
    status, _, _ = _request(server, "GET", "/v1/status", token=False, headers=viewer)
    assert status == 200
    for path in ("/v1/releases/promote", "/v1/artifacts/gc", "/v1/forwards/quick"):
        status, _, body = _request(
            server, "POST", path, token=False, headers=viewer, body=b"{}"
        )
        assert status == 403
        assert json.loads(body) == {"error": "forbidden-role"}
    status, _, _ = _request(
        server,
        "POST",
        "/v1/artifacts/gc",
        token=False,
        headers={"Authorization": f"Bearer {operator_token}"},
        body=b"{}",
    )
    assert status == 200


def test_page_embeds_config_safely_and_renders_with_empty_config(
    server: LocalObserveServer,
) -> None:
    server.ui_config = UiConfig.model_validate(
        {"topology_subtitle": "</script><script>alert(1)</script>"}
    )
    _, _, body = _request(server, "GET", "/", token=False)
    assert b"</script><script>alert(1)" not in body
    assert b"\\u003c/script\\u003e" in body
    server.ui_config = UiConfig()
    _, _, body = _request(server, "GET", "/", token=False)
    assert b"__PICELI_" not in body
    assert b'data-testid="topology-grid"' in body


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_page_escape_function_escapes_quotes(server: LocalObserveServer) -> None:
    _, _, body = _request(server, "GET", "/", token=False)
    page = body.decode()
    esc_source = re.search(r"const esc = .*?;\n", page).group(0)
    script = esc_source + "process.stdout.write(esc(`a'b\"c<d>&`));"
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True, timeout=30
    )
    assert result.stdout == "a&#39;b&quot;c&lt;d&gt;&amp;"
    # User values reach actions through data-* attributes, never inline JS.
    assert 'data-name="${esc(f.name)}"' in page
    assert "('${esc(" not in page
