"""Disposable local OIDC issuer exercises real code/PKCE and signed ID tokens."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc import jwk, jwt

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.server.app import create_app
from piceli.server.cluster_security import ClusterSecurity, ClusterSecurityConfig
from piceli.services.authority import ScopePolicy
from piceli.services.query import QueryService
from piceli.services.registration import Registration


@contextmanager
def issuer() -> Iterator[tuple[str, dict[str, bool]]]:
    signing_key = jwk.RSAKey.generate_key(2048, parameters={"kid": "test-key"})
    wrong_key = jwk.RSAKey.generate_key(2048, parameters={"kid": "test-key"})
    flows: dict[str, dict[str, str]] = {}
    flags = {"bad_nonce": False, "bad_signature": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def send_json(self, value: dict[str, Any]) -> None:
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            base = f"http://127.0.0.1:{self.server.server_port}"
            if parsed.path == "/.well-known/openid-configuration":
                return self.send_json(
                    {
                        "issuer": base,
                        "authorization_endpoint": base + "/authorize",
                        "token_endpoint": base + "/token",
                        "jwks_uri": base + "/jwks",
                        "response_types_supported": ["code"],
                        "subject_types_supported": ["public"],
                        "id_token_signing_alg_values_supported": ["RS256"],
                        "code_challenge_methods_supported": ["S256"],
                    }
                )
            if parsed.path == "/jwks":
                return self.send_json({"keys": [signing_key.as_dict(private=False)]})
            if parsed.path == "/authorize":
                values = {key: item[0] for key, item in parse_qs(parsed.query).items()}
                if (
                    values.get("client_id") != "piceli-test"
                    or values.get("code_challenge_method") != "S256"
                    or "openid" not in values.get("scope", "").split()
                ):
                    self.send_error(400)
                    return
                code = hashlib.sha256(values["state"].encode()).hexdigest()
                flows[code] = values
                target = (
                    values["redirect_uri"]
                    + "?code="
                    + code
                    + "&state="
                    + values["state"]
                )
                self.send_response(302)
                self.send_header("Location", target)
                self.end_headers()
                return
            self.send_error(404)

        def do_POST(self) -> None:
            if self.path != "/token":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length > 4096:
                self.send_error(413)
                return
            values = {
                key: item[0]
                for key, item in parse_qs(self.rfile.read(length).decode()).items()
            }
            flow = flows.pop(values.get("code", ""), None)
            challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(values.get("code_verifier", "").encode()).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            if (
                flow is None
                or values.get("grant_type") != "authorization_code"
                or values.get("redirect_uri") != flow["redirect_uri"]
                or challenge != flow["code_challenge"]
            ):
                self.send_error(400)
                return
            base = f"http://127.0.0.1:{self.server.server_port}"
            now = int(time.time())
            token = jwt.encode(
                {"alg": "RS256", "kid": "test-key"},
                {
                    "iss": base,
                    "sub": "operator-1",
                    "aud": "piceli-test",
                    "iat": now,
                    "exp": now + 300,
                    "nonce": "wrong" if flags["bad_nonce"] else flow["nonce"],
                },
                wrong_key if flags["bad_signature"] else signing_key,
            )
            self.send_json(
                {
                    "access_token": "unused-test-token",
                    "token_type": "Bearer",
                    "expires_in": 300,
                    "id_token": token,
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", flags
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_signed_oidc_login_pkce_session_csrf_and_bad_nonce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>Piceli</title>")
    with issuer() as (issuer_url, flags):
        principal_id = hashlib.sha256(
            (issuer_url + "\0operator-1").encode()
        ).hexdigest()
        policy = ScopePolicy({principal_id: {"shop": frozenset({"inspect"})}})
        service = QueryService(
            [
                Registration(
                    "shop",
                    "Shop",
                    KubeconfigTarget(tmp_path / "kc", "explicit", "shop"),
                ),
                Registration(
                    "other",
                    "Other",
                    KubeconfigTarget(tmp_path / "kc", "explicit", "other"),
                ),
            ],
            scope_policy=policy,
        )
        security = ClusterSecurity(
            ClusterSecurityConfig(
                origin="http://127.0.0.1:8000",
                issuer=issuer_url,
                metadata_url=issuer_url + "/.well-known/openid-configuration",
                client_id="piceli-test",
                allow_insecure_loopback_test=True,
            )
        )
        callback_errors = []
        original_callback = security.callback

        async def callback_with_diagnostics(request: Any) -> Any:
            try:
                return await original_callback(request)
            except Exception as error:
                callback_errors.append(repr(error))
                raise

        monkeypatch.setattr(security, "callback", callback_with_diagnostics)
        app = create_app(service, static_dir=tmp_path, cluster_security=security)
        with TestClient(app, base_url="http://127.0.0.1:8000") as browser:
            login = browser.get("/auth/login", follow_redirects=False)
            assert login.status_code == 302
            with httpx.Client() as idp:
                authorization = idp.get(
                    login.headers["location"], follow_redirects=False
                )
            assert authorization.status_code == 302
            callback = browser.get(
                authorization.headers["location"], follow_redirects=False
            )
            assert callback.status_code == 200, callback_errors
            assert (
                '<meta http-equiv="refresh" content="0;url=/applications">'
                in callback.text
            )
            assert [
                item["id"]
                for item in browser.get("/api/v1/applications").json()["items"]
            ] == ["shop"]
            assert browser.get("/api/v1/applications/other").status_code == 404
            mutation = "/api/v1/applications/shop/evaluation-preview"
            assert browser.post(mutation, json={"intent": "deploy"}).status_code == 403
            assert (
                browser.post(
                    mutation,
                    json={"intent": "deploy"},
                    headers={
                        "Origin": "http://127.0.0.1:8000",
                        "X-Piceli-CSRF": browser.cookies[security.csrf_cookie_name],
                    },
                ).status_code
                == 409
            )
            flags["bad_nonce"] = True
            browser.cookies.clear()
            login = browser.get("/auth/login", follow_redirects=False)
            with httpx.Client() as idp:
                authorization = idp.get(
                    login.headers["location"], follow_redirects=False
                )
            rejected = browser.get(
                authorization.headers["location"], follow_redirects=False
            )
            assert rejected.status_code == 403
            assert browser.get("/api/v1/applications").status_code == 403
            flags["bad_nonce"] = False
            flags["bad_signature"] = True
            login = browser.get("/auth/login", follow_redirects=False)
            with httpx.Client() as idp:
                authorization = idp.get(
                    login.headers["location"], follow_redirects=False
                )
            rejected = browser.get(
                authorization.headers["location"], follow_redirects=False
            )
            assert rejected.status_code == 403
            assert browser.get("/api/v1/applications").status_code == 403


NAVIGATE = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}


def test_browser_login_with_fetch_metadata_reaches_the_application(
    tmp_path: Path,
) -> None:
    """The login round trip as a browser sends it, Fetch Metadata included."""
    (tmp_path / "index.html").write_text("<!doctype html><title>Piceli</title>")
    with issuer() as (issuer_url, _flags):
        principal_id = hashlib.sha256(
            (issuer_url + "\0operator-1").encode()
        ).hexdigest()
        service = QueryService(
            [
                Registration(
                    "shop",
                    "Shop",
                    KubeconfigTarget(tmp_path / "kc", "explicit", "shop"),
                )
            ],
            scope_policy=ScopePolicy({principal_id: {"shop": frozenset({"inspect"})}}),
        )
        security = ClusterSecurity(
            ClusterSecurityConfig(
                origin="http://127.0.0.1:8000",
                issuer=issuer_url,
                metadata_url=issuer_url + "/.well-known/openid-configuration",
                client_id="piceli-test",
                allow_insecure_loopback_test=True,
            )
        )
        app = create_app(service, static_dir=tmp_path, cluster_security=security)
        cross_site = {**NAVIGATE, "Sec-Fetch-Site": "cross-site"}
        with TestClient(app, base_url="http://127.0.0.1:8000") as browser:
            # Opened from a link elsewhere: no cookie yet (SameSite=Strict).
            opened = browser.get("/applications", headers=cross_site)
            assert opened.status_code == 200
            assert "set-cookie" not in opened.headers
            assert 'content="0;url=http://127.0.0.1:8000/applications"' in opened.text
            # The same-origin reload finds no session and starts the login.
            same_origin = {**NAVIGATE, "Sec-Fetch-Site": "same-origin"}
            reload = browser.get(
                "/applications", headers=same_origin, follow_redirects=False
            )
            assert reload.status_code == 303
            assert reload.headers["location"] == "/auth/login"
            login = browser.get(
                "/auth/login", headers=same_origin, follow_redirects=False
            )
            assert login.status_code == 302
            with httpx.Client() as idp:
                authorization = idp.get(
                    login.headers["location"], follow_redirects=False
                )
            # The identity provider redirects back: a cross-site navigation.
            callback = browser.get(
                authorization.headers["location"],
                headers=cross_site,
                follow_redirects=False,
            )
            assert callback.status_code == 200
            assert security.cookie_name in callback.headers.get("set-cookie", "")
            assert '<meta http-equiv="refresh" content="0;url=/applications">' in (
                callback.text
            )
            # The page then navigates from this origin, cookie included.
            landed = browser.get("/applications", headers=same_origin)
            assert landed.status_code == 200
            assert "<title>Piceli</title>" in landed.text
            fetch = {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
            assert (
                browser.get(
                    "/api/v1/applications",
                    headers={**fetch, "Sec-Fetch-Site": "same-origin"},
                ).status_code
                == 200
            )
            # Everything but a top-level document navigation stays same-origin.
            for method, path, headers in (
                (
                    "GET",
                    "/api/v1/applications",
                    {**fetch, "Sec-Fetch-Site": "cross-site"},
                ),
                ("GET", "/api/v1/applications", {**cross_site}),
                (
                    "GET",
                    "/applications",
                    {**cross_site, "Sec-Fetch-Dest": "iframe"},
                ),
                (
                    "GET",
                    "/applications",
                    {**cross_site, "Origin": "https://foreign.example"},
                ),
                ("GET", "/favicon.svg", {**cross_site, "Sec-Fetch-Dest": "image"}),
                (
                    "POST",
                    "/api/v1/applications/shop/evaluation-preview",
                    {**cross_site, "Origin": "https://foreign.example"},
                ),
                ("POST", "/auth/login", cross_site),
            ):
                refused = browser.request(
                    method, path, headers=headers, follow_redirects=False
                )
                assert refused.status_code == 403, (method, path, headers)
            assert (
                browser.get(
                    "/auth/login",
                    headers={
                        **NAVIGATE,
                        "Sec-Fetch-Site": "cross-site",
                        "Origin": "null",
                    },
                    follow_redirects=False,
                ).status_code
                == 403
            )
