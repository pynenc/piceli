"""The controller's deployment history on the command line (0.38.0).

- ``piceli gitops history [ENV] [--limit N]``: each environment's deploy
  runs, newest first: outcome, trigger, commits, digests per component,
  checks, stage and cycle times, and the run's and release's time windows;
- ``piceli gitops show RUN [--logs]``: one run (its ``id`` or ``run_id``, or
  a unique prefix), with the log tail the controller kept on ``--logs``;
- ``piceli gitops compare ENV [A] [B]``: two runs, by default the running
  release and the one before it: source commits, components whose image
  changed, objects added, removed and changed, and both time windows.

They read the document the UI's Deployment history reads
(``piceli.gitops-history.v1``, :mod:`piceli.gitops.releases`), as ``gitops
status`` reads its status: ``--kubeconfig``/``--context`` or ``--state-dir``.
Read-only. ``--json`` prints one object on stdout; text goes to stderr.

Importing this module is side-effect free.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

import typer

from piceli.cli_contract import emit_json, say
from piceli.k8s.cli.gitops import (
    ContextOption,
    JsonOption,
    KubeconfigOption,
    NamespaceOption,
    StateDirOption,
    TransportOption,
    _channel,
    _guard,
    app,
)


def _read(
    state_dir: Any, kubeconfig: Any, context: Any, namespace: str, transport: str
) -> Any:
    with _channel(state_dir, kubeconfig, context, namespace, transport) as channel:
        with _guard():
            return channel.read_history()


def _short(value: Any, size: int = 12) -> str:
    return str(value)[:size] if value else "-"


def _seconds(value: Any) -> str:
    if not isinstance(value, int | float):
        return "?"
    minutes, seconds = divmod(round(value), 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def _outcome(run: Mapping[str, Any]) -> str:
    state = str(run.get("state") or "unknown")
    if run.get("kind", "run") != "run":
        return f"{run.get('kind')} ({state})"
    if state in {"deployed", "degraded"}:
        rolled = ", ".join(run.get("rolled") or ()) or "nothing"
        verb = "verified" if run.get("action") == "verified" else state
        return f"{verb}; rolled {rolled}"
    reason = run.get("reason") or (run.get("failure") or {}).get("reason")
    return f"{state} ({reason})" if reason else state


def _say_run(run: Mapping[str, Any], *, detail: bool = False) -> None:
    window = run.get("window") or {}
    say(
        f"  {run.get('id')}  {_outcome(run)}  "
        f"{window.get('started_at') or '?'} .. {window.get('finished_at') or '?'} "
        f"({_seconds(window.get('seconds'))})"
    )
    if run.get("trigger"):
        say(f"    trigger {run['trigger']}")
    sources = run.get("sources") or {}
    if sources:
        say(
            "    sources "
            + ", ".join(
                f"{name}@{_short((item or {}).get('commit'))}"
                for name, item in sorted(sources.items())
            )
        )
    components = run.get("components") or ()
    if components:
        say(
            "    components "
            + ", ".join(
                f"{item.get('name')} {_short(item.get('digest'), 19)}"
                + (" built" if item.get("built") else "")
                for item in components
            )
        )
    checks = run.get("checks") or {}
    results = checks.get("results") or ()
    if results:
        passed = sum(1 for item in results if item.get("passed"))
        say(f"    checks {passed}/{len(results)} passed")
    if detail:
        for name, stage in (run.get("stages") or {}).items():
            say(
                f"    stage {name}: {stage.get('state')} "
                f"{_seconds(stage.get('seconds'))}"
            )
        cycle = run.get("cycle") or {}
        for name, value in (cycle.get("phases") or {}).items():
            say(f"    cycle {name}: {_seconds(value)}")
        failure = run.get("failure") or {}
        if failure.get("reason") or failure.get("stage"):
            # 0.41.0: no "in stage None" when the stage is not known.
            say(
                f"    failure {failure.get('reason') or '?'}"
                + (f" in stage {failure['stage']}" if failure.get("stage") else "")
                + (f" on build host {failure['host']}" if failure.get("host") else "")
                + (f": {failure['message']}" if failure.get("message") else "")
            )
        if run.get("stopped") == "same-failure":
            say(
                "    not retried: the same failure as the attempt before, on the "
                "same inputs (a new revision or piceli gitops sync tries again)"
            )


@app.command("history")
def history(
    env: Annotated[
        str | None, typer.Argument(help="One environment (default: all)")
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", min=1, help="Runs per environment (newest)")
    ] = 20,
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    namespace: NamespaceOption = "piceli-system",
    state_dir: StateDirOption = None,
    transport: TransportOption = "https",
    as_json: JsonOption = False,
) -> None:
    """Each environment's deploy runs, newest first, with their time windows."""
    from piceli.gitops import releases

    document = _read(state_dir, kubeconfig, context, namespace, transport)
    with _guard():
        body = releases.history(document, env, limit)
    for item in body["environments"]:
        say(f"{item['name']}: {len(item['runs'])} run(s)")
        for run in item["runs"]:
            _say_run(run)
    if body["truncated"]:
        say("note: the controller dropped its oldest runs to fit the history")
    if as_json:
        emit_json(body)


@app.command("show")
def show(
    run: Annotated[str, typer.Argument(help="A run's id or run_id (or a prefix)")],
    env: Annotated[
        str | None, typer.Option("--env", help="Look in this environment only")
    ] = None,
    with_logs: Annotated[
        bool, typer.Option("--logs", help="Also print the log tail the run kept")
    ] = False,
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    namespace: NamespaceOption = "piceli-system",
    state_dir: StateDirOption = None,
    transport: TransportOption = "https",
    as_json: JsonOption = False,
) -> None:
    """One deploy run: outcome, commits, digests, checks, stages, failure."""
    from piceli.gitops import releases

    document = _read(state_dir, kubeconfig, context, namespace, transport)
    with _guard():
        body = releases.run(document, run, env, with_logs)
    say(f"{body['env']}:")
    _say_run(body["run"], detail=True)
    if with_logs:
        for item in body["logs"]:
            where = f" (stage {item['stage']})" if item.get("stage") else ""
            number = f" {item['attempt']}" if item.get("attempt") else ""
            say(
                f"--- {item['source']}{number} {item.get('reason') or ''}{where} "
                f"{item.get('at') or ''}"
            )
            say(item["text"].rstrip("\n"))
        if not body["logs"]:
            say("no log kept for this run")
        say(f"note: {body['logs_note']}")
    if as_json:
        emit_json(body)


@app.command("compare")
def compare(
    env: Annotated[str, typer.Argument(help="The environment")],
    before: Annotated[
        str | None,
        typer.Argument(
            help="The older run (default: the release before the running one)"
        ),
    ] = None,
    after: Annotated[
        str | None,
        typer.Argument(help="The newer run (default: the running release)"),
    ] = None,
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    namespace: NamespaceOption = "piceli-system",
    state_dir: StateDirOption = None,
    transport: TransportOption = "https",
    as_json: JsonOption = False,
) -> None:
    """Two runs side by side: commits, changed images, objects, time windows."""
    from piceli.gitops import releases

    document = _read(state_dir, kubeconfig, context, namespace, transport)
    with _guard():
        body = releases.compare(document, env, before, after)
    for side in ("before", "after"):
        item = body[side]
        window = item["release_window"]
        say(
            f"{side:6} {item['id']}  {item.get('state')}  release {item.get('release')}"
            f"  {window.get('started_at')} .. {window.get('finished_at')}"
        )
    for source in body["sources"]:
        if source["changed"]:
            say(
                f"  source {source['name']}: "
                f"{_short((source['before'] or {}).get('commit'))} -> "
                f"{_short((source['after'] or {}).get('commit'))}"
            )
    say("  images changed: " + (", ".join(body["images_changed"]) or "none"))
    resources = body["resources"]
    for key in ("added", "removed", "changed"):
        names = [f"{r['kind']}/{r['name']}" for r in resources[key]]
        say(f"  {key}: " + (", ".join(names) or "none"))
    if resources["partial"]:
        say("  note: a run listed only part of its changes (the history bounds them)")
    say(f"  note: {body['fields']['note']}")
    if as_json:
        emit_json(body)
