"""``piceli builder status``: what each build host last did (0.28.0).

Read from the GitOps controller's published status only:

- ``controller.build_hosts``: each host's last sighting (its Piceli
  version, when, what it sent) and the last build it claimed or answered;
- ``controller.builds_wanted``: the claims it holds now (``claimed``, a live
  claim) or held until it went quiet (``claim_expired``, see
  :func:`piceli.infra.remote_builds.live_claim`), and its progress reports;
- ``controller.build_health``: its quarantine and health records.

The declared hosts (``DevBuilds(hosts=[...])``) that never showed up are
listed too. Importing this module is side-effect free.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA = "piceli.builder-status.v1"


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _latest(*times: Any) -> str | None:
    """The latest of ISO times (``YYYY-MM-DDTHH:MM:SSZ`` sort as text)."""
    found = [str(item) for item in times if isinstance(item, str) and item]
    return max(found) if found else None


def view(status: Mapping[str, Any] | None, declared: Sequence[str]) -> dict[str, Any]:
    """``piceli.builder-status.v1``: one row per build host, declared ones
    first in their declared order, then the others by name."""
    controller = _dict(_dict(status).get("controller"))
    sightings = _dict(controller.get("build_hosts"))
    health = {
        name: _dict(entry)
        for name, entry in _dict(controller.get("build_health")).items()
        if _dict(entry).get("kind") != "node"
    }
    claims: dict[str, dict[str, Any]] = {}
    for entry in controller.get("builds_wanted") or ():
        entry = _dict(entry)
        progress = _dict(entry.get("progress"))
        for key, state in (("claimed", "live"), ("claim_expired", "expired")):
            held = _dict(entry.get(key))
            if not held.get("host"):
                continue
            claim = {
                "build": entry.get("id"),
                "state": state,
                "at": held.get("at"),
                "stage": progress.get("stage"),
            }
            current = claims.get(str(held["host"]))
            # A live claim wins over an expired one.
            if current is None or (current["state"], state) == ("expired", "live"):
                claims[str(held["host"])] = claim | {"_seen": progress.get("at")}
    names = [*declared]
    names += sorted({*sightings, *health, *claims} - set(declared))
    rows = []
    for name in names:
        sighting = _dict(sightings.get(name))
        record = health.get(name) or {}
        claim = claims.get(name)
        events = [_dict(item).get("at") for item in record.get("events") or ()]
        last_seen = _latest(
            sighting.get("seen_at"),
            *(events or ()),
            *((claim["at"], claim["_seen"]) if claim else ()),
        )
        quarantined = bool(record.get("quarantined"))
        if last_seen is None:
            state = "never-seen"
        elif quarantined:
            state = "quarantined"
        elif claim is not None:
            state = "building" if claim["state"] == "live" else "claim-expired"
        else:
            state = "idle"
        rows.append(
            {
                "host": name,
                "declared": name in declared,
                "state": state,
                "version": sighting.get("version"),
                "last_seen": last_seen,
                "seen": sighting.get("seen"),
                "last_build": _dict(sighting.get("last_build")) or None,
                "claim": {k: v for k, v in claim.items() if k != "_seen"}
                if claim
                else None,
                "quarantined": quarantined,
                "quarantine": {
                    "since": record.get("since"),
                    "reason": record.get("reason"),
                }
                if quarantined
                else None,
            }
        )
    return {
        "schema": SCHEMA,
        "hosts": rows,
        "never_seen": [row["host"] for row in rows if row["state"] == "never-seen"],
    }


__all__ = ["SCHEMA", "view"]
