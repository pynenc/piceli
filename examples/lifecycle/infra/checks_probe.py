"""A Python check: the web site serves the configuration the build put there."""

from __future__ import annotations

import json

from piceli.checks import CheckContext, CheckFailed


def site_config(ctx: CheckContext) -> str | None:
    response = ctx.http_get("service/web", "/config.json")
    if response.status != 200:
        raise CheckFailed(f"/config.json returned {response.status}")
    try:
        config = json.loads(response.text)
    except ValueError:
        return "/config.json is not JSON"
    if config.get("name") != "lifecycle":
        return "/config.json names another site"
    return None
