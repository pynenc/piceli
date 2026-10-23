"""What the trains are doing and did (``piceli.trains.v1``).

``piceli trains`` and the UI's Trains view build the same document with
:func:`overview`, from each train's state directory
(:class:`~piceli.train.state.TrainState`) and nothing else:

- ``current``: the run holding the train's lock (the lock names its run and
  live process), its branches and verdicts so far, the trial and the step in
  progress with its start, elapsed time, host, log and the last lines of
  that log, read from ``runs/<id>/events.jsonl``;
- ``runs``: recent runs, newest first, with their verdicts and the seconds
  each step took (summed over the run's trials). A run whose record still
  says ``running`` while no process holds the lock was killed:
  ``interrupted``;
- ``trends``: per step, the median duration of its last N executions and
  the last one against it (``slower`` above 125 %, ``faster`` below 80 %);
- ``flaky``: each flaky test's count, first and last sighting and the
  output of its last first failure.

:func:`deployed` joins a ``piceli.targets.v1`` document with the runs that
produced what each target runs: a target's tag, commit or pinned product
commit matched to the promoted tag or head of a run.

Importing this module is side-effect free.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from piceli.train.state import TrainState, default_root, train_names

SCHEMA = "piceli.trains.v1"
#: Recent runs shown by default.
DEFAULT_RUNS = 10
#: Executions per step a trend looks at by default.
DEFAULT_TREND = 10
#: Lines of a running step's log shown.
TAIL_LINES = 20
SLOWER = 1.25
FASTER = 0.8


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def tail(path: Any, lines: int = TAIL_LINES) -> str:
    """The last ``lines`` lines of a log file ('' when unreadable)."""
    if not path:
        return ""
    try:
        with Path(str(path)).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 64 * 1024))
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.rstrip("\n").splitlines()[-lines:])


# ------------------------------------------------------------- the current run


def current(
    state: TrainState, *, now: float | None = None, lines: int = TAIL_LINES
) -> dict[str, Any] | None:
    """The run in progress, or ``None``."""
    owner = state.owner()
    if owner is None or not isinstance(owner.get("run"), str):
        return None
    run_id = owner["run"]
    now = time.time() if now is None else now
    events = state.events(run_id)
    started_at = next(
        (e.get("at") for e in events if e.get("event") == "run-started"), None
    )
    started = _epoch(started_at) or float(owner.get("since") or now)
    branches: list[str] = []
    verdicts: dict[str, str] = {}
    trial: dict[str, Any] | None = None
    step: dict[str, Any] | None = None
    done: list[dict[str, Any]] = []
    phase = "trials"
    for event in events:
        kind = event.get("event")
        if kind == "run-started":
            branches = [str(b) for b in event.get("branches") or ()]
            verdicts = dict.fromkeys(branches, "pending")
        elif kind == "trial-started":
            trial = {
                "id": event.get("trial"),
                "branches": list(event.get("branches") or ()),
                "base": event.get("base"),
                "started": event.get("at"),
            }
            done = []
        elif kind == "step-started":
            step = {
                "trial": event.get("trial"),
                "name": event.get("step"),
                "kind": event.get("kind"),
                "host": event.get("host"),
                "log": event.get("log"),
                "started": event.get("at"),
            }
            if event.get("trial") is None:
                phase = "publish"
        elif kind in {"step-finished", "published", "publish-failed"}:
            step = None
            done.append(
                {
                    "trial": event.get("trial"),
                    "name": event.get("step"),
                    "verdict": event.get("verdict"),
                    "duration": event.get("duration"),
                    "host": event.get("host"),
                }
            )
        elif kind == "trial-finished":
            trial = None
        elif kind in {"branch-joined", "branch-ejected", "branch-included"}:
            verdicts[str(event.get("branch"))] = str(kind).removeprefix("branch-")
        elif kind == "promoted":
            phase = "promote"
    if step is not None:
        begun = _epoch(step["started"]) or now
        step["elapsed"] = round(max(0.0, now - begun), 1)
        step["tail"] = tail(step.get("log"), lines)
    return {
        "run": run_id,
        "started": started_at,
        "elapsed": round(max(0.0, now - started), 1),
        "branches": branches,
        "verdicts": verdicts,
        "phase": phase,
        "trial": trial,
        "step": step,
        "done": done,
        "pid": owner.get("pid"),
    }


# ------------------------------------------------------------------- history


def _step_seconds(record: Mapping[str, Any]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for trial in record.get("trials") or ():
        for step in trial.get("steps") or ():
            name = str(step.get("name"))
            totals[name] = round(
                totals.get(name, 0.0) + float(step.get("duration") or 0), 3
            )
    for step in (record.get("promote") or {}).get("publish") or ():
        name = f"publish:{step.get('name')}"
        totals[name] = round(
            totals.get(name, 0.0) + float(step.get("duration") or 0), 3
        )
    return totals


def run_row(record: Mapping[str, Any], active: str | None) -> dict[str, Any]:
    """One run of the history."""
    state = record.get("state")
    if state == "running" and record.get("run") != active:
        state = "interrupted"
    promote = record.get("promote") or {}
    push = record.get("push") or {}
    return {
        "run": record.get("run"),
        "state": state,
        "ok": record.get("ok"),
        "started": record.get("started"),
        "duration": record.get("duration"),
        "host": record.get("host"),
        "head": record.get("head"),
        "branches": [
            {
                key: branch.get(key)
                for key in ("branch", "verdict", "step", "reason")
                if branch.get(key) is not None
            }
            for branch in record.get("branches") or ()
        ],
        "trials": len(record.get("trials") or ()),
        "steps": _step_seconds(record),
        "tag": promote.get("tag"),
        "promote": promote.get("state", "not-requested"),
        "push": push.get("state", "not-requested"),
        "flaky": len(record.get("flaky") or ()),
    }


def trends(
    records: Sequence[Mapping[str, Any]], samples: int = DEFAULT_TREND
) -> list[dict[str, Any]]:
    """Per step: the median of its last ``samples`` executions (newest
    records first) and the last against it. Skipped steps do not count."""
    seen: dict[str, list[tuple[float, str]]] = {}
    order: list[str] = []
    for record in records:
        trials = sorted(
            record.get("trials") or (), key=lambda t: t.get("id") or 0, reverse=True
        )
        for trial in trials:
            for step in trial.get("steps") or ():
                if step.get("verdict") == "skipped" or step.get("kind") in {
                    "merge",
                    "spec",
                }:
                    continue
                name = str(step.get("name"))
                if name not in seen:
                    seen[name] = []
                    order.append(name)
                if len(seen[name]) < samples:
                    seen[name].append(
                        (float(step.get("duration") or 0), str(record.get("run")))
                    )
    rows = []
    for name in order:
        values = seen[name]
        middle = median(value for value, _ in values)
        last, last_run = values[0]
        ratio = round(last / middle, 2) if middle > 0 else None
        trend = "steady"
        if ratio is not None and len(values) > 1:
            trend = (
                "slower" if ratio > SLOWER else "faster" if ratio < FASTER else "steady"
            )
        rows.append(
            {
                "step": name,
                "samples": len(values),
                "median": round(middle, 3),
                "last": round(last, 3),
                "last_run": last_run,
                "ratio": ratio,
                "trend": trend,
            }
        )
    return rows


def flaky_rows(state: TrainState, lines: int = 12) -> list[dict[str, Any]]:
    """Flaky tests, most frequent first."""
    rows: list[dict[str, Any]] = []
    for item in state.flaky().values():
        rows.append(
            {
                "key": item.get("key"),
                "test": item.get("test"),
                "target": item.get("target"),
                "step": item.get("step"),
                "count": int(item.get("count") or 0),
                "first_seen": item.get("first_seen"),
                "last_seen": item.get("last_seen"),
                "last_run": item.get("last_run"),
                "last_branches": item.get("last_branches") or [],
                "last_tail": "\n".join(
                    str(item.get("last_tail") or "").splitlines()[-lines:]
                ),
            }
        )
    rows.sort(key=lambda row: str(row["last_seen"] or ""), reverse=True)
    rows.sort(key=lambda row: int(row["count"]), reverse=True)
    return rows


def view(
    state: TrainState,
    *,
    runs: int = DEFAULT_RUNS,
    trend: int = DEFAULT_TREND,
    now: float | None = None,
    lines: int = TAIL_LINES,
) -> dict[str, Any]:
    """One train: ``current``, ``runs``, ``trends`` and ``flaky``."""
    live = current(state, now=now, lines=lines)
    history = state.history(limit=max(runs, trend))
    active = live["run"] if live else None
    return {
        "train": state.root.name,
        "state": "running" if live else "idle",
        "current": live,
        "runs": [run_row(record, active) for record in history[:runs]],
        "trends": trends(
            [
                r
                for r in history
                if r.get("run") != active and r.get("state") != "running"
            ],
            trend,
        ),
        "flaky": flaky_rows(state),
    }


def overview(
    root: Path | None = None,
    names: Iterable[str] | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Every train under ``root`` (or the ``names`` given)."""
    base = root if root is not None else default_root()
    chosen = list(names) if names is not None else train_names(base)
    return {
        "schema": SCHEMA,
        "trains": [view(TrainState(base / name), **options) for name in chosen],
        "deployed": None,
    }


# ------------------------------------------------------------ deployed where


def _promotions(
    histories: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
    by_tag: dict[str, dict[str, Any]] = {}
    by_head: list[tuple[str, dict[str, Any]]] = []
    for train, records in histories.items():
        for record in records:
            promote = record.get("promote") or {}
            if promote.get("state") not in {"promoted", "publish-failed"}:
                continue
            producer = {
                "train": train,
                "run": record.get("run"),
                "tag": promote.get("tag"),
                "head": promote.get("head") or record.get("head"),
                "started": record.get("started"),
                "joined": [
                    b.get("branch")
                    for b in record.get("branches") or ()
                    if b.get("verdict") == "joined"
                ],
                "pushed": (record.get("push") or {}).get("state") == "pushed",
            }
            if producer["tag"]:
                by_tag.setdefault(str(producer["tag"]), producer)
            if producer["head"]:
                by_head.append((str(producer["head"]), producer))
    return by_tag, by_head


def _same(commit: str, head: str) -> bool:
    return len(commit) >= 7 and (head.startswith(commit) or commit.startswith(head))


def _commits(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    found = [str(value["commit"])] if value.get("commit") else []
    commits = value.get("commits")
    if isinstance(commits, Mapping):
        found += [str(c) for c in commits.values() if c]
    return found


def deployed(
    targets: Mapping[str, Any] | None,
    histories: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """Each target of a ``piceli.targets.v1`` document with the train run
    that produced it (``train``: ``None`` when no run matches) and how it
    matched (``tag``, ``commit``: the deployed commit is a promoted head,
    ``pinned``: the product commit the target pins is)."""
    if not targets:
        return []
    by_tag, by_head = _promotions(histories)
    rows = []
    for target in targets.get("targets") or ():
        revision = target.get("revision") or {}
        producer, how = None, None
        tag = revision.get("tag") if isinstance(revision, Mapping) else None
        if tag and str(tag) in by_tag:
            producer, how = by_tag[str(tag)], "tag"
        for label, commits in (
            ("commit", _commits(revision)),
            ("pinned", _commits(target.get("pinned"))),
        ):
            if producer is not None:
                break
            for commit in commits:
                match = next((p for head, p in by_head if _same(commit, head)), None)
                if match is not None:
                    producer, how = match, label
                    break
        rows.append(
            {
                "kind": target.get("kind"),
                "name": target.get("name"),
                "where": target.get("where"),
                "state": target.get("state"),
                "revision": {
                    "commit": revision.get("commit")
                    if isinstance(revision, Mapping)
                    else None,
                    "tag": tag,
                },
                "pinned": (target.get("pinned") or {}).get("commit")
                if isinstance(target.get("pinned"), Mapping)
                else None,
                "train": producer,
                "matched": how,
            }
        )
    return rows


def histories(
    root: Path | None = None, limit: int = 200
) -> dict[str, list[dict[str, Any]]]:
    """Every train's run records (newest first, at most ``limit`` each)."""
    base = root if root is not None else default_root()
    return {name: TrainState(base / name).history(limit) for name in train_names(base)}


# --------------------------------------------------------------- human text


def _clock(value: Any) -> str:
    """``10:16:02Z`` from an ISO time."""
    if not isinstance(value, str) or "T" not in value:
        return "-"
    return value.split("T", 1)[1][:8] + "Z"


def _short(value: Any) -> str:
    return str(value)[:12] if value else "-"


def table(document: Mapping[str, Any], *, logs: bool = True) -> list[str]:
    """The document as human lines (stderr)."""
    from piceli.train.summary import seconds

    out: list[str] = []
    if not document.get("trains"):
        out.append("no train has run yet (state under $XDG_STATE_HOME/piceli/train)")
    for train in document.get("trains") or ():
        live = train.get("current")
        if live:
            out.append(
                f"train {train['train']}: running {live['run']} for "
                f"{seconds(live['elapsed'])} ({live['phase']})"
            )
            verdicts = live.get("verdicts") or {}
            out.append(
                "  branches: "
                + " ".join(
                    f"{b}={verdicts.get(b, 'pending')}" for b in live["branches"]
                )
            )
            trial, step = live.get("trial"), live.get("step")
            if trial:
                out.append(f"  trial {trial['id']}: {' '.join(trial['branches'])}")
            for item in live.get("done") or ():
                out.append(
                    f"    {item['name']:<14} {item['verdict']:<8} "
                    f"{seconds(item.get('duration'))} on {item.get('host')}"
                )
            if step:
                out.append(
                    f"    {step['name']:<14} running  {seconds(step['elapsed'])} on "
                    f"{step.get('host')} (started {_clock(step.get('started'))})"
                )
                if logs and step.get("log"):
                    out.append(f"      log {step['log']}")
                for row in (step.get("tail") or "").splitlines()[-8:] if logs else ():
                    out.append(f"      | {row}")
        else:
            out.append(f"train {train['train']}: idle")
        if train.get("runs"):
            out.append("  recent runs:")
        for run in train.get("runs") or ():
            verdicts = ", ".join(
                f"{b['branch']} {b.get('verdict')}"
                + (f" ({b['step']})" if b.get("verdict") == "ejected" else "")
                for b in run.get("branches") or ()
            )
            steps = " · ".join(
                f"{name} {seconds(value)}"
                for name, value in (run.get("steps") or {}).items()
                if name not in {"merge", "spec"}
            )
            extra = f" {run['tag']}" if run.get("tag") else ""
            if run.get("push") == "pushed":
                extra += " pushed"
            out.append(
                f"    {run['run']} {run['state']!s:<11} {seconds(run.get('duration')):>8}"
                f"{extra}  {verdicts}"
            )
            if steps:
                out.append(f"      {steps}")
        if train.get("trends"):
            out.append("  step trends (median of the last runs, last against it):")
        for row in train.get("trends") or ():
            out.append(
                f"    {row['step']:<16} median {seconds(row['median']):>8}  last "
                f"{seconds(row['last']):>8}  {row['trend']} (n={row['samples']})"
            )
        if train.get("flaky"):
            out.append("  flaky tests:")
        for row in train.get("flaky") or ():
            out.append(
                f"    {row['count']:>3}x {row['key']} (last {row.get('last_seen')}, "
                f"run {row.get('last_run')})"
            )
    rows = document.get("deployed")
    if rows:
        out.append("deployed:")
        for row in rows:
            revision = row.get("revision") or {}
            what = revision.get("tag") or _short(revision.get("commit"))
            producer = row.get("train")
            by = (
                f"<- train {producer['train']} run {producer['run']} ({row['matched']})"
                if producer
                else "<- no train run"
            )
            out.append(
                f"  {row.get('kind')} {row.get('name')}: {what} {row.get('state')} {by}"
            )
    elif document.get("deployed_reason"):
        out.append(f"deployed: not read [{document['deployed_reason']}]")
    return out
