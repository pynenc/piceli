"""One-command UI startup keeps the launch boundary and owns disposable state."""

from __future__ import annotations

import http.cookiejar
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from scripts import ui_preview


def test_browser_opens_after_authenticated_readiness_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    session = http.cookiejar.CookieJar()
    browser = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(session)
    )

    def open_browser(url: str) -> bool:
        # Called only once the launch URL and its authenticated API are usable.
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        unauthenticated = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with pytest.raises(urllib.error.HTTPError) as refused:
            unauthenticated.open(f"{origin}/api/v1/capabilities", timeout=2)
        assert refused.value.code == 403
        with browser.open(url, timeout=2) as page:
            assert page.status == 200
            assert urlsplit(page.url).query == ""
            assert b"<!doctype html>" in page.read().lower()
        with browser.open(f"{origin}/api/v1/capabilities", timeout=2) as response:
            assert json.load(response)["actions"]["composition"]["allowed"] is True
        assert any(cookie.name.startswith("piceli_session_") for cookie in session)
        assert parsed.path == "/composition/overview"
        assert parsed.query == ""
        opened.append(url)
        return True

    monkeypatch.setattr(ui_preview.webbrowser, "open", open_browser)
    with ui_preview.preview_server(port=0) as (process, url):
        assert process.poll() is None
        assert opened == [url]
    assert process.poll() is not None


def test_failed_start_removes_private_state_without_opening_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []
    directories: list[Path] = []
    opened: list[str] = []

    def fail_start(command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        del command
        env = kwargs["env"]
        assert isinstance(env, dict)
        directories.append(Path(env["TMPDIR"]))
        process = original(
            [
                sys.executable,
                "-c",
                "import os; from pathlib import Path; "
                "Path(os.environ['TMPDIR'], 'owned-state').write_text('temporary'); "
                "raise SystemExit(23)",
            ],
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(ui_preview.subprocess, "Popen", fail_start)
    monkeypatch.setattr(ui_preview.webbrowser, "open", lambda url: opened.append(url))
    with pytest.raises(RuntimeError, match="exited during startup"):
        with ui_preview.preview_server(startup_timeout=5, port=0):
            pytest.fail("A failed server must never be yielded as ready")
    assert opened == []
    assert processes[0].returncode == 23
    assert all(not path.exists() for path in directories)


def test_lock_changes_require_install_but_optional_platform_packages_do_not(
    tmp_path: Path,
) -> None:
    (tmp_path / "node_modules/.bin").mkdir(parents=True)
    for name in ("vite", "tsc"):
        (tmp_path / "node_modules/.bin" / name).touch()
    package = {
        "version": "1.0.0",
        "resolved": "https://registry.example/tool",
        "integrity": "sha512-original",
    }
    expected = {
        "": {},
        "node_modules/tool": package,
        "node_modules/optional-platform": {"version": "1.0.0", "optional": True},
    }
    (tmp_path / "package-lock.json").write_text(json.dumps({"packages": expected}))
    installed = {"packages": {"node_modules/tool": package}}
    (tmp_path / "node_modules/.package-lock.json").write_text(json.dumps(installed))
    assert ui_preview.dependencies_current(tmp_path)
    expected["node_modules/tool"] = {**package, "integrity": "sha512-changed"}
    (tmp_path / "package-lock.json").write_text(json.dumps({"packages": expected}))
    assert not ui_preview.dependencies_current(tmp_path)
    (tmp_path / "node_modules/.package-lock.json").unlink()
    assert not ui_preview.dependencies_current(tmp_path)


def test_bookmarked_preview_preserves_deep_link_and_recovers_stale_tokens(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from piceli.server.app import create_app
    from piceli.services.query import QueryService
    from tests.browser.preview_navigation import PreviewNavigationBootstrap

    (tmp_path / "index.html").write_text("<!doctype html><title>Current app</title>")
    app = create_app(QueryService([]), static_dir=tmp_path)
    app.add_middleware(PreviewNavigationBootstrap, security=app.state.security)
    origin = "http://127.0.0.1:8000"
    path = "/applications/shop/resources?resource=deployment-api&panel=logs&filter=api"
    with TestClient(app, base_url=origin, headers={"Accept": "text/html"}) as client:
        for suffix in ("", "&token=old-preview", "&token=old&token=stale"):
            client.cookies.clear()
            response = client.get(path + suffix)
            assert response.status_code == 200
            assert str(response.url) == origin + path
            assert client.get("/api/v1/capabilities").status_code == 200
            assert client.get(path).status_code == 200
        client.cookies.clear()
        alias = client.get("http://localhost:8000" + path)
        assert alias.status_code == 200
        assert str(alias.url) == origin + path
        client.cookies.clear()
        client.cookies.set(
            app.state.security.cookie_name,
            "expired-session",
            domain="127.0.0.1",
            path="/",
        )
        assert client.get(path).status_code == 200
        assert client.get("/api/v1/capabilities").status_code == 200


@pytest.mark.parametrize("fetch_site", ["same-site", "cross-site"])
@pytest.mark.parametrize("suffix", ["", "&token=old-preview"])
def test_external_preview_links_reload_safely_before_bootstrapping(
    tmp_path: Path, fetch_site: str, suffix: str
) -> None:
    from fastapi.testclient import TestClient

    from piceli.server.app import create_app
    from piceli.services.query import QueryService
    from tests.browser.preview_navigation import PreviewNavigationBootstrap

    (tmp_path / "index.html").write_text("<!doctype html><title>Current app</title>")
    app = create_app(QueryService([]), static_dir=tmp_path)
    app.add_middleware(PreviewNavigationBootstrap, security=app.state.security)
    path = "/composition/overview?component=api&view=topology"
    with TestClient(
        app, base_url="http://127.0.0.1:8000", headers={"Accept": "text/html"}
    ) as client:
        response = client.get(
            path + suffix,
            headers={
                "Sec-Fetch-Site": fetch_site,
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            },
        )
        assert response.status_code == 200
        assert 'http-equiv="refresh"' in response.text
        assert "component=api" in response.text
        assert "view=topology" in response.text
        assert "token=" not in response.text
        assert "set-cookie" not in response.headers
        assert client.get("/api/v1/capabilities").status_code == 403
        # The browser's same-origin document reload performs the normal exchange.
        assert (
            client.get(path, headers={"Sec-Fetch-Site": "same-origin"}).status_code
            == 200
        )
        assert client.get("/api/v1/capabilities").status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "headers"),
    [
        ("GET", "/api/v1/capabilities", {}),
        ("POST", "/api/v1/composition/sync", {}),
        ("GET", "/composition/overview", {"Host": "evil.example"}),
        ("GET", "/composition/overview", {"Origin": "https://evil.example"}),
        (
            "GET",
            "/composition/overview",
            {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
        ),
        (
            "GET",
            "/composition/overview",
            {
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": "no-cors",
                "Sec-Fetch-Dest": "image",
            },
        ),
        ("GET", "/assets/missing.js", {}),
    ],
)
def test_preview_bootstrap_does_not_grant_other_requests(
    tmp_path: Path, method: str, path: str, headers: dict[str, str]
) -> None:
    from fastapi.testclient import TestClient

    from piceli.server.app import create_app
    from piceli.services.query import QueryService
    from tests.browser.preview_navigation import PreviewNavigationBootstrap

    (tmp_path / "index.html").write_text("<!doctype html><title>Current app</title>")
    app = create_app(QueryService([]), static_dir=tmp_path)
    app.add_middleware(PreviewNavigationBootstrap, security=app.state.security)
    with TestClient(
        app, base_url="http://127.0.0.1:8000", headers={"Accept": "text/html"}
    ) as client:
        response = client.request(method, path, headers=headers, follow_redirects=False)
        assert "set-cookie" not in response.headers
        assert not client.cookies
        assert response.status_code >= 400
        assert client.get("/api/v1/capabilities").status_code == 403


def test_busy_preview_port_is_not_reused() -> None:
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with pytest.raises(RuntimeError, match="unavailable"):
            ui_preview.available_port(port)
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            connection, _ = listener.accept()
            connection.close()


def test_busy_default_port_falls_back_without_touching_listener(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        monkeypatch.setattr(ui_preview, "DEFAULT_PORT", port)
        selected = ui_preview.available_port()
        assert selected != port
        assert (
            f"Default preview port {port} is busy; using {selected}."
            in capsys.readouterr().out
        )
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            connection, _ = listener.accept()
            connection.close()
