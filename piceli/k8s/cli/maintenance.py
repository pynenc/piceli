"""``piceli cache``, ``piceli doctor`` and ``piceli runs``: runner hygiene.

Each takes the pipeline (``MODULE:ATTR``, optional ``--env``) or a state
directory (``--state-dir``; default ``./.piceli-deploy``, the pipeline
default). None of them contacts a cluster. Output follows the CLI contract:
one JSON object on stdout (with ``--json``, or always for ``cache prune``),
human text on stderr.

Importing this module is side-effect free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import EXIT_FAILED, emit_json, reject, say

app = typer.Typer(
    rich_markup_mode=None,
    help=(
        "Disk used by Piceli on this runner: build caches, receipts, run "
        "state and temporary directories (status and a safe prune)."
    ),
    no_args_is_help=True,
)

DEFAULT_STATE_DIR = Path(".piceli-deploy")

TargetArgument = Annotated[
    str | None,
    typer.Argument(
        help="The pipeline, MODULE:ATTR or path/to/file.py:ATTR (default: the "
        "state directory --state-dir)",
        show_default=False,
    ),
]
EnvOption = Annotated[
    str | None,
    typer.Option(
        "--env",
        help="Only this environment's state (default: every environment)",
        show_default=False,
    ),
]
StateDirOption = Annotated[
    Path | None,
    typer.Option(
        "--state-dir",
        help="A pipeline state directory, instead of a pipeline "
        "(default: ./.piceli-deploy)",
        show_default=False,
    ),
]
JsonOption = Annotated[
    bool, typer.Option("--json", help="Print one JSON object on stdout")
]


def _pipeline(target: str, env: str | None) -> Any:
    """Load ``MODULE:ATTR`` (every environment unless ``env`` selects one)."""
    from piceli.app.render import RenderError, load_target
    from piceli.pipeline import Pipeline, PipelineError

    try:
        value = load_target(target, Path.cwd())
    except PipelineError as error:
        say(f"the pipeline module refused its declaration: {error}")
        reject(error.code)
    except RenderError as error:
        say(str(error))
        reject("pipeline-not-found")
    except Exception as error:
        say(f"importing {target} failed: {type(error).__name__}: {error}")
        reject("pipeline-load-failed")
    if not isinstance(value, Pipeline):
        say(f"{target} is a {type(value).__name__}, not a piceli Pipeline")
        reject("pipeline-not-found")
    if env is not None:
        try:
            return value.for_environment(env)
        except PipelineError as error:
            reject(error.code, str(error))
    return value


def _selected(
    target: str | None, env: str | None, state_dir: Path | None
) -> tuple[Any, list[Any]]:
    """``(pipeline or None, state directories)`` of the command's arguments."""
    from piceli.maintenance.cache import state_dirs

    if target is not None and state_dir is not None:
        reject("cache-arguments-conflict", "name the pipeline or --state-dir, not both")
    if target is None and env is not None:
        reject("cache-arguments-conflict", "--env needs the pipeline (MODULE:ATTR)")
    if target is not None:
        pipeline = _pipeline(target, env)
        return pipeline, state_dirs(pipeline)
    return None, state_dirs(state_dir=(state_dir or DEFAULT_STATE_DIR).absolute())


def _budget(value: str | None) -> int | None:
    from piceli.maintenance.cache import CacheError, parse_size

    if value is None:
        return None
    try:
        return parse_size(value)
    except CacheError as error:
        reject(error.code, str(error))


@app.command("status")
def cache_status(
    target: TargetArgument = None,
    env: EnvOption = None,
    state_dir: StateDirOption = None,
    keep_last: Annotated[
        int,
        typer.Option(
            "--keep-last",
            min=1,
            help="Runs a prune keeps (for reclaimable_bytes)",
        ),
    ] = 10,
    as_json: JsonOption = False,
) -> None:
    """Show the disk used per state directory and category, and Piceli's
    temporary directories. Read-only."""
    from piceli.maintenance.cache import format_size, status

    _pipeline_value, dirs = _selected(target, env, state_dir)
    report = status(dirs, keep_last=keep_last)
    for item in report["state_dirs"]:
        where = item["environment"] or item["pipeline"] or item["path"]
        say(
            f"{where}: {format_size(item['bytes'])}"
            + (
                f" (budget {format_size(item['budget']['bytes'])}"
                + (", OVER" if item["budget"]["over"] else "")
                + ")"
                if "budget" in item
                else ""
            )
            + f", {item['runs']['count']} run(s), "
            f"{format_size(item['reclaimable_bytes'])} reclaimable"
        )
        for name, usage in item["categories"].items():
            if usage["files"]:
                say(
                    f"  {name:<10} {format_size(usage['bytes']):>10}  {usage['files']} file(s)"
                )
    temp = report["temp"]
    say(
        f"temporary directories: {temp['entries']} ({format_size(temp['bytes'])}), "
        f"{temp['stale']} stale ({format_size(temp['stale_bytes'])})"
    )
    if as_json:
        emit_json(report)


@app.command("prune")
def cache_prune(
    target: TargetArgument = None,
    env: EnvOption = None,
    state_dir: StateDirOption = None,
    keep_last: Annotated[
        int,
        typer.Option("--keep-last", min=1, help="Runs to keep (at least 1)"),
    ] = 10,
    budget: Annotated[
        str | None,
        typer.Option(
            "--budget",
            help="Also free space until each state directory fits, e.g. 20GiB "
            "(default: the pipeline's cache_budget)",
            show_default=False,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="List what would be removed; remove nothing"),
    ] = False,
) -> None:
    """Remove what no release, rollback or resume needs: stale temporary
    directories and partial files, runs beyond --keep-last, unused delivery
    receipts, and (over --budget) build outputs and logs.

    Never removes the release state (catalog, execution journal, secret
    store, approved plans, backups), build receipts, the latest run, a
    resumable run, or the runs of the last --keep-last applied releases.
    Holds each state directory's run lock.
    """
    from piceli.maintenance.cache import PruneOptions, format_size, prune
    from piceli.state.errors import StateError

    _pipeline_value, dirs = _selected(target, env, state_dir)
    options = PruneOptions(keep_last=keep_last, budget=_budget(budget), dry_run=dry_run)
    try:
        report = prune(dirs, options)
    except StateError as error:
        reject(error.code, str(error))
    verb = "would free" if dry_run else "freed"
    for item in report["state_dirs"]:
        where = item["environment"] or item["pipeline"] or item["path"]
        say(
            f"{where}: {verb} {format_size(item['freed_bytes'])} "
            f"({len(item['removed'])} item(s)), "
            f"{format_size(item['bytes_after'])} left"
        )
        for entry in item["removed"]:
            say(f"  {entry['why']:<18} {entry['path']} ({format_size(entry['bytes'])})")
        if item.get("note"):
            say(f"  note: {item['note']}")
    temp = report["temp"]
    if temp["removed"]:
        say(
            f"temporary directories: {verb} {format_size(temp['freed_bytes'])} "
            f"({len(temp['removed'])} stale)"
        )
    if not report["within_budget"]:
        from piceli.cli_contract import fail

        fail(
            "cache-over-budget",
            "a state directory is still over its budget after pruning",
            hints=("see what is left: piceli cache status",),
            **{key: value for key, value in report.items() if key != "state"},
        )
    emit_json(report)


def doctor(
    target: TargetArgument = None,
    env: EnvOption = None,
    state_dir: StateDirOption = None,
    as_json: JsonOption = False,
) -> None:
    """Check this runner: free disk and memory against what the next build
    needs (estimated from the last build receipts), and the tools the
    pipeline uses (docker, docker buildx, kubectl). Exit 1 on a warning.

    Read-only; never contacts a cluster.
    """
    from piceli.maintenance.doctor import diagnose

    pipeline, dirs = _selected(target, env, state_dir)
    report = diagnose(dirs, pipeline=pipeline)
    for check in report["checks"]:
        say(f"{check['status']:<8} {check['check']}: {check.get('detail', '')}")
    if as_json:
        emit_json(report)
    if report["state"] != "ok" and report.get("reason"):
        say(f"next: piceli explain {report['reason']}")
        raise typer.Exit(EXIT_FAILED)


def runs(
    target: TargetArgument = None,
    env: EnvOption = None,
    state_dir: StateDirOption = None,
    limit: Annotated[
        int, typer.Option("--limit", min=1, help="Newest runs to list")
    ] = 20,
    as_json: JsonOption = False,
) -> None:
    """List the deploy runs of a pipeline, newest first, with their state,
    release, duration and summary files. Read-only (with shared state it
    reads the local working copy: run `piceli state pull` first)."""
    from piceli.pipeline.journal import Journal
    from piceli.pipeline.summary import read, summary_paths

    _pipeline_value, dirs = _selected(target, env, state_dir)
    listed: list[dict[str, Any]] = []
    for item in dirs:
        for run in Journal(item.path).runs():
            data = run.data
            stages = data.get("stages") or {}
            seconds = sum(
                float(value.get("seconds") or 0)
                for value in stages.values()
                if isinstance(value, dict)
            )
            apply = (stages.get("apply") or {}).get("output") or {}
            plan = (stages.get("plan") or {}).get("output") or {}
            entry: dict[str, Any] = {
                "run_id": run.run_id,
                "state": run.state,
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "until": data.get("until"),
                "release": apply.get("release") or plan.get("release"),
                "stages": {
                    name: value.get("state")
                    for name, value in stages.items()
                    if isinstance(value, dict)
                },
                "seconds": round(seconds, 3),
                "environment": item.environment,
            }
            if isinstance(data.get("reason"), str):
                entry["reason"] = data["reason"]
            summary = read(run.path)
            if summary is not None:
                json_path, markdown_path = summary_paths(run.path)
                entry["summary"] = {
                    "json": str(json_path),
                    "markdown": str(markdown_path),
                }
            listed.append(entry)
    listed.sort(key=lambda entry: entry["run_id"], reverse=True)
    listed = listed[:limit]
    for entry in listed:
        where = f" [{entry['environment']}]" if entry["environment"] else ""
        reason = f" ({entry['reason']})" if entry.get("reason") else ""
        say(
            f"{entry['run_id']}{where} {entry['state']}{reason} "
            f"release {entry['release'] or '-'} {entry['seconds']:.1f}s"
        )
    if not listed:
        say("no deploy runs")
    if as_json:
        emit_json({"schema": "piceli.runs.v1", "state": "listed", "runs": listed})


def register(root: typer.Typer) -> None:
    """Add ``cache``, ``doctor`` and ``runs`` to the root application."""
    root.add_typer(app, name="cache")
    root.command("doctor")(doctor)
    root.command("runs")(runs)
