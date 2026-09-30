"""Loopback browser boundary: exact authority, same origin and scoped session.

Who is trusted: the local user who started ``piceli ui serve`` and can read
the launch URL it prints (or its ``0600`` token file). A browser gets the
session only by opening that URL once; any other local process or account
that merely reaches the loopback port gets no session and no CSRF token. On a
remote host behind an SSH tunnel the same holds for the other accounts on
that host.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit

from starlette.requests import Request


@dataclass(frozen=True)
class LocalSecurity:
    origin: str
    prefix: str = ""
    session: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    launch_token: str = field(
        default_factory=lambda: secrets.token_urlsafe(32), repr=False
    )

    def __post_init__(self) -> None:
        if not _LAUNCH_TOKEN.fullmatch(self.launch_token):
            raise ValueError("invalid UI launch token")
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
    def _cookie_suffix(self) -> str:
        return hashlib.sha256((self.origin + self.prefix).encode()).hexdigest()[:12]

    @property
    def cookie_name(self) -> str:
        """The session cookie, named per origin (host and port) and prefix.

        Browsers do not isolate cookies by port: a cookie for ``127.0.0.1`` is
        sent to every loopback port, including a forwarded workload opened in
        the same browser. Distinct names keep two local UIs from overwriting
        each other; they do not hide the values. The session cookie is
        HttpOnly, and the Host, Origin, Fetch Metadata and CSRF checks
        together, not the CSRF token alone, guard every request.
        """
        return "piceli_session_" + self._cookie_suffix

    @property
    def csrf_cookie_name(self) -> str:
        """The script-readable CSRF cookie, named like :attr:`cookie_name`."""
        return "piceli_csrf_" + self._cookie_suffix

    def launch_url(self) -> str:
        """The one address that grants a browser this server's session."""
        return f"{self.origin}{self.prefix}/?token={self.launch_token}"

    def launch_token_matches(self, value: str) -> bool:
        return hmac.compare_digest(
            value.encode("utf-8"), self.launch_token.encode("utf-8")
        )

    def has_session(self, request: Request) -> bool:
        return hmac.compare_digest(
            request.cookies.get(self.cookie_name, "").encode("utf-8"),
            self.session.encode("utf-8"),
        )

    def without_token(self, request: Request) -> str:
        """The requested URL on the configured origin, minus its launch token."""
        query = [
            (key, value)
            for key, value in request.query_params.multi_items()
            if key != "token"
        ]
        target = self.origin + request.url.path
        return target + (f"?{urlencode(query)}" if query else "")

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
        if api and not self.has_session(request):
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


_LAUNCH_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
_TOKEN_IN_TEXT = re.compile(r"([?&]token=)[^&\s\"']*")


def redact_launch_token(text: str) -> str:
    """``text`` with the value of any ``token=`` query parameter replaced."""
    return _TOKEN_IN_TEXT.sub(r"\1[redacted]", text)


class RedactLaunchToken(logging.Filter):
    """Keep the launch token out of server logs (for example access lines)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_launch_token(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_launch_token(item) if isinstance(item, str) else item
                for item in record.args
            )
        return True


def uvicorn_log_config() -> dict[str, Any]:
    """Uvicorn's default logging with the launch token redacted from every line."""
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["piceli_redact_token"] = {"()": RedactLaunchToken}
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = config.setdefault("loggers", {}).setdefault(name, {})
        logger["filters"] = [*logger.get("filters", []), "piceli_redact_token"]
    return config
