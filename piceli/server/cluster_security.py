"""OIDC code-flow sessions for an explicitly configured HTTPS cluster origin."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request
from starlette.responses import RedirectResponse

from piceli.services.contracts import Principal


@dataclass(frozen=True)
class ClusterSecurityConfig:
    origin: str
    issuer: str
    metadata_url: str
    client_id: str
    client_secret: str | None = field(default=None, repr=False)
    prefix: str = ""
    allow_insecure_loopback_test: bool = False

    def __post_init__(self) -> None:
        origin = urlsplit(self.origin)
        issuer = urlsplit(self.issuer)
        metadata = urlsplit(self.metadata_url)
        schemes = {"https"}
        if self.allow_insecure_loopback_test:
            schemes.add("http")
        for parsed in (origin, issuer, metadata):
            if (
                parsed.scheme not in schemes
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                raise ValueError("invalid cluster OIDC URL")
            if parsed.scheme == "http":
                try:
                    if not ipaddress.ip_address(parsed.hostname).is_loopback:
                        raise ValueError("OIDC test URL must be literal loopback")
                except ValueError:
                    raise ValueError("OIDC test URL must be literal loopback") from None
        if origin.path or origin.query or origin.fragment:
            raise ValueError("cluster origin must not contain a path or query")
        if not self.client_id or len(self.client_id) > 256:
            raise ValueError("invalid OIDC client ID")
        if self.prefix and not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", self.prefix):
            raise ValueError("invalid cluster URL prefix")


@dataclass(frozen=True)
class _Session:
    principal: Principal
    csrf: str
    expires_at: float


class ClusterSecurity:
    """Server-held sessions; browser cookies never contain provider tokens."""

    cookie_name = "piceli_cluster_session"

    def __init__(self, config: ClusterSecurityConfig) -> None:
        self.config = config
        self.oauth = OAuth()
        self.client = self.oauth.register(
            "piceli",
            client_id=config.client_id,
            client_secret=config.client_secret,
            server_metadata_url=config.metadata_url,
            client_kwargs={
                "scope": "openid profile",
                "code_challenge_method": "S256",
            },
        )
        self.login_cookie_key = secrets.token_urlsafe(48)
        self._lock = threading.RLock()
        self._sessions: dict[str, _Session] = {}

    @property
    def callback_uri(self) -> str:
        return self.config.origin + self.config.prefix + "/auth/callback"

    def _same_origin(self, request: Request, *, mutation: bool) -> bool:
        if request.headers.get("host") != urlsplit(self.config.origin).netloc:
            return False
        if request.headers.get("origin", self.config.origin) != self.config.origin:
            return False
        if request.headers.get("sec-fetch-site", "none") not in {
            "none",
            "same-origin",
        }:
            return False
        return not mutation or request.headers.get("origin") == self.config.origin

    def principal(self, request: Request) -> Principal | None:
        token = request.cookies.get(self.cookie_name, "")
        if not token or len(token) > 128:
            return None
        key = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            if session.expires_at <= time.time():
                del self._sessions[key]
                return None
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                csrf = request.headers.get("x-piceli-csrf", "")
                if not hmac.compare_digest(csrf, session.csrf):
                    return None
            return session.principal

    def accepted(self, request: Request, *, api: bool) -> bool:
        if not self._same_origin(
            request, mutation=request.method not in {"GET", "HEAD", "OPTIONS"}
        ):
            return False
        return not api or self.principal(request) is not None

    async def login(self, request: Request) -> RedirectResponse:
        if not self._same_origin(request, mutation=False):
            raise ValueError("invalid login origin")
        metadata = await self.client.load_server_metadata()
        if metadata.get("issuer") != self.config.issuer:
            raise ValueError("OIDC issuer mismatch")
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            endpoint = urlsplit(str(metadata.get(name, "")))
            if not endpoint.hostname or endpoint.username or endpoint.password:
                raise ValueError("OIDC endpoint is invalid")
            if endpoint.scheme != "https":
                try:
                    loopback = ipaddress.ip_address(endpoint.hostname).is_loopback
                except ValueError:
                    loopback = False
                if not (
                    self.config.allow_insecure_loopback_test
                    and endpoint.scheme == "http"
                    and loopback
                ):
                    raise ValueError("OIDC endpoint requires HTTPS")
        return await self.client.authorize_redirect(request, self.callback_uri)

    async def callback(self, request: Request) -> RedirectResponse:
        # A cross-site top-level GET is expected from the identity provider.
        if request.headers.get("host") != urlsplit(self.config.origin).netloc:
            raise ValueError("invalid callback host")
        token = await self.client.authorize_access_token(request)
        info: Any = token.get("userinfo")
        if not isinstance(info, dict) or not isinstance(info.get("sub"), str):
            raise ValueError("verified ID token is required")
        if info.get("iss") != self.config.issuer:
            raise ValueError("OIDC issuer mismatch")
        expiry = info.get("exp")
        if not isinstance(expiry, int) or expiry <= time.time():
            raise ValueError("OIDC ID token expired")
        identity = hashlib.sha256(
            (self.config.issuer + "\0" + info["sub"]).encode()
        ).hexdigest()
        display = info.get("preferred_username") or info["sub"]
        principal = Principal(
            id=identity,
            name=str(display)[:128],
            kind="oidc",
        )
        session_token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        max_age = min(3600, max(1, int(expiry - time.time())))
        with self._lock:
            for key, value in list(self._sessions.items()):
                if value.expires_at <= time.time():
                    del self._sessions[key]
            if len(self._sessions) >= 1000:
                raise ValueError("cluster session capacity reached")
            self._sessions[hashlib.sha256(session_token.encode()).hexdigest()] = (
                _Session(principal, csrf, time.time() + max_age)
            )
        response = RedirectResponse(self.config.prefix + "/applications", 303)
        response.set_cookie(
            self.cookie_name,
            session_token,
            max_age=max_age,
            httponly=True,
            secure=self.config.origin.startswith("https:"),
            samesite="strict",
            path=self.config.prefix or "/",
        )
        response.set_cookie(
            "piceli_csrf",
            csrf,
            max_age=max_age,
            httponly=False,
            secure=self.config.origin.startswith("https:"),
            samesite="strict",
            path=self.config.prefix or "/",
        )
        return response

    def live(self, request: Request, principal: Principal) -> bool:
        return self.principal(request) == principal
