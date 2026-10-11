"""When the controller's GitHub token expires (0.19.0).

GitHub reports a fine-grained or classic token's expiry in the
``GitHub-Authentication-Token-Expiration`` header of any authenticated API
response. The controller asks ``GET https://api.github.com/rate_limit``
(which costs no rate limit) every :data:`CHECK_SECONDS` for each set of
HTTPS credentials of a ``github.com`` source, and warns
:data:`WARN_DAYS` days before the expiry: in ``gitops status``
(``controller.warnings``) and the log. A token without an expiry, another
host or a failed request warns nothing.

Importing this module is side-effect free.
"""

from __future__ import annotations

import base64
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

CHECK_SECONDS = 6 * 3600
WARN_DAYS = 7
API = "https://api.github.com/rate_limit"
HEADER = "GitHub-Authentication-Token-Expiration"
_FORMATS = ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z")

Fetch = Callable[[str, dict[str, str]], dict[str, str]]


def _fetch(url: str, headers: dict[str, str]) -> dict[str, str]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=10) as reply:
        return {key.lower(): value for key, value in reply.headers.items()}


def parse_expiry(value: str | None) -> datetime | None:
    """The header's time (``2026-11-01 12:00:00 UTC``), or ``None``."""
    for form in _FORMATS:
        try:
            parsed = datetime.strptime((value or "").strip(), form)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def github_token_expiry(
    credentials_dir: Path | None, fetch: Fetch = _fetch
) -> datetime | None:
    """The expiry GitHub reports for the token in ``credentials_dir``."""
    if credentials_dir is None:
        return None
    try:
        password = (credentials_dir / "password").read_text().strip()
        username = (credentials_dir / "username").read_text().strip()
    except OSError:
        return None
    if not password:
        return None
    pair = base64.b64encode(f"{username or 'x'}:{password}".encode()).decode()
    try:
        headers = fetch(
            API,
            {
                "Authorization": f"Basic {pair}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "piceli-gitops",
            },
        )
    except Exception:
        return None
    return parse_expiry(headers.get(HEADER.lower()))


def is_github(url: str | None) -> bool:
    """Whether ``url`` is a ``github.com`` remote fetched over HTTPS (a token)."""
    from piceli.dev.delta import canonical_url

    found = canonical_url(url) if (url or "").startswith("https://") else None
    return found is not None and found.startswith("https://github.com/")
