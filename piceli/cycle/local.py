"""The host switches ``piceli host wait`` saw, kept on this machine.

A Nix host tells the controller nothing (it pulls its builds), so the CLI
keeps what it sees: each attempt ``host wait`` ended on, once, in
``$XDG_STATE_HOME/piceli/cycle/hosts.json`` (else
``~/.local/state/piceli/cycle/hosts.json``), at most :data:`MAX_HOSTS`
entries (the newest). ``piceli cycle report`` reads it.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA = "piceli.cycle-hosts.v1"
#: Host attempts kept (the newest).
MAX_HOSTS = 200
#: The attempt fields kept (no output, no secret).
_ATTEMPT = ("commit", "tag", "ref", "published_at", "started_at", "ended_at", "state")
_STAGE = ("name", "state", "seconds")


def root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "piceli" / "cycle"


def hosts_path(base: Path | None = None) -> Path:
    return (base or root()) / "hosts.json"


def read_hosts(base: Path | None = None) -> list[dict[str, Any]]:
    """The kept host attempts, oldest first (empty when none or unreadable)."""
    try:
        body = json.loads(hosts_path(base).read_text())
    except (OSError, ValueError):
        return []
    items = body.get("attempts") if isinstance(body, Mapping) else None
    return [dict(item) for item in items or () if isinstance(item, Mapping)]


def remember_host(
    host: str, attempt: Mapping[str, Any], base: Path | None = None
) -> bool:
    """Keep ``host``'s finished ``attempt`` once (by host, commit and start);
    ``False`` when it was kept already or is not finished. Never raises."""
    if not attempt.get("ended_at") or not attempt.get("started_at"):
        return False
    entry: dict[str, Any] = {"host": str(host)[:64]}
    entry.update({key: attempt.get(key) for key in _ATTEMPT})
    entry["stages"] = [
        {key: stage.get(key) for key in _STAGE}
        for stage in attempt.get("stages") or ()
        if isinstance(stage, Mapping)
    ][:12]
    try:
        kept = read_hosts(base)
        key = (entry["host"], entry.get("commit"), entry.get("started_at"))
        if any(
            (item.get("host"), item.get("commit"), item.get("started_at")) == key
            for item in kept
        ):
            return False
        kept = [*kept, entry][-MAX_HOSTS:]
        path = hosts_path(base)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = path.with_suffix(".json.partial")
        partial.write_text(
            json.dumps({"schema": SCHEMA, "attempts": kept}, sort_keys=True) + "\n"
        )
        os.replace(partial, path)
    except OSError:
        return False
    return True
