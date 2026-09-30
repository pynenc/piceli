"""Only the launch URL grants the local session; the token never reaches logs."""

from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from piceli.server.app import create_app
from piceli.server.security import (
    LocalSecurity,
    redact_launch_token,
    uvicorn_log_config,
)
from piceli.services.query import QueryService

ORIGIN = "http://127.0.0.1:8000"


def _app(tmp_path: Path):  # type: ignore[no-untyped-def]
    (tmp_path / "index.html").write_text("<!doctype html><title>Piceli</title>")
    return create_app(QueryService([]), static_dir=tmp_path)


def _no_cookies(response: httpx.Response) -> bool:
    return "set-cookie" not in response.headers


def test_a_page_without_the_launch_token_grants_nothing(tmp_path: Path) -> None:
    app = _app(tmp_path)
    security: LocalSecurity = app.state.security
    with TestClient(app, base_url=ORIGIN) as client:
        for path in ("/", "/applications", "/applications/shop/resources"):
            page = client.get(path)
            assert page.status_code == 401
            assert _no_cookies(page)
            assert "?token=" in page.text
            assert security.session not in page.text
            assert security.csrf not in page.text
        assert not client.cookies
        assert client.get("/api/v1/capabilities").status_code == 403
        # Static assets stay public; they carry no session.
        assert _no_cookies(client.get("/favicon.ico", follow_redirects=False))


def test_a_wrong_token_is_refused(tmp_path: Path) -> None:
    app = _app(tmp_path)
    token = app.state.security.launch_token
    with TestClient(app, base_url=ORIGIN) as client:
        for query in (
            "token=" + "x" * len(token),
            "token=",
            "token=" + token[:-1],
            f"token={token}&token={token}",
        ):
            refused = client.get(f"/?{query}", follow_redirects=False)
            assert refused.status_code == 403
            assert refused.json()["code"] == "ui-request-rejected"
            assert _no_cookies(refused)
        assert client.get("/api/v1/capabilities").status_code == 403


def test_the_launch_token_is_exchanged_for_a_session_and_dropped(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    security: LocalSecurity = app.state.security
    assert security.launch_url() == f"{ORIGIN}/?token={security.launch_token}"
    with TestClient(app, base_url=ORIGIN) as client:
        launch = client.get(
            f"/applications?view=table&token={security.launch_token}",
            follow_redirects=False,
        )
        assert launch.status_code == 303
        assert launch.headers["location"] == f"{ORIGIN}/applications?view=table"
        cookies = launch.headers.get_list("set-cookie")
        session = next(
            item for item in cookies if item.startswith(security.cookie_name)
        )
        assert "HttpOnly" in session and "SameSite=strict" in session
        assert client.get("/api/v1/capabilities").status_code == 200
        # The session now opens every page without the token.
        assert client.get("/applications/shop").status_code == 200


def test_the_token_redirect_never_leaves_the_origin(tmp_path: Path) -> None:
    app = _app(tmp_path)
    token = app.state.security.launch_token
    with TestClient(app, base_url=ORIGIN) as client:
        launch = client.get(f"//evil.example/x?token={token}", follow_redirects=False)
        assert launch.status_code == 303
        assert launch.headers["location"].startswith(ORIGIN + "/")


def test_a_cross_site_link_reloads_from_the_origin_without_granting(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    navigation = {
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }
    with TestClient(app, base_url=ORIGIN) as client:
        bounce = client.get("/applications?view=table", headers=navigation)
        assert bounce.status_code == 200
        assert _no_cookies(bounce)
        assert (
            '<meta http-equiv="refresh" content="0;url='
            f'{ORIGIN}/applications?view=table">' in bounce.text
        )
        # The reload is same-origin; without a session it still gets nothing.
        again = client.get("/applications?view=table")
        assert again.status_code == 401 and _no_cookies(again)


def test_local_security_rejects_a_weak_token_and_hides_it() -> None:
    with pytest.raises(ValueError):
        LocalSecurity(ORIGIN, launch_token="short")
    security = LocalSecurity(ORIGIN)
    assert security.launch_token not in repr(security)
    assert redact_launch_token(f"GET /?a=1&token={security.launch_token} HTTP/1.1") == (
        "GET /?a=1&token=[redacted] HTTP/1.1"
    )


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_the_launch_token_is_not_logged(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    (tmp_path / "index.html").write_text("<!doctype html><title>Piceli</title>")
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    app = create_app(QueryService([]), origin=origin, static_dir=tmp_path)
    token = app.state.security.launch_token
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="info",
            log_config=uvicorn_log_config(),
        )
    )
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    capture = Capture()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        logging.getLogger("uvicorn.access").addHandler(capture)
        with httpx.Client(base_url=origin) as client:
            assert (
                client.get(f"/?token={token}", follow_redirects=False).status_code
                == 303
            )
            assert client.get("/?token=wrong-token-value").status_code == 403
    finally:
        server.should_exit = True
        thread.join(10)
        logging.getLogger("uvicorn.access").removeHandler(capture)
    output = capfd.readouterr()
    assert any("token=[redacted]" in line for line in records)
    assert all(token not in line for line in records)
    assert token not in output.out and token not in output.err
    assert "wrong-token-value" not in output.err


def test_ui_serve_prints_the_launch_address_and_removes_its_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    from piceli.k8s.cli import app as cli

    seen: dict[str, object] = {}

    def run(server, **kwargs):  # type: ignore[no-untyped-def]
        token = server.state.security.launch_token
        (path,) = (tmp_path / "state").glob("launch-token-8123-*")
        seen["path"] = path
        seen["file"] = path.read_text().strip() == token
        seen["mode"] = path.stat().st_mode & 0o777
        seen["token"] = token
        seen["filters"] = kwargs["log_config"]["loggers"]["uvicorn.access"]["filters"]

    monkeypatch.setattr(uvicorn, "run", run)
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n")
    result = CliRunner().invoke(
        cli,
        [
            "ui",
            "serve",
            "--kubeconfig",
            str(kubeconfig),
            "--context",
            "fake",
            "--namespace",
            "shop",
            "--port",
            "8123",
            "--state-dir",
            str(tmp_path / "state"),
        ],
    )
    assert result.exit_code == 0
    assert seen["file"] is True and seen["mode"] == 0o600
    assert seen["filters"] == ["piceli_redact_token"]
    assert result.output.count(f"http://127.0.0.1:8123/?token={seen['token']}") == 1
    assert not seen["path"].exists()


def test_a_second_server_cannot_replace_the_first_servers_token(tmp_path: Path) -> None:
    from piceli.k8s.ui_state import write_launch_token

    first = write_launch_token(tmp_path, 8123, "first-token")
    second = write_launch_token(tmp_path, 8123, "second-token")
    assert first != second
    assert first.read_text() == "first-token\n"
    assert second.read_text() == "second-token\n"
    second.unlink()
    assert first.read_text() == "first-token\n"
    first.unlink()
