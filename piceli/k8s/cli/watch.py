"""``piceli watch``: follow a deploy run from its journal, for people and agents.

Read-only and offline: it reads the run journal of a pipeline's state
directory (the file ``piceli deploy`` rewrites at every stage change and on
every progress line), never the cluster. Run it from another terminal, or
after the fact. With ``--json`` stdout carries JSON lines
(``piceli.watch-event.v1``): one ``snapshot``, then one event per change
(``stage``, ``run``, ``progress``), then one ``result``. Human text goes to
stderr. Exit codes: ``0`` the run is ready or stopped as asked, ``1`` it
failed, was interrupted, was rolled back, or ``--timeout`` passed, ``2``
rejected (no run).

Importing this module is side-effect free.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import EXIT_FAILED, EXIT_OK, emit_json, fail, reject, say
from piceli.k8s.cli.maintenance import (
    EnvOption,
    StateDirOption,
    TargetArgument,
    _selected,
)

WATCH_SCHEMA = "piceli.watch-event.v1"
#: Run states after which nothing more happens until ``--resume``.
SETTLED = frozenset(
    {"ready", "stopped", "failed", "rejected", "rolled-back", "interrupted"}
)
#: Settled states that mean success.
SUCCEEDED = frozenset({"ready", "stopped"})


def _stage_states(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        name: value
        for name, value in (data.get("stages") or {}).items()
        if isinstance(value, dict)
    }


def snapshot(data: dict[str, Any]) -> dict[str, Any]:
    """The ``snapshot`` event: where the run is now."""
    stages = _stage_states(data)
    apply = (stages.get("apply") or {}).get("output") or {}
    plan = (stages.get("plan") or {}).get("output") or {}
    body: dict[str, Any] = {
        "schema": WATCH_SCHEMA,
        "event": "snapshot",
        "run_id": data.get("run_id"),
        "state": data.get("state"),
        "until": data.get("until"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "release": apply.get("release") or plan.get("release"),
        "stages": {name: value.get("state") for name, value in stages.items()},
    }
    if isinstance(data.get("reason"), str):
        body["reason"] = data["reason"]
    return body


def changes(before: dict[str, Any], after: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Events between two reads of the same run's journal, in order."""
    run_id = after.get("run_id")
    old_stages, new_stages = _stage_states(before), _stage_states(after)
    for name, value in new_stages.items():
        state = value.get("state")
        previous = (old_stages.get(name) or {}).get("state")
        if state == previous:
            continue
        event: dict[str, Any] = {
            "schema": WATCH_SCHEMA,
            "event": "stage",
            "run_id": run_id,
            "stage": name,
            "state": state,
            "previous": previous,
            "at": value.get("finished_at") or value.get("started_at"),
        }
        for key in ("seconds", "reason", "message"):
            if key in value:
                event[key] = value[key]
        yield event
    seen = max((item.get("n", 0) for item in before.get("progress") or []), default=0)
    for item in after.get("progress") or []:
        if item.get("n", 0) <= seen:
            continue
        yield {
            "schema": WATCH_SCHEMA,
            "event": "progress",
            "run_id": run_id,
            "stage": item.get("stage"),
            "line": item.get("line"),
            "at": item.get("at"),
        }
    if after.get("state") != before.get("state"):
        event = {
            "schema": WATCH_SCHEMA,
            "event": "run",
            "run_id": run_id,
            "state": after.get("state"),
            "previous": before.get("state"),
            "at": after.get("updated_at"),
        }
        if isinstance(after.get("reason"), str):
            event["reason"] = after["reason"]
        yield event


def result(data: dict[str, Any], run_path: Path | None) -> dict[str, Any]:
    """The final ``result`` event of a run that settled (or is read once)."""
    from piceli.pipeline.summary import summary_paths

    body = snapshot(data)
    body["event"] = "result"
    body["resumable"] = data.get("state") not in {"ready", "rolled-back", "stopped"}
    if run_path is not None:
        json_path, markdown_path = summary_paths(run_path)
        if json_path.is_file() and markdown_path.is_file():
            body["summary"] = {"json": str(json_path), "markdown": str(markdown_path)}
    return body


def _line(event: dict[str, Any]) -> str:
    kind = event["event"]
    if kind == "snapshot":
        stages = " ".join(f"{k}={v}" for k, v in event["stages"].items())
        return f"run {event['run_id']}: {event['state']} ({stages})"
    if kind == "stage":
        note = event.get("reason") or ""
        return f"[{event['stage']}] {event['state']}" + (f" ({note})" if note else "")
    if kind == "progress":
        return f"[{event['stage']}] {event['line']}"
    if kind == "run":
        return f"run {event['state']}"
    return f"watch: run {event['run_id']} {event['state']}"


def follow(
    read: Callable[[], dict[str, Any] | None],
    emit: Callable[[dict[str, Any]], None],
    *,
    interval: float,
    timeout: float | None,
    wait: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Emit the run's changes until it settles; return its last journal data.

    ``read`` returns the current journal document (``None`` while unreadable).
    Returns early, unsettled, when ``timeout`` passes or ``wait`` is false.
    """
    deadline = None if timeout is None else clock() + timeout
    current = read()
    while current is None:
        if not wait or (deadline is not None and clock() >= deadline):
            return {}
        sleep(interval)
        current = read()
    emit(snapshot(current))
    while wait and current.get("state") not in SETTLED:
        if deadline is not None and clock() >= deadline:
            break
        sleep(interval)
        latest = read()
        if latest is None:
            continue
        for event in changes(current, latest):
            emit(event)
        current = latest
    return current


def watch(
    target: TargetArgument = None,
    env: EnvOption = None,
    state_dir: StateDirOption = None,
    run: Annotated[
        str | None,
        typer.Option(
            "--run",
            help="Watch this run id (default: the newest run)",
            show_default=False,
        ),
    ] = None,
    once: Annotated[
        bool,
        typer.Option("--once", help="Print the run's current state and exit"),
    ] = False,
    interval: Annotated[
        float,
        typer.Option("--interval", min=0.05, max=60, help="Journal poll seconds"),
    ] = 0.5,
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout",
            min=0.0,
            help="Stop watching after this many seconds (watch-timeout, exit 1)",
            show_default=False,
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="Print JSON lines: snapshot, changes, then the result"
        ),
    ] = False,
) -> None:
    """Follow a deploy run until it settles: every stage change and progress
    line, then the outcome. Read-only; reads the local run journal (with shared
    state run `piceli state pull` first), never the cluster."""
    from piceli.pipeline.journal import Journal

    _pipeline_value, dirs = _selected(target, env, state_dir)
    candidates = [item for state in dirs for item in Journal(state.path).runs()]
    if run is not None:
        candidates = [item for item in candidates if item.run_id == run]
    candidates.sort(key=lambda item: item.run_id)
    if not candidates:
        reject(
            "watch-no-run",
            f"run {run} not found" if run else "no deploy run to watch",
        )
    chosen = candidates[-1]

    def read() -> dict[str, Any] | None:
        import json

        try:
            value = json.loads(chosen.path.read_text())
        except (OSError, ValueError):
            return None  # being replaced: read again on the next poll
        return value if isinstance(value, dict) else None

    def emit(event: dict[str, Any]) -> None:
        say(_line(event))
        if as_json:
            emit_json(event)

    last = follow(
        read,
        emit,
        interval=interval,
        timeout=timeout,
        wait=not once,
    )
    last = last or dict(chosen.data)
    final = result(last, chosen.path)
    if last.get("state") not in SETTLED and not once:
        say(_line(final))
        fail(
            "watch-timeout",
            "the run had not settled when --timeout passed",
            event="result",
            run_id=final["run_id"],
            run_state=final["state"],
            stages=final["stages"],
        )
    emit(final) if as_json else say(_line(final))
    if last.get("state") in SETTLED - SUCCEEDED:
        raise typer.Exit(EXIT_FAILED)
    raise typer.Exit(EXIT_OK)


def register(root: typer.Typer) -> None:
    """Add ``watch`` to the root application."""
    root.command("watch")(watch)
