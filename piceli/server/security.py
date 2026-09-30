"""Loopback browser boundary: exact authority, same origin and scoped session."""

from __future__ import annotations

import hmac
import ipaddress
import re
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from starlette.requests import Request


@dataclass(frozen=True)
class LocalSecurity:
    origin: str
    prefix: str = ""
    session: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.origin)
        try:
            local = (
                parsed.hostname == "localhost"
                or ipaddress.ip_address(parsed.hostname or "").is_loopback
            )
        except ValueError:
            local = False
        if (
            not local
            or parsed.scheme not in {"http", "https"}
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("UI origin must be an explicit loopback HTTP origin")
        if self.prefix and not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", self.prefix):
            raise ValueError("invalid UI URL prefix")

    @property
    def cookie_name(self) -> str:
        # Per-origin port/prefix session name avoids sibling local servers.
        import hashlib

        return (
            "piceli_session_"
            + hashlib.sha256((self.origin + self.prefix).encode()).hexdigest()[:12]
        )

    def canonical_navigation(self, request: Request, *, api: bool) -> str | None:
        """Send a loopback host alias to the configured origin before bootstrap."""
        configured = urlsplit(self.origin)
        alias = (
            "localhost"
            if configured.hostname == "127.0.0.1"
            else "127.0.0.1"
            if configured.hostname == "localhost"
            else None
        )
        if (
            api
            or alias is None
            or configured.port is None
            or request.headers.get("host", "").lower() != f"{alias}:{configured.port}"
            or request.method not in {"GET", "HEAD"}
            or "origin" in request.headers
            or request.headers.get("sec-fetch-mode", "navigate") != "navigate"
            or request.headers.get("sec-fetch-dest", "document") != "document"
        ):
            return None
        target = self.origin + request.url.path
        return target + (f"?{request.url.query}" if request.url.query else "")

    def accepted(self, request: Request, *, api: bool) -> bool:
        if request.headers.get("host") != urlsplit(self.origin).netloc:
            return False
        if request.headers.get("origin", self.origin) != self.origin:
            return False
        fetch_site = request.headers.get("sec-fetch-site", "none")
        if fetch_site not in {"none", "same-origin"}:
            # A clicked link from another site is a safe way to open the UI.
            # Permit only a top-level document navigation; API calls and
            # subresources still require a same-origin browser context.
            navigation = (
                not api
                and request.method in {"GET", "HEAD"}
                and request.headers.get("sec-fetch-mode") == "navigate"
                and request.headers.get("sec-fetch-dest") == "document"
                and "origin" not in request.headers
            )
            if fetch_site not in {"cross-site", "same-site"} or not navigation:
                return False
        if api and not hmac.compare_digest(
            request.cookies.get(self.cookie_name, "").encode("utf-8"),
            self.session.encode("utf-8"),
        ):
            return False
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") != self.origin:
                return False
            if not hmac.compare_digest(
                request.headers.get("x-piceli-csrf", "").encode("utf-8"),
                self.csrf.encode("utf-8"),
            ):
                return False
        return True
