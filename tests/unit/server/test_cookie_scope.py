"""Two local UIs in one browser keep separate session and CSRF cookies."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from piceli.server.app import create_app
from piceli.services.query import QueryService

MUTATION = "/api/v1/applications/shop/evaluation-preview"


def _launched(tmp_path: Path, port: int) -> TestClient:
    static = tmp_path / str(port)
    static.mkdir()
    (static / "index.html").write_text(
        "<!doctype html><html><head><title>Piceli</title></head></html>"
    )
    origin = f"http://127.0.0.1:{port}"
    app = create_app(QueryService([]), origin=origin, static_dir=static)
    client = TestClient(app, base_url=origin)
    assert client.get(f"/?token={app.state.security.launch_token}").status_code == 200
    return client


def test_cookie_names_are_per_instance_and_strict(tmp_path: Path) -> None:
    first = _launched(tmp_path, 8000)
    second = _launched(tmp_path, 8001)
    a = first.app.state.security  # type: ignore[attr-defined]
    b = second.app.state.security  # type: ignore[attr-defined]
    assert a.cookie_name != b.cookie_name
    assert a.csrf_cookie_name != b.csrf_cookie_name
    assert a.csrf_cookie_name.startswith("piceli_csrf_")
    page = first.get("/applications")
    cookies = page.headers.get_list("set-cookie")
    session = next(item for item in cookies if item.startswith(a.cookie_name + "="))
    csrf = next(item for item in cookies if item.startswith(a.csrf_cookie_name + "="))
    for value in (session, csrf):
        assert "Path=/" in value and "SameSite=strict" in value
    assert "HttpOnly" in session and "HttpOnly" not in csrf
    # The page names its own CSRF cookie, so its script never reads another's.
    meta = re.search(r'<meta name="piceli-csrf-cookie" content="([^"]+)">', page.text)
    assert meta is not None and meta.group(1) == a.csrf_cookie_name


def test_a_second_local_ui_does_not_break_the_first(tmp_path: Path) -> None:
    first = _launched(tmp_path, 8000)
    second = _launched(tmp_path, 8001)
    # A browser sends 127.0.0.1 cookies to every port: share one jar.
    first.cookies.update(second.cookies)
    security = first.app.state.security  # type: ignore[attr-defined]
    token = first.cookies[security.csrf_cookie_name]
    guarded = first.post(
        MUTATION,
        json={"intent": "deploy"},
        headers={"Origin": "http://127.0.0.1:8000", "X-Piceli-CSRF": token},
    )
    # Past the guard: this app has no deployment service, so 409, not 403.
    assert guarded.status_code == 409
    other = second.cookies[
        second.app.state.security.csrf_cookie_name  # type: ignore[attr-defined]
    ]
    refused = first.post(
        MUTATION,
        json={"intent": "deploy"},
        headers={"Origin": "http://127.0.0.1:8000", "X-Piceli-CSRF": other},
    )
    assert refused.status_code == 403
