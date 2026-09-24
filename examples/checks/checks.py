"""A Python post-deploy check: ``call = "checks.py:login_page"``."""

from __future__ import annotations

from piceli.checks import CheckContext, CheckFailed


def login_page(ctx: CheckContext) -> None:
    response = ctx.http_get("service/web", "/login")  # through a temporary forward
    if response.status != 200 or "Sign in" not in response.text:
        raise CheckFailed(f"login page answered {response.status}")
