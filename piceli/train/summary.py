"""The short account of a finished train run (``piceli.train.summary.v1``).

Notifications, the ``piceli.dev.train.finished`` OpenTelemetry event and
``piceli train-wait`` all carry this object, built from the run's result
(``piceli.train.v1``) by :func:`summarize`:

.. code-block:: json

    {
      "schema": "piceli.train.summary.v1",
      "train": "shop",
      "run": "20261023T101500Z-3f2a1c",
      "state": "ejected",
      "ok": false,
      "joined": ["feat-a"],
      "included": [],
      "ejected": [{"branch": "fix-c", "step": "fitness", "reason": "train-step-failed"}],
      "promoted": true,
      "tag": "tested/2026-10-23.1",
      "pushed": false,
      "promote": "promoted",
      "push": "not-requested",
      "head": "<commit>",
      "duration": 412.7,
      "flaky": [{"key": "-p api --test http retries_once", "count": 3}],
      "record": ".../runs/<run>/run.json"
    }

It holds names, commits, step names and fixed reason codes only: no step
output, so it can go to a webhook. Importing this module is side-effect free.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SCHEMA = "piceli.train.summary.v1"


def summarize(result: Mapping[str, Any]) -> dict[str, Any]:
    """The summary of a run result (also of a record still ``running``)."""
    branches = [b for b in result.get("branches") or () if isinstance(b, Mapping)]

    def named(verdict: str) -> list[str]:
        return [str(b.get("branch")) for b in branches if b.get("verdict") == verdict]

    promote = result.get("promote") or {}
    push = result.get("push") or {}
    return {
        "schema": SCHEMA,
        "train": result.get("train"),
        "run": result.get("run"),
        "state": result.get("state"),
        "ok": result.get("ok"),
        "joined": named("joined"),
        "included": named("included"),
        "ejected": [
            {
                "branch": b.get("branch"),
                "step": b.get("step"),
                "reason": b.get("reason"),
            }
            for b in branches
            if b.get("verdict") == "ejected"
        ],
        "promoted": promote.get("state") in {"promoted", "publish-failed"},
        "tag": promote.get("tag"),
        "pushed": push.get("state") == "pushed",
        "promote": promote.get("state", "not-requested"),
        "push": push.get("state", "not-requested"),
        "head": result.get("head"),
        "duration": result.get("duration"),
        "flaky": [
            {"key": item.get("key"), "count": item.get("count", 1)}
            for item in result.get("flaky") or ()
            if isinstance(item, Mapping)
        ],
        "record": result.get("record"),
    }


def headline(summary: Mapping[str, Any]) -> str:
    """One line for a person: ``train shop: 2 joined, fix-c ejected (fitness);
    promoted tested/2026-10-23.1, pushed; 6.9 min``."""
    parts: list[str] = []
    joined = summary.get("joined") or []
    if joined:
        parts.append(
            f"{len(joined)} joined" if len(joined) > 2 else " ".join(joined) + " joined"
        )
    for item in summary.get("ejected") or []:
        parts.append(f"{item.get('branch')} ejected ({item.get('step')})")
    if not parts:
        parts.append("nothing to join")
    tail: list[str] = []
    if summary.get("promoted"):
        tail.append(f"promoted {summary.get('tag')}")
    elif summary.get("promote") == "rejected":
        tail.append("not promoted")
    if summary.get("pushed"):
        tail.append("pushed")
    elif summary.get("push") == "failed":
        tail.append("push failed")
    flaky = summary.get("flaky") or []
    if flaky:
        tail.append(f"{len(flaky)} flaky")
    duration = summary.get("duration")
    if isinstance(duration, int | float):
        tail.append(seconds(duration))
    text = f"train {summary.get('train')}: " + ", ".join(parts)
    return text + ("; " + ", ".join(tail) if tail else "")


def seconds(value: float | None) -> str:
    """``41 s``, ``6.9 min``, ``2.1 h``."""
    if value is None:
        return "-"
    if value < 90:
        return f"{value:.1f} s" if value < 10 else f"{value:.0f} s"
    if value < 5400:
        return f"{value / 60:.1f} min"
    return f"{value / 3600:.1f} h"
