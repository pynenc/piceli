"""Automatic browser bootstrap for the disposable showcase only.

Production application security stays unchanged. This wrapper feeds safe document
navigations through its existing launch-token exchange; API calls still need the
session cookie and mutations still need same-origin CSRF authorization.
"""

from __future__ import annotations

from urllib.parse import urlencode

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from piceli.server.security import LocalSecurity


class PreviewNavigationBootstrap:
    def __init__(self, app: ASGIApp, *, security: LocalSecurity) -> None:
        self.app = app
        self.security = security

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            path = request.url.path
            accept = request.headers.get("accept", "text/html")
            document = (
                request.method in {"GET", "HEAD"}
                and path != "/api"
                and not path.startswith(("/api/", "/assets/"))
                and "text/html" in accept
                and request.headers.get("sec-fetch-mode", "navigate") == "navigate"
                and request.headers.get("sec-fetch-dest", "document") == "document"
                and self.security.accepted(request, api=False)
            )
            if document and (
                not self.security.has_session(request)
                or "token" in request.query_params
            ):
                query = [
                    (key, value)
                    for key, value in request.query_params.multi_items()
                    if key != "token"
                ]
                # External document links first take the production boundary's
                # same-origin reload. Drop old bookmark tokens before that
                # boundary checks them; bootstrap only on the resulting reload.
                if request.headers.get("sec-fetch-site", "none") in {
                    "none",
                    "same-origin",
                }:
                    query.append(("token", self.security.launch_token))
                scope = {**scope, "query_string": urlencode(query).encode("ascii")}
        await self.app(scope, receive, send)
