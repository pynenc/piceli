"""Independent HTTP boundaries and packaged route behavior."""

import hashlib
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import pytest
from fastapi.testclient import TestClient

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.server.app import create_app
from piceli.server.cluster_security import (
    ClusterSecurity,
    ClusterSecurityConfig,
    _Session,
)
from piceli.server.security import LocalSecurity
from piceli.services.authority import ScopePolicy
from piceli.services.contracts import Principal
from piceli.services.query import QueryService
from piceli.services.registration import Registration


def test_local_ui_import_does_not_load_cluster_oidc() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import piceli.server.app; import sys; assert 'authlib' not in sys.modules",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_session_origin_host_and_fetch_metadata(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>Piceli</title>")
    app = create_app(QueryService([]), static_dir=tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/v1/capabilities").status_code == 403
        assert (
            client.get("/", headers={"Origin": "https://foreign.example"}).status_code
            == 403
        )
        assert not client.cookies
        assert (
            client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        )
        navigation = {
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        }
        assert client.get("/applications", headers=navigation).status_code == 200
        assert client.cookies
        assert (
            client.get(
                "/applications",
                headers={**navigation, "Origin": "https://foreign.example"},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/favicon.svg",
                headers={**navigation, "Sec-Fetch-Dest": "image"},
            ).status_code
            == 403
        )
        assert client.get("/api/v1/capabilities", headers=navigation).status_code == 403
        alias = client.get(
            "/applications?target=shop",
            headers={**navigation, "Host": "localhost:8000"},
            follow_redirects=False,
        )
        assert alias.status_code == 307
        assert alias.headers["location"] == (
            "http://127.0.0.1:8000/applications?target=shop"
        )
        assert "set-cookie" not in alias.headers
        assert (
            client.get(
                "/api/v1/capabilities",
                headers={**navigation, "Host": "localhost:8000"},
            ).status_code
            == 403
        )
        assert client.get("/").status_code == 200
        assert client.get("/api/v1/capabilities").status_code == 200
        assert (
            client.get(
                "/api/v1/capabilities", headers={"Host": "foreign.example"}
            ).status_code
            == 403
        )
        assert (
            client.get("/api/v1/capabilities", headers={"Origin": "null"}).status_code
            == 403
        )
        assert (
            client.get(
                "/api/v1/capabilities", headers={"Sec-Fetch-Site": "same-site"}
            ).status_code
            == 403
        )
        error = client.get("/api/v1/applications/unknown")
        assert error.status_code == 404
        assert error.json()["code"] == "ui-not-found"
        assert error.json()["correlation_id"]


def test_prefix_deep_links_and_missing_assets(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(
        '<!doctype html><html><head><title>Piceli</title><script src="./assets/app-hash.js"></script></head></html>'
    )
    (tmp_path / "app.js").write_text("console.log('local')")
    (tmp_path / "favicon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app-hash.js").write_text("console.log('bundled')")
    app = create_app(QueryService([]), static_dir=tmp_path, url_prefix="/piceli")
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.get("/piceli/applications/shop/resources")
        assert response.status_code == 200
        assert "Piceli" in response.text
        base = re.search(r'<base href="([^"]+)"', response.text)
        assert base is not None
        script = re.search(r'src="([^"]+)"', response.text)
        assert script is not None
        resolved = urljoin(urljoin(str(response.url), base.group(1)), script.group(1))
        assert resolved == "http://127.0.0.1:8000/piceli/assets/app-hash.js"
        asset = client.get(resolved)
        assert asset.status_code == 200
        assert asset.headers["Cache-Control"] == "public, max-age=31536000, immutable"
        assert "set-cookie" not in asset.headers
        assert response.headers["Cache-Control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert client.get("/piceli/api/v1/applications").json()["items"] == []
        assert client.get("/piceli/app.js").status_code == 200
        assert "image/svg+xml" in client.get("/favicon.ico").headers["content-type"]
        assert (
            client.get("/.well-known/appspecific/com.chrome.devtools.json").status_code
            == 204
        )
        assert client.get("/piceli/assets/missing.js").status_code == 404
        assert client.get("/piceli/api/v1/missing").status_code == 404
        assert client.get("/api/v1/capabilities").status_code == 404


@pytest.mark.parametrize(
    "origin",
    [
        "http://0.0.0.0:8000",
        "https://foreign.example",
        "http://localhost/path",
        "http://user@localhost",
    ],
)
def test_refuses_nonlocal_origin(origin: str) -> None:
    with pytest.raises(ValueError):
        LocalSecurity(origin)


def test_cluster_session_scopes_http_pages_and_revocation(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>Piceli</title>")
    principal = Principal(id="alice", name="Alice", kind="oidc")
    policy = ScopePolicy({"alice": {"shop": frozenset({"inspect"})}})
    service = QueryService(
        [
            Registration(
                "shop", "Shop", KubeconfigTarget(tmp_path / "kc", "explicit", "shop")
            ),
            Registration(
                "other", "Other", KubeconfigTarget(tmp_path / "kc", "explicit", "other")
            ),
        ],
        scope_policy=policy,
    )
    security = ClusterSecurity(
        ClusterSecurityConfig(
            origin="http://127.0.0.1:8000",
            issuer="http://127.0.0.1:9000",
            metadata_url="http://127.0.0.1:9000/.well-known/openid-configuration",
            client_id="piceli-test",
            allow_insecure_loopback_test=True,
        )
    )
    app = create_app(
        service,
        static_dir=tmp_path,
        cluster_security=security,
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/v1/applications").status_code == 403
        assert client.get("/", follow_redirects=False).headers["location"] == (
            "/auth/login"
        )
        cookie = "valid-session"
        security._sessions[hashlib.sha256(cookie.encode()).hexdigest()] = _Session(
            principal, "csrf", time.time() + 60
        )
        client.cookies.set(security.cookie_name, cookie)
        page = client.get("/api/v1/applications")
        assert page.status_code == 200
        assert [item["id"] for item in page.json()["items"]] == ["shop"]
        assert client.get("/api/v1/capabilities").json()["principal"]["id"] == "alice"
        assert client.get("/api/v1/applications/other").status_code == 404
        policy.replace({})
        assert client.get("/api/v1/applications").json()["items"] == []
        assert client.get("/api/v1/applications/shop").status_code == 404
        del security._sessions[hashlib.sha256(cookie.encode()).hexdigest()]
        assert client.get("/api/v1/applications").status_code == 403
