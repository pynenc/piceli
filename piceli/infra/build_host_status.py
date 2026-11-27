"""``piceli builder status``: what each build host last did (0.28.0).

Read from the GitOps controller's published status only:

- ``controller.build_hosts``: each host's last sighting (its Piceli
  version, when, what it sent) and the last build it claimed or answered;
- ``controller.builds_wanted``: the claims it holds now (``claimed``, a live
  claim) or held until it went quiet (``claim_expired``, see
  :func:`piceli.infra.remote_builds.live_claim`), and its progress reports;
- ``controller.build_health``: its quarantine and health records;
- ``controller.version`` (0.33.0): a host whose last sighting shows another
  Piceli version is ``version-mismatch`` (it claims nothing).

The declared hosts (``DevBuilds(hosts=[...])``) that never showed up are
listed too. :func:`liveness` (0.89.0, ``piceli gitops hosts``) says, per
host, whether the in-cluster fallback waits for it, and why not. Importing
this module is side-effect free.
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
    controller_version = controller.get("version")
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
        mismatch = (
            bool(sighting.get("version"))
            and isinstance(controller_version, str)
            and controller_version not in ("", "unknown")
            and sighting.get("version") != controller_version
        )
        if last_seen is None:
            state = "never-seen"
        elif quarantined:
            state = "quarantined"
        elif mismatch and claim is None:
            state = "version-mismatch"
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
                "version_mismatch": mismatch,
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
        "controller_version": controller_version,
        "hosts": rows,
        "never_seen": [row["host"] for row in rows if row["state"] == "never-seen"],
    }


def liveness(
    controller: Mapping[str, Any], now: float | None = None
) -> dict[str, dict[str, Any]]:
    """0.89.0: per build host the controller heard from or declares, whether
    its in-cluster fallback counts it (``waits``) and its ``state``:

    - ``busy``: it holds a live claim (``builds``);
    - ``live``: heard from (a lease, a claim, a report) within
      :data:`~piceli.infra.remote_builds.CLAIM_STALE_SECONDS`;
    - ``upgrading``: it runs another Piceli version and restarts on the
      controller's (within
      :data:`~piceli.infra.remote_builds.UPGRADE_GRACE_SECONDS` of its report);
    - ``stale``: not heard from for ``CLAIM_STALE_SECONDS``: taken for gone;
    - ``never-seen``: declared, never heard from.

    ``now`` defaults to the controller's last poll (its own clock)."""
    from piceli.infra.remote_builds import (
        CLAIM_STALE_SECONDS,
        UPGRADE_GRACE_SECONDS,
        _epoch,
    )

    if now is None:
        last = _latest(controller.get("last_poll"), controller.get("heartbeat_at"))
        now = _epoch(last)
    if now is None:
        import time

        now = time.time()
    sightings = _dict(controller.get("build_hosts"))
    declared = [str(h) for h in controller.get("build_hosts_declared") or ()]
    busy: dict[str, list[str]] = {}
    for entry in controller.get("builds_wanted") or ():
        held = _dict(_dict(entry).get("claimed"))
        if held.get("host"):
            busy.setdefault(str(held["host"]), []).append(str(_dict(entry).get("id")))
    found: dict[str, dict[str, Any]] = {}
    for name in [*declared, *sorted(set(sightings) - set(declared))]:
        sighting = _dict(sightings.get(name))
        seen = _epoch(sighting.get("seen_at"))
        upgrading = _dict(sighting.get("upgrading"))
        since = _epoch(upgrading.get("since"))
        item: dict[str, Any] = {
            "last_seen": sighting.get("seen_at"),
            "seen": sighting.get("seen"),
            "age_seconds": None if seen is None else max(0, int(now - seen)),
        }
        if name in busy:
            item.update(state="busy", waits=True, builds=busy[name])
        elif seen is not None and now - seen < CLAIM_STALE_SECONDS:
            item.update(state="live", waits=True)
        elif upgrading and since is not None and now - since < UPGRADE_GRACE_SECONDS:
            item.update(
                state="upgrading",
                waits=True,
                upgrading=upgrading,
                until=_iso_at(since + UPGRADE_GRACE_SECONDS),
            )
        elif seen is None:
            item.update(state="never-seen", waits=False)
        else:
            item.update(state="stale", waits=False)
        found[name] = item
    return found


def _iso_at(seconds: float) -> str:
    from piceli.infra.remote_builds import _iso

    return _iso(seconds)


__all__ = ["SCHEMA", "liveness", "view"]
