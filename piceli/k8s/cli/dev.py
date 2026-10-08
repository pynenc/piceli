"""``piceli dev …``: development builds on a cluster builder (0.18.0).

Human text (the run's log, progress) goes to stderr; with ``--json`` one
``piceli.dev-run.v1`` object goes to stdout. Exit codes: 0 the command
passed, 1 it failed, timed out, was cancelled or the run broke, 2 refused.
"""

from __future__ import annotations

import getpass
import os
import re
import socket
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import emit_json, reject, say
from piceli.k8s.cli.cluster import (
    JsonOption,
    TransportOption,
    connect,
    load_cluster,
)
from piceli.k8s.cli.cluster import (
    _guard as _cluster_guard,
)

app = typer.Typer(
    rich_markup_mode=None,
    help="Development builds: your build and test commands on the cluster's builder.",
)

ClusterOption = Annotated[
    str,
    typer.Option(
        "--cluster",
        envvar="PICELI_DEV_CLUSTER",
        help="MODULE:ATTR of the piceli.infra.Cluster with dev=DevBuilds(...) "
        "(or PICELI_DEV_CLUSTER)",
        show_default=False,
    ),
]
AllowExecOption = Annotated[
    bool,
    typer.Option("--allow-exec", help="Allow the profile's exec credential plugin"),
]
ExecSha256Option = Annotated[
    str | None,
    typer.Option("--exec-sha256", help="Pin the exec credential plugin's sha256"),
]
_LABEL_CHARS = re.compile(r"[^a-z0-9._-]+")


def requester_label(value: str | None) -> str:
    """The requester as a label value (``PICELI_DEV_REQUESTER``, else user@host)."""
    text = (
        value
        or os.environ.get("PICELI_DEV_REQUESTER")
        or (f"{getpass.getuser()}-{socket.gethostname().split('.')[0]}")
    )
    slug = _LABEL_CHARS.sub("-", text.lower()).strip("-._")[:63].strip("-._")
    return slug or "anonymous"


def run_summary(result: dict[str, Any]) -> str:
    durations = result.get("durations") or {}
    parts = [
        f"{name} {durations[name]:.0f}s"
        for name in ("queue", "transfer", "sync", "fetch", "build", "test")
        if name in durations
    ]
    cache = result.get("cache") or {}
    tests = result.get("tests") or {}
    line = (
        f"run {result.get('run')}: {result.get('state')}"
        + (
            f" (exit {result['exit_code']})"
            if result.get("exit_code") not in (None, 0)
            else ""
        )
        + f" in {durations.get('total', 0):.0f}s"
        + (f" [{', '.join(parts)}]" if parts else "")
    )
    if tests:
        line += f"; tests {tests.get('passed')} passed, {tests.get('failed')} failed"
    if cache.get("lineage"):
        line += (
            f"; lineage {cache['lineage']} ({'warm' if cache.get('warm') else 'cold'}"
        )
        if cache.get("crates_compiled") is not None:
            line += f", {cache['crates_compiled']} crates compiled"
        line += ")"
    if result.get("reason") and result.get("state") != "passed":
        line += f" ({result['reason']})"
    return line


@app.command("run")
def run(
    command: Annotated[
        list[str],
        typer.Argument(help="The command, after --", show_default=False),
    ],
    cluster: ClusterOption,
    ref: Annotated[
        str | None,
        typer.Option(
            "--ref", help="Build this commit (branch, tag or sha) of the repository"
        ),
    ] = None,
    worktree: Annotated[
        Path | None,
        typer.Option(
            "--worktree",
            help="Build this working tree as it is on disk (uncommitted and new files; "
            "not Git's ignored ones). The default without --ref: here",
        ),
    ] = None,
    repo: Annotated[
        Path,
        typer.Option(
            "--repo", help="The root source's repository with --ref (default: here)"
        ),
    ] = Path("."),
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            help="The root source's directory name (default: the repository's)",
        ),
    ] = None,
    cwd: Annotated[
        str | None,
        typer.Option(
            "--cwd",
            help="Where the command runs, inside the root source (default: your directory's place in it)",
        ),
    ] = None,
    source: Annotated[
        list[str] | None,
        typer.Option(
            "--source",
            help="A sibling source NAME=PATH@REF (repeat); it sits next to the root, as ../NAME",
        ),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="A DevProfile name (default: the first)"),
    ] = None,
    node: Annotated[
        str | None,
        typer.Option("--node", help="The builder node (must be the declared one)"),
    ] = None,
    priority: Annotated[
        str,
        typer.Option("--priority", help="round (integration rounds), agent or normal"),
    ] = "agent",
    requester: Annotated[
        str | None,
        typer.Option(
            "--requester",
            help="Who asks (fair share; default PICELI_DEV_REQUESTER or user-host)",
        ),
    ] = None,
    artifact: Annotated[
        list[str] | None,
        typer.Option(
            "--artifact",
            help="Copy back files matching this glob (target/... is the target dir; repeat)",
        ),
    ] = None,
    artifacts_dir: Annotated[
        Path, typer.Option("--artifacts-dir", help="Where artifacts land (under RUN/)")
    ] = Path("piceli-dev-artifacts"),
    queue_timeout: Annotated[
        int, typer.Option("--queue-timeout", help="Seconds to wait for a slot", min=1)
    ] = 3600,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            help="Do not stream the run's log (the tail is still in the result)",
        ),
    ] = False,
    keep: Annotated[
        bool, typer.Option("--keep", help="Keep the run's Job (debugging)")
    ] = False,
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
    as_json: JsonOption = False,
) -> None:
    """Run COMMAND on the builder for a commit (--ref) or a working tree (default: here)."""
    from piceli.dev.client import RunRequest
    from piceli.dev.client import run as run_on
    from piceli.dev.cluster import DevCluster
    from piceli.dev.model import DevError
    from piceli.dev.pack import parse_source, toplevel

    declared = load_cluster(cluster)
    try:
        if ref is not None and worktree is not None:
            raise DevError("dev-run-invalid", "give --ref or --worktree, not both")
        root_dir = toplevel(repo if ref is not None else (worktree or Path(".")))
        here = Path.cwd().resolve()
        where = cwd
        if where is None:
            where = (
                str(here.relative_to(root_dir.resolve()))
                if here.is_relative_to(root_dir.resolve())
                else "."
            )
        from piceli.dev.pack import SourceRequest

        request = RunRequest(
            root=SourceRequest(name or root_dir.name, root_dir, ref),
            command=list(command),
            extras=[parse_source(text) for text in source or ()],
            cwd=where,
            profile=profile,
            requester=requester_label(requester),
            priority=priority,
            artifacts=list(artifact or ()),
            artifacts_dir=artifacts_dir,
            queue_timeout=float(queue_timeout),
            keep=keep,
        )
    except DevError as error:
        reject(error.code, str(error))
    with connect(declared, transport, allow_exec, exec_sha256) as api, _cluster_guard():
        port = DevCluster(api)
        dev = port.config()
        if dev is None:
            reject(
                "dev-not-enabled",
                f"development builds are not installed on {declared.name}: declare "
                "Cluster(dev=DevBuilds(...)) and run piceli cluster init",
            )
        if node is not None and node != dev.node:
            reject(
                "dev-run-invalid", f"--node {node}: runs go to the builder {dev.node}"
            )
        try:
            result = run_on(
                port,
                dev,
                request,
                say=say,
                log=None if quiet else say,
            )
        except DevError as error:
            reject(error.code, str(error))
    say(run_summary(result))
    if result.get("state") != "passed" and quiet:
        for line in result.get("log_tail") or ():
            say(f"  | {line}")
    if as_json:
        emit_json(result)
    if result.get("state") != "passed":
        raise typer.Exit(1)


def register(root: typer.Typer) -> None:
    root.add_typer(app, name="dev")


def _port(
    cluster: str, transport: str, allow_exec: bool, exec_sha256: str | None
) -> Any:
    """A context manager yielding ``(DevCluster, DevBuilds)`` (refusal when not installed)."""
    from contextlib import contextmanager

    from piceli.dev.cluster import DevCluster

    declared = load_cluster(cluster)

    @contextmanager
    def opened() -> Any:
        with (
            connect(declared, transport, allow_exec, exec_sha256) as api,
            _cluster_guard(),
        ):
            port = DevCluster(api)
            dev = port.config()
            if dev is None:
                reject(
                    "dev-not-enabled",
                    f"development builds are not installed on {declared.name}",
                )
            yield port, dev

    return opened()


def queue_view(port: Any, dev: Any, now: float) -> dict[str, Any]:
    """Runs, queue and cache use from the live Jobs and the scheduler's status."""
    from piceli.dev.scheduler import (
        STATUS_SCHEMA,
        alive,
        auto_slots,
        order,
        run_summary,
        state_of,
    )

    jobs = port.jobs()
    status = port.status() or {}
    running = [job for job in jobs if state_of(job) == "running"]
    queued = [job for job in jobs if state_of(job) == "queued"]
    finished = [job for job in jobs if state_of(job) in {"complete", "failed"}]
    slots = status.get("slots") or auto_slots(
        dev, None if isinstance(dev.slots, int) else port.node(dev.node)
    )
    return {
        "schema": STATUS_SCHEMA,
        "node": dev.node,
        "slots": slots,
        "scheduler": {"alive": alive(status, now), "at": status.get("scheduler_at")},
        "running": [run_summary(job, now) for job in running],
        "queued": [
            {**run_summary(job, now), "position": index + 1}
            for index, job in enumerate(order(queued, running))
        ],
        "finishing": [run_summary(job, now) for job in finished],
        "recent": list(status.get("recent") or ()),
        "cache": dict(status.get("cache") or {}),
    }


@app.command("status")
def status(
    cluster: ClusterOption,
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
    as_json: JsonOption = False,
) -> None:
    """Slots, running and queued runs (with their position), recent runs and cache use."""
    import time

    with _port(cluster, transport, allow_exec, exec_sha256) as (port, dev):
        view = queue_view(port, dev, time.time())
    scheduler = view["scheduler"]
    say(
        f"dev builds on {view['node']}: {len(view['running'])}/{view['slots']} slots busy, "
        f"{len(view['queued'])} queued; scheduler "
        + ("alive" if scheduler["alive"] else "not running (runs start at once)")
    )
    for item in view["running"]:
        say(
            f"  running {item['run']} {item['requester']} ({item['priority']}, {item['profile']})"
        )
    for item in view["queued"]:
        say(
            f"  queued  #{item['position']} {item['run']} {item['requester']} ({item['priority']})"
        )
    for item in view["recent"][:10]:
        say(
            f"  done    {item.get('run')} {item.get('requester')}: {item.get('state')}"
            + (f" ({item['reason']})" if item.get("reason") else "")
        )
    cache = view["cache"]
    if cache.get("used_bytes") is not None:
        say(
            f"  cache   {cache['used_bytes'] / 2**30:.1f} GiB of "
            f"{(cache.get('max_bytes') or 0) / 2**30:.1f} GiB in lineages"
        )
    if as_json:
        emit_json(view)


@app.command("logs")
def logs(
    run_id: Annotated[str, typer.Argument(help="The run (from dev run or dev status)")],
    cluster: ClusterOption,
    follow: Annotated[
        bool, typer.Option("--follow", help="Stream until the run ends")
    ] = False,
    tail: Annotated[
        int | None, typer.Option("--tail", help="Only the last N lines", min=1)
    ] = None,
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
) -> None:
    """A run's output (stdout): live, or the recorded tail of a finished run."""
    from piceli.dev.pod import RESULT_MARKER

    with _port(cluster, transport, allow_exec, exec_sha256) as (port, _dev):
        pod = port.pod(run_id)
        if pod is not None and follow:
            port.follow(
                pod["metadata"]["name"],
                lambda line: (
                    None if line.startswith(RESULT_MARKER) else typer.echo(line)
                ),
            )
            return
        text = port.logs(run_id, tail) if pod is not None else None
        if text is not None:
            for line in text.splitlines():
                if not line.startswith(RESULT_MARKER):
                    typer.echo(line)
            return
        recorded = next(
            (
                item
                for item in (port.status() or {}).get("recent") or ()
                if item.get("run") == run_id
            ),
            None,
        )
    if recorded is None:
        reject("dev-run-unknown", f"no run {run_id} (piceli dev status lists them)")
    say(
        f"run {run_id} finished ({recorded.get('state')}); its pod is gone, the recorded tail:"
    )
    for line in recorded.get("log_tail") or ():
        typer.echo(line)


@app.command("cancel")
def cancel(
    run_id: Annotated[str, typer.Argument(help="The run to cancel")],
    cluster: ClusterOption,
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
    as_json: JsonOption = False,
) -> None:
    """Cancel a queued or running run (its Job and pod are deleted)."""
    with _port(cluster, transport, allow_exec, exec_sha256) as (port, _dev):
        if port.job(run_id) is None:
            reject("dev-run-unknown", f"no run {run_id} is queued or running")
        port.delete(run_id)
    say(f"run {run_id}: cancelled")
    if as_json:
        emit_json({"run": run_id, "state": "cancelled"})


@app.command("schedule")
def schedule(
    cluster: ClusterOption,
    once: Annotated[
        bool, typer.Option("--once", help="One tick, print the status, exit")
    ] = False,
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
) -> None:
    """Run the queue yourself (a cluster without the GitOps controller, which runs it)."""
    import time

    from piceli.dev.scheduler import TICK_SECONDS, Scheduler

    with _port(cluster, transport, allow_exec, exec_sha256) as (port, dev):
        scheduler = Scheduler(port, dev, say=say)
        if once:
            emit_json(scheduler.tick())
            return
        say(f"dev scheduler: queueing development runs on {dev.node} (Ctrl-C stops)")
        try:
            while True:
                scheduler.tick()
                time.sleep(TICK_SECONDS)
        except KeyboardInterrupt:
            return
