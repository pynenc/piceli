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
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import EXIT_FAILED, emit_json, reject, say
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
    repaired = cache.get("repaired") or {}
    if repaired:
        line += f"; corrupted cache ({repaired.get('kind')}) repaired: " + ", ".join(
            repaired.get("crates") or ()
        )
    elif (cache.get("suspect") or {}).get("kind"):
        line += (
            f"; the cache looks corrupted ({cache['suspect']['kind']}): "
            "piceli dev cache clear --lineage NAME"
        )
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
            help="The root source's name and lineage (default: the repository's main checkout directory, shared by its worktrees)",
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
    host: Annotated[
        str | None,
        typer.Option(
            "--host",
            envvar="PICELI_DEV_HOST",
            help="Run on this declared build host only (default: the first that is "
            "up, with a free slot first; the next one if it turns out down)",
        ),
    ] = None,
    platform: Annotated[
        str | None,
        typer.Option(
            "--platform",
            help="linux/amd64 or linux/arm64 (default: the host's own; a docker "
            "host runs another one emulated, slower)",
        ),
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
    unschedulable_seconds: Annotated[
        int,
        typer.Option(
            "--unschedulable-seconds",
            help="Fail when the run's pod finds no node for this long, with the "
            "scheduler's reason (0.35.0)",
            min=1,
        ),
    ] = 60,
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
    from piceli.dev.pack import parse_source, repository_name, toplevel

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
            root=SourceRequest(name or repository_name(root_dir), root_dir, ref),
            command=list(command),
            extras=[parse_source(text) for text in source or ()],
            cwd=where,
            profile=profile,
            requester=requester_label(requester),
            priority=priority,
            artifacts=list(artifact or ()),
            artifacts_dir=artifacts_dir,
            queue_timeout=float(queue_timeout),
            unschedulable_timeout=float(unschedulable_seconds),
            keep=keep,
        )
    except DevError as error:
        reject(error.code, str(error))
    dev = declared.dev
    if dev is None:
        # No local declaration: the cluster's installed one, on its builder
        # node only (as before 0.20).
        with (
            connect(declared, transport, allow_exec, exec_sha256) as api,
            _cluster_guard(),
        ):
            port = DevCluster(api)
            installed = port.config()
            if installed is None:
                reject(
                    "dev-not-enabled",
                    f"development builds are not installed on {declared.name}: "
                    "declare Cluster(dev=DevBuilds(...)) and run piceli cluster init",
                )
            if node is not None and node != installed.node:
                reject(
                    "dev-run-invalid",
                    f"--node {node}: runs go to the builder {installed.node}",
                )
            try:
                result: dict[str, Any] = run_on(
                    port, installed, request, say=say, log=None if quiet else say
                )
            except DevError as error:
                reject(error.code, str(error))
        _finish_run(result, quiet, as_json)
        return
    if node is not None and node != dev.node:
        reject("dev-run-invalid", f"--node {node}: runs go to the builder {dev.node}")
    from piceli.dev.hosts import UNAVAILABLE, HostState, candidates, probe

    probed: list[HostState] = []

    def probe_one(item: Any) -> HostState:
        state = probe(
            item,
            cluster_up=lambda: _cluster_up(
                declared, item.node, transport, allow_exec, exec_sha256
            ),
        )
        probed.append(state)
        return state

    try:
        states = candidates(
            dev,
            requested=host
            or (
                node and next((h.name for h in dev.hosts if h.kind == "cluster"), None)
            ),
            platform=platform,
            probe=probe_one,
        )
    except DevError as error:
        reject(error.code, str(error))
    for skipped in probed:
        if skipped.up and skipped.quarantined is not None:
            say(
                f"piceli: skipping build host {skipped.host.name}: quarantined: "
                f"{skipped.quarantined} (piceli dev release-host {skipped.host.name})"
            )
    result = {}
    for index, state in enumerate(states):
        chosen = state.host
        try:
            if chosen.kind == "cluster":
                with (
                    connect(declared, transport, allow_exec, exec_sha256) as api,
                    _cluster_guard(),
                ):
                    port = DevCluster(api)
                    installed = port.config()
                    if installed is None:
                        reject(
                            "dev-not-enabled",
                            f"development builds are not installed on "
                            f"{declared.name}: run piceli cluster init",
                        )
                    result = run_on(
                        port, installed, request, say=say,
                        log=None if quiet else say, host=chosen,
                    )  # fmt: skip
            else:
                from piceli.dev.hosts import DockerPort, NativePort

                host_port: Any = (
                    DockerPort(chosen, dev, platform=platform)
                    if chosen.kind == "docker"
                    else NativePort(chosen, dev)
                )
                result = run_on(
                    host_port, dev, request, say=say,
                    log=None if quiet else say, host=chosen,
                )  # fmt: skip
        except DevError as error:
            reject(error.code, str(error))
        _record_health(chosen, result)
        if result.get("reason") == UNAVAILABLE and index + 1 < len(states):
            # 0.20: the host went down between the check and the run.
            say(
                f"piceli: {result.get('message')}; trying {states[index + 1].host.name}"
            )
            continue
        break
    _finish_run(result, quiet, as_json)


def _record_health(chosen: Any, result: dict[str, Any]) -> None:
    """0.22: a run that showed a machine-health signature records an event on
    its docker or native host; the host may now be quarantined."""
    signal = result.get("health")
    if not isinstance(signal, dict) or chosen.kind == "cluster":
        return
    from piceli.dev.health import Health, describe_quarantine
    from piceli.dev.model import DevError

    try:
        health = Health.of(chosen)
        found = None if health is None else health.record(signal, "dev-run")
    except (DevError, OSError) as error:
        say(f"piceli: the health event of {chosen.name} was not recorded ({error})")
        return
    say(
        f"piceli: health event on build host {chosen.name}: {signal.get('kind')}"
        + (f" ({signal.get('line')})" if signal.get("line") else "")
    )
    if found is not None:
        result["host_quarantined"] = found
        say(f"piceli: {describe_quarantine(chosen.name, found)}")


def _finish_run(result: dict[str, Any], quiet: bool, as_json: bool) -> None:
    say(run_summary(result))
    if result.get("state") != "passed" and result.get("first_error"):
        say("first error:")
        for line in result["first_error"]:
            say(f"  | {line}")
    if result.get("state") != "passed" and quiet:
        say("last lines:")
        for line in result.get("log_tail") or ():
            say(f"  | {line}")
    if as_json:
        emit_json(result)
    if result.get("state") != "passed":
        raise typer.Exit(1)


def _cluster_up(
    declared: Any,
    node: str | None,
    transport: str,
    allow_exec: bool,
    exec_sha256: str | None,
) -> bool:
    """Whether the in-cluster host can take runs: the API answers and its
    node is Ready and schedulable (a cordoned builder is skipped)."""
    from piceli.gitops.install import Api
    from piceli.k8s.cli.cluster import exec_policy
    from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig
    from piceli.profiles import override, resolve

    if node is None:
        return False
    try:
        found = resolve(override() or declared.credentials)
        client = api_client_from_kubeconfig(
            found.kubeconfig.absolute(), found.context, transport=transport,  # type: ignore[arg-type]
            exec_policy=exec_policy(allow_exec, exec_sha256),
        )  # fmt: skip
        raw = Api(client).call(f"/api/v1/nodes/{node}", "GET")
    except Exception:
        return False
    if not isinstance(raw, dict) or (raw.get("spec") or {}).get("unschedulable"):
        return False
    return any(
        item.get("type") == "Ready" and item.get("status") == "True"
        for item in (raw.get("status") or {}).get("conditions") or ()
    )


def register(root: typer.Typer) -> None:
    from piceli.k8s.cli.dev_nix import register as register_nix

    register_nix(app)
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


@app.command("hosts")
def hosts(
    cluster: ClusterOption,
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
    as_json: JsonOption = False,
) -> None:
    """Each declared build host: up or down (why), platforms, slots, runs and queue."""
    from piceli.dev.hosts import probe

    declared = load_cluster(cluster)
    dev = declared.dev
    if dev is None:
        reject(
            "dev-not-enabled",
            f"{declared.name} declares no development builds: add "
            "Cluster(dev=DevBuilds(...))",
        )

    def cluster_up(node: str | None) -> Callable[[], bool]:
        return lambda: _cluster_up(declared, node, transport, allow_exec, exec_sha256)

    states = [probe(item, cluster_up=cluster_up(item.node)) for item in dev.hosts]
    for state in states:
        say(
            f"{state.host.name:<12} {state.host.kind:<8} "
            + (
                f"up, {', '.join(state.platforms) or '?'}; "
                f"{state.running}/{state.host.slots} busy, {state.waiting} waiting"
                if state.up
                else f"down: {state.detail}"
            )
            # 0.25.0: a host declared runs=False only publishes.
            + ("" if state.host.runs else "; publishes Nix releases only (runs=False)")
        )
        health = state.health or {}
        if state.quarantined is not None:
            say(
                f"{'':<12} QUARANTINED: {state.quarantined} "
                f"(piceli dev release-host {state.host.name})"
            )
        elif health.get("recent"):
            say(
                f"{'':<12} {health['recent']} health event(s) within the window "
                f"(quarantined at {(health.get('policy') or {}).get('after')})"
            )
    if as_json:
        emit_json(
            {
                "schema": "piceli.dev-hosts.v1",
                "hosts": [state.describe() for state in states],
            }
        )


@app.command("release-host")
def release_host(
    name: Annotated[str, typer.Argument(help="The declared docker or native host")],
    cluster: ClusterOption,
    as_json: JsonOption = False,
) -> None:
    """Clear a build host's health events: it is no longer quarantined (0.22.0)."""
    from piceli.dev.health import Health
    from piceli.dev.model import DevError

    declared = load_cluster(cluster)
    dev = declared.dev
    chosen = dev.host(name) if dev is not None else None
    if chosen is None:
        reject("dev-host-unknown", f"no build host {name!r} is declared")
    if chosen.kind == "cluster":
        reject(
            "dev-host-unknown",
            f"{name} is the cluster's builder node: its health is the GitOps "
            f"controller's (piceli gitops release-host {chosen.node})",
        )
    try:
        health = Health.of(chosen)
        before = None if health is None else health.quarantined()
        cleared = 0 if health is None else health.release()
    except (DevError, OSError) as error:
        reject("dev-host-unavailable", f"build host {name}: {error}")
    say(
        f"build host {name}: {cleared} health event(s) cleared"
        + ("; no longer quarantined" if before is not None else "")
    )
    if as_json:
        emit_json(
            {
                "schema": "piceli.dev-host-release.v1",
                "host": name,
                "cleared": cleared,
                "was_quarantined": before is not None,
            }
        )


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


cache_app = typer.Typer(
    help="The development builds' cargo cache on the builder node.",
    no_args_is_help=True,
)
app.add_typer(cache_app, name="cache")


@cache_app.command("clear")
def cache_clear(
    cluster: ClusterOption,
    lineage: Annotated[
        str | None,
        typer.Option(
            "--lineage",
            help="KEY/N, KEY, or a root source name (every lineage of it); default: all",
        ),
    ] = None,
    crate: Annotated[
        list[str] | None,
        typer.Option("--crate", help="Only this crate's artifacts (repeat)"),
    ] = None,
    host: Annotated[
        str | None,
        typer.Option(
            "--host",
            help="A declared docker or native build host's cache (0.20.0; "
            "default: the in-cluster builder's)",
        ),
    ] = None,
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
    as_json: JsonOption = False,
) -> None:
    """Empty free lineages' cargo targets (a corrupted cache); busy ones are skipped."""
    import secrets

    from piceli.dev.jobs import cache_clear_job
    from piceli.dev.model import DevError

    declared = load_cluster(cluster)
    local = declared.dev
    chosen = local.host(host) if local is not None and host else None
    if host and chosen is None:
        reject("dev-host-unknown", f"no build host {host!r} is declared")
    if local is not None and chosen is not None and chosen.kind != "cluster":
        from piceli.dev.hosts import clear_host_cache

        try:
            result = clear_host_cache(
                chosen, local, lineage=lineage, crates=list(crate or ())
            )
        except DevError as error:
            reject(error.code, str(error))
        _report_cleared(result, crate, as_json)
        return
    with _port(cluster, transport, allow_exec, exec_sha256) as (port, dev):
        job = cache_clear_job(
            dev, name=secrets.token_hex(4), lineage=lineage, crates=list(crate or ())
        )
        try:
            result = port.clear_cache(job)
        except DevError as error:
            reject(error.code, str(error), exit_code=EXIT_FAILED)
    if result is None:
        reject("dev-cache-clear-failed", "the cache-clear Job printed no result")
    _report_cleared(result, crate, as_json)


def _report_cleared(
    result: dict[str, Any], crate: list[str] | None, as_json: bool
) -> None:
    for item in result.get("cleared") or ():
        what = (
            f"{item['entries']} entries of {', '.join(crate or ())}"
            if "entries" in item
            else f"{(item.get('bytes') or 0) / 2**30:.2f} GiB"
        )
        say(f"cleared {item['lineage']}: {what}")
    for name in result.get("busy") or ():
        say(f"busy    {name}: a run holds it; not cleared")
    if not result.get("cleared") and not result.get("busy"):
        say("no lineage matched")
    if as_json:
        emit_json(result)


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
    if recorded.get("first_error"):
        typer.echo("first error:")
        for line in recorded["first_error"]:
            typer.echo(line)
        typer.echo("last lines:")
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
