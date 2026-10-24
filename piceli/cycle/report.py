"""``piceli cycle report``: push to running-and-checked, per target.

:func:`collect` turns the records Piceli keeps into cycles
(:mod:`piceli.cycle.record`); :func:`report` groups them by target (an
environment, a Nix release, a Nix host, a train) and gives, for the cycles
that ended running and checked, the count, p50 and p90 of their time, the
slowest phase (the highest p50 by phase) and the slowest single cycle.
Percentiles are nearest-rank: p90 of ten cycles is the ninth fastest.

Importing this module is side-effect free.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from piceli.cycle import KINDS, REPORT_SCHEMA
from piceli.cycle.record import (
    environment_cycle,
    epoch,
    host_cycle,
    iso,
    release_cycle,
    train_cycle,
)

_SINCE = re.compile(r"(\d+(?:\.\d+)?)([smhdw])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_since(text: str, now: float) -> float:
    """``24h``, ``30m``, ``7d``, ``2w`` (before ``now``) or an ISO time, as
    seconds since the epoch. :raises ValueError: neither."""
    found = _SINCE.fullmatch(text.strip())
    if found:
        return now - float(found.group(1)) * _UNITS[found.group(2)]
    at = epoch(text.strip())
    if at is None:
        raise ValueError(f"--since {text!r}: a duration (24h, 30m, 7d) or an ISO time")
    return at


def percentile(values: Sequence[float], q: float) -> float | None:
    """The nearest-rank ``q`` percentile (0 < q <= 1) of ``values``."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def collect(
    *,
    history: Mapping[str, Any] | None = None,
    status: Mapping[str, Any] | None = None,
    trains: Iterable[Mapping[str, Any]] = (),
    hosts: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Every cycle the sources hold: the controller's history (or, without
    one, the last cycle of each environment in its status), its Nix
    releases, train run records and the host attempts ``host wait`` kept."""
    found: list[dict[str, Any]] = []
    releases: list[Mapping[str, Any]] = []
    if history:
        for env, body in sorted((history.get("envs") or {}).items()):
            for entry in (body or {}).get("runs") or ():
                if isinstance(entry, Mapping):
                    item = environment_cycle(str(env), entry)
                    if item is not None:
                        found.append(item)
        for name, body in sorted((history.get("nix_releases") or {}).items()):
            for run in (body or {}).get("runs") or ():
                if isinstance(run, Mapping):
                    releases.append(run)
                    found.append(release_cycle(str(name), run))
    elif status:
        for record in (status.get("envs") or {}).values():
            own = record.get("cycle") if isinstance(record, Mapping) else None
            if isinstance(own, Mapping) and own.get("kind") == "environment":
                found.append(dict(own))
    for record in trains:
        own = record.get("cycle")
        if isinstance(own, Mapping) and own.get("kind") == "train":
            found.append(dict(own))
        elif record.get("state") != "running":
            found.append(train_cycle(record))
    for entry in hosts:
        found.append(host_cycle(str(entry.get("host")), entry, releases))
    return found


def _when(item: Mapping[str, Any]) -> float | None:
    return epoch(item.get("checked_at") or item.get("seen_at"))


def report(
    cycles: Iterable[Mapping[str, Any]],
    *,
    now: float,
    since: float | None = None,
    target: str | None = None,
    sources: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``piceli.cycle-report.v1`` document of ``cycles``: those since
    ``since`` (by when they ended, else when they were seen), of ``target``."""
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for item in cycles:
        when = _when(item)
        if since is not None and (when is None or when < since):
            continue
        if target is not None and item.get("target") != target:
            continue
        key = (str(item.get("kind")), str(item.get("target")))
        groups.setdefault(key, []).append(item)
    order = {kind: index for index, kind in enumerate(KINDS)}
    targets = [
        _target(kind, name, items)
        for (kind, name), items in sorted(
            groups.items(), key=lambda pair: (order.get(pair[0][0], 99), pair[0][1])
        )
    ]
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": iso(now),
        "since": iso(since) if since is not None else None,
        "target": target,
        "sources": dict(sources or {}),
        "percentiles": "nearest-rank",
        "targets": targets,
    }


def _target(kind: str, name: str, items: list[Mapping[str, Any]]) -> dict[str, Any]:
    done = [
        i for i in items if i.get("ok") and isinstance(i.get("seconds"), int | float)
    ]
    totals = [float(i["seconds"]) for i in done]
    by_phase: dict[str, list[float]] = {}
    for item in done:
        for phase, seconds in (item.get("phases") or {}).items():
            by_phase.setdefault(str(phase), []).append(float(seconds))
    phases = {
        phase: percentile(values, 0.5) for phase, values in sorted(by_phase.items())
    }
    slowest_phase = max(
        phases.items(), key=lambda pair: (pair[1] or 0, pair[0]), default=None
    )
    worst = max(done, key=lambda i: float(i["seconds"]), default=None)
    seen = [i for i in done if i.get("from") != "pushed"]
    first_seen = min(
        (str(i["seen_at"]) for i in seen if i.get("seen_at")), default=None
    )
    return {
        "target": name,
        "kind": kind,
        "cycles": len(done),
        "failed": len(items) - len(done),
        "p50_seconds": percentile(totals, 0.5),
        "p90_seconds": percentile(totals, 0.9),
        "phases_p50": phases,
        "slowest_phase": {"name": slowest_phase[0], "p50_seconds": slowest_phase[1]}
        if slowest_phase
        else None,
        "slowest_cycle": {
            "commit": worst.get("commit"),
            "seconds": worst.get("seconds"),
            "seen_at": worst.get("seen_at"),
            "checked_at": worst.get("checked_at"),
            "slowest_phase": max(
                (worst.get("phases") or {}).items(),
                key=lambda pair: pair[1],
                default=(None, None),
            )[0],
        }
        if worst
        else None,
        # Cycles timed from when Piceli first saw the commit (push unknown).
        "push_unknown": len(seen),
        "first_seen_at": first_seen,
    }


# ------------------------------------------------------------------ output


def duration(seconds: float | None) -> str:
    """``42s``, ``4m 10s``, ``1h 05m``; ``-`` when unknown."""
    if seconds is None:
        return "-"
    if seconds < 10:
        return f"{seconds:.1f}s"
    whole = round(seconds)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m {whole % 60:02d}s"
    return f"{whole // 3600}h {(whole % 3600) // 60:02d}m"


def _phase(item: Mapping[str, Any]) -> str:
    slowest = item.get("slowest_phase")
    if not slowest:
        return "-"
    return f"{slowest['name']} ({duration(slowest['p50_seconds'])})"


def _cycle(item: Mapping[str, Any]) -> str:
    worst = item.get("slowest_cycle")
    if not worst:
        return "-"
    commit = str(worst.get("commit") or "")[:12]
    return f"{duration(worst.get('seconds'))}" + (f" ({commit})" if commit else "")


def notes(document: Mapping[str, Any]) -> list[str]:
    """What the report does not know, one line each."""
    found = []
    for item in document["targets"]:
        if item["push_unknown"]:
            found.append(
                f"{item['target']}: push time unknown for {item['push_unknown']} of "
                f"{item['cycles']} cycle(s); timed from when Piceli first saw the "
                f"commit (first at {item['first_seen_at']})"
            )
    for name, value in (document.get("sources") or {}).items():
        if isinstance(value, str):
            found.append(f"{name}: {value}")
    return found


def text(document: Mapping[str, Any]) -> list[str]:
    """Human lines (stderr)."""
    since = document.get("since")
    lines = [
        f"cycle report{f' since {since}' if since else ''}: "
        f"{len(document['targets'])} target(s), push to running and checked"
    ]
    for item in document["targets"]:
        failed = f", {item['failed']} failed" if item["failed"] else ""
        lines.append(
            f"  {item['kind']:<12} {item['target']:<20} {item['cycles']} cycle(s)"
            f"{failed}  p50 {duration(item['p50_seconds'])}  "
            f"p90 {duration(item['p90_seconds'])}  slowest phase {_phase(item)}  "
            f"slowest cycle {_cycle(item)}"
        )
    lines.extend(f"  note: {line}" for line in notes(document))
    return lines


def markdown(document: Mapping[str, Any]) -> str:
    """A table a project can paste into its docs (generated, not written by hand)."""
    since = document.get("since")
    rows = [
        f"<!-- generated by `piceli cycle report --markdown` at "
        f"{document['generated_at']}; not written by hand -->",
        "",
        "| Target | Kind | Cycles | p50 | p90 | Slowest phase (p50) | Slowest cycle |",
        "| --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for item in document["targets"]:
        failed = f" (+{item['failed']} failed)" if item["failed"] else ""
        rows.append(
            f"| {item['target']} | {item['kind']} | {item['cycles']}{failed} | "
            f"{duration(item['p50_seconds'])} | {duration(item['p90_seconds'])} | "
            f"{_phase(item)} | {_cycle(item)} |"
        )
    rows.append("")
    rows.append(
        "Push to running and checked"
        + (f", since {since}" if since else "")
        + "; percentiles are nearest-rank."
    )
    rows.extend(f"- {line}" for line in notes(document))
    return "\n".join(rows) + "\n"
