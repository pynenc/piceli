"""``piceli access`` and ``piceli status``: reach the app, and say whether it is up.

Both take one TARGET: a ``release.toml`` path, or ``module:attr`` of an
object with ``.app`` and ``.target`` (see :mod:`piceli.k8s.access`).

Contract (see ``docs/access.md``):

- ``status`` is read-only: it reads the local release state, reads workloads
  and pods through the target's explicit kubeconfig and context, and probes
  the declared forwards on ``127.0.0.1``. ``--json`` prints one
  ``piceli.status.v1`` object. Exit ``0`` when every workload is ready, ``1``
  otherwise, ``2`` when TARGET is rejected.
- ``access`` starts one ``kubectl port-forward`` per declared forward and
  supervises it (health probes, bounded restarts) until interrupted. ``--json``
  prints JSON lines (``started``, ``status``, ``stopped`` events). A required
  forward whose local port is taken is refused with ``access-port-conflict``
  and the owner's pid and command; nothing is started.

Importing this module is side-effect free.
"""

from __future__ import annotations

import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import EXIT_FAILED, emit_json, reject, say
from piceli.k8s.access import (
    AccessTarget,
    AccessTargetError,
    KubernetesWorkloadReader,
    access_ui_config,
    collect_status,
    port_conflicts,
    resolve_target,
    select_shortcuts,
)
from piceli.k8s.ui_config import UI_CONFIG_ENV, load_ui_config

TARGET_HELP = (
    "path/to/release.toml, or module:attr (file.py:attr) of an object with "
    ".app (a piceli App) and .target (kubeconfig, context, namespace)"
)

#: Builds the live workload reader; replaced in tests (never a real cluster).
_workload_reader: Any = KubernetesWorkloadReader


def _resolve(target: str) -> AccessTarget:
    try:
        return resolve_target(target, Path.cwd())
    except AccessTargetError as error:
        say(f"piceli: {error}")
        reject(error.code)


# ------------------------------------------------------------------- status


def _short(digest: str | None) -> str:
    return digest.split(":", 1)[-1][:12] if digest else "-"


def _seconds(timestamp: str) -> str:
    """An ISO timestamp without microseconds (human output; JSON keeps it)."""
    try:
        return datetime.fromisoformat(timestamp).replace(microsecond=0).isoformat()
    except ValueError:
        return timestamp


def _human_status(document: dict[str, Any], target: str) -> str:
    lines = [
        f"{document['app']} is {document['state'].upper()}  "
        f"(namespace {document['namespace']}, context {document['context']})"
    ]
    release = document.get("release")
    if release:
        latest = release.get("latest") or {}
        at = _seconds(str(latest.get("at") or ""))
        when = f", {latest['intent']} at {at}" if at else ""
        lines.append(
            f"release    {release.get('current') or release['name']}  "
            f"{release['state']}{when}"
        )
    lines.append("workloads")
    rows = []
    for item in document["workloads"]:
        replicas = item.get("replicas") or {}
        count = (
            f"{replicas.get('ready', 0)}/{replicas.get('desired', 0)}"
            if replicas
            else "-"
        )
        images = " ".join(
            f"{image['container']}={_short(image['digest'])}"
            for image in item["images"]
        )
        rows.append((item, f"{item['kind']}/{item['name']}", count, images))
    name_width = max((len(row[1]) for row in rows), default=0)
    count_width = max((len(row[2]) for row in rows), default=0)
    for item, name, count, images in rows:
        lines.append(
            f"  {item['health']:<12} {name:<{name_width}}  "
            f"{count:>{count_width}}  {images}".rstrip()
        )
        lines.extend(f"      {problem}" for problem in item["problems"][:5])
    access = document["access"]
    lines.append(f"access     {access['state']}")
    down = False
    for forward in access["forwards"]:
        lines.append(
            f"  {forward['forward']:<9} {forward['id']:<12} {forward['url']}  "
            f"-> {forward['target']}:{forward['remote_port']}"
        )
        owner = forward.get("owner")
        if forward["forward"] == "occupied":
            # Never print another process's command line; the pid is enough.
            holder = f"pid {owner['pid']}" if owner else "another process"
            lines.append(
                f"      port {forward['local_port']} is held by {holder}, not by "
                "piceli (status-port-occupied)"
            )
        elif forward["forward"] != "up":
            down = True
            if owner:
                lines.append(
                    f"      port {forward['local_port']} held by piceli's forward "
                    f"(pid {owner['pid']})"
                )
    if down:
        lines.append(f"Start the forwards with: piceli access {target}")
    checks = document.get("checks")
    if isinstance(checks, dict) and checks.get("state"):
        lines.append(f"checks     {checks['state']}")
    for code in document["errors"]:
        lines.append(f"error      {code} (piceli explain {code})")
    return "\n".join(lines)


def status(
    target: Annotated[str, typer.Argument(help=TARGET_HELP, show_default=False)],
    as_json: Annotated[
        bool, typer.Option("--json", help="Print one piceli.status.v1 JSON object")
    ] = False,
    timeout: Annotated[
        float, typer.Option(min=1, max=60, help="Per-request API timeout (seconds)")
    ] = 10.0,
) -> None:
    """Say whether the app is up and how to reach it. Read-only.

    Reports the release (name, state), each workload's readiness, health and
    image digests, and every declared URL with whether its forward is up
    right now. Exit 0 when every workload is ready, 1 otherwise.
    """
    resolved = _resolve(target)
    reader: Any = None
    reader_error: str | None = None
    if resolved.workloads:
        from piceli.k8s.ops.provider_factory import (
            ExecAuthError,
            ProviderFactoryError,
        )

        try:
            reader = _workload_reader(
                kubeconfig=resolved.kubeconfig,
                context=resolved.context,
                transport=resolved.transport,
                timeout=timeout,
                exec_policy=resolved.exec_policy,
            )
        except ExecAuthError as error:
            reader_error = error.code  # exec plugin refused, never its output
        except ProviderFactoryError:
            reader_error = "access-kubeconfig-invalid"
        except Exception:  # detail may contain credentials
            reader_error = "status-cluster-unreadable"
    try:
        document = collect_status(resolved, reader, reader_error=reader_error)
    finally:
        close = getattr(reader, "close", None)
        if close is not None:
            close()
    if as_json:
        emit_json(document)
    else:
        typer.echo(_human_status(document, target))
    if document["state"] != "up":
        raise typer.Exit(EXIT_FAILED)


# ------------------------------------------------------------------- access


def _check_kubeconfig(resolved: AccessTarget) -> None:
    """The kubeconfig file exists and names the context; never the ambient one.

    A context whose user runs an exec credential plugin is refused unless the
    target allows it (``allow_exec``); an allowed plugin is resolved and
    pinned here (``kubectl`` then runs it itself, as for ``piceli observe``).
    """
    from piceli.k8s.ops.provider_factory import (
        ExecAuthError,
        ProviderFactoryError,
        _named,
        _read_kubeconfig,
        verify_exec_user,
    )

    try:
        _named(_read_kubeconfig(resolved.kubeconfig), "contexts", resolved.context)
        verify_exec_user(
            resolved.kubeconfig, resolved.context, exec_policy=resolved.exec_policy
        )
    except ExecAuthError as error:
        say(f"piceli: {error}")
        reject(error.code)
    except (ProviderFactoryError, OSError) as error:
        say(f"piceli: {error}")
        reject("access-kubeconfig-invalid")


def _dashboard_shortcut(port: int) -> Any:
    from piceli.k8s.ui_config import UiShortcut

    return UiShortcut(
        id="dashboard",
        label="dashboard",
        target="service/dashboard",
        local_port=port,
        remote_port=port,
    )


def _status_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (item["state"], item["health"], item["restarts"], item["error"])


def _serve_dashboard(
    resolved: AccessTarget, supervisor: Any, port: int, ui_config: Path | None
) -> Any:
    from piceli.k8s.observe import KubernetesDynamicInventoryReader, PreferenceStore
    from piceli.k8s.observe_server import LocalObserveServer
    from piceli.k8s.operator import build_operator_report
    from piceli.k8s.release import ReleaseCatalog

    config = access_ui_config(load_ui_config(ui_config), resolved)
    readers: list[Any] = []  # built on first use: the dynamic client calls the API
    catalog = None
    if resolved.spec is not None and resolved.spec.catalog_path.exists():
        catalog = ReleaseCatalog(resolved.spec.catalog_path)

    def report() -> Any:
        if not readers:
            readers.append(
                KubernetesDynamicInventoryReader(
                    kubeconfig=resolved.kubeconfig,
                    context=resolved.context,
                    exec_policy=resolved.exec_policy,
                )
            )
        return build_operator_report(
            readers[0],
            resolved.namespace,
            catalog=catalog,
            managed_labels=config.inventory.managed_labels,
            revision_label=config.inventory.revision_label,
        )

    server = LocalObserveServer(
        ("127.0.0.1", port),
        report,
        PreferenceStore(),
        supervisor,
        None,
        catalog=catalog,
        namespace=resolved.namespace,
        kubeconfig=resolved.kubeconfig,
        context=resolved.context,
        ui_config=config,
    )
    threading.Thread(
        target=server.serve_forever, name="piceli-access-dashboard", daemon=True
    ).start()
    return server


def access(
    target: Annotated[str, typer.Argument(help=TARGET_HELP, show_default=False)],
    only: Annotated[
        list[str] | None,
        typer.Option("--only", help="Supervise only this forward id (repeatable)"),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print JSON lines: started, status, stopped"),
    ] = False,
    kubectl: Annotated[str, typer.Option(help="kubectl executable")] = "kubectl",
    poll: Annotated[
        float, typer.Option(min=0.1, max=60, help="Status report cadence (seconds)")
    ] = 1.0,
    dashboard: Annotated[
        int | None,
        typer.Option(
            min=1,
            max=65535,
            help="Also serve the local dashboard on this loopback port, with "
            "these forwards as its shortcuts",
        ),
    ] = None,
    ui_config: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            readable=True,
            envvar=UI_CONFIG_ENV,
            help="Optional dashboard TOML (badges, tiers, extra shortcuts)",
        ),
    ] = None,
) -> None:
    """Forward the app's declared ports to 127.0.0.1 and keep them healthy.

    Supervises exactly the forwards the model declares (``app.access.forward``)
    with health probes and bounded restarts until Ctrl-C, SIGTERM or SIGHUP,
    then stops every forward it started. Refuses before starting anything when
    a required local port is held by another process, naming its pid and
    command.
    """
    from piceli.k8s.cli.observe import interrupts_as_keyboard_interrupt
    from piceli.k8s.observe import ForwardSupervisor

    resolved = _resolve(target)
    selected, unknown = select_shortcuts(resolved.shortcuts, only)
    if unknown:
        say(f"piceli: unknown forward id(s): {', '.join(unknown)}")
        reject("access-unknown-forward", unknown=unknown)
    if not selected:
        reject("access-none-declared")
    executable = shutil.which(kubectl)
    if executable is None:
        reject("access-kubectl-missing")
    _check_kubeconfig(resolved)
    conflicts = port_conflicts(selected)
    blocking = [item for item in conflicts if item.required]
    if blocking:
        for item in blocking:
            say(f"piceli: {item.describe()}")
        reject(
            "access-port-conflict",
            conflicts=[item.to_dict() for item in blocking],
        )
    if dashboard is not None:
        taken = port_conflicts(
            [_dashboard_shortcut(dashboard)],
        )
        if taken:
            say(f"piceli: dashboard {taken[0].describe()}")
            reject(
                "access-port-conflict",
                conflicts=[item.to_dict() for item in taken],
            )
    skipped = {item.id for item in conflicts}
    start = [item for item in selected if item.id not in skipped]
    supervisor = ForwardSupervisor(
        kubeconfig=resolved.kubeconfig,
        context=resolved.context,
        kubectl=executable,
        shortcuts=selected,
        namespace=resolved.namespace,
    )
    server = None
    failed = False
    try:
        for item in start:
            supervisor.quick_start(item.id, resolved.namespace)
        if dashboard is not None:
            server = _serve_dashboard(resolved, supervisor, dashboard, ui_config)
        started = {
            "event": "started",
            "app": resolved.name,
            "namespace": resolved.namespace,
            "forwards": [
                {
                    "id": item.id,
                    "url": item.url,
                    "target": item.target,
                    "local_port": item.local_port,
                    "remote_port": item.remote_port,
                }
                for item in start
            ],
            "skipped": [item.to_dict() for item in conflicts],
            "dashboard": f"http://127.0.0.1:{dashboard}" if dashboard else None,
        }
        if as_json:
            emit_json(started)
        else:
            typer.echo(
                f"Forwarding {len(start)} port(s) of {resolved.name} "
                f"(namespace {resolved.namespace}); Ctrl-C stops them."
            )
            for item in start:
                typer.echo(
                    f"  {item.id:<12} {item.url}  -> {item.target}:{item.remote_port}"
                )
            for conflict in conflicts:
                typer.echo(f"  skipped: {conflict.describe()}")
            if dashboard:
                typer.echo(f"  dashboard    http://127.0.0.1:{dashboard}")
        ids = {item.id for item in start}
        seen: dict[str, tuple[Any, ...]] = {}
        wake = threading.Event()
        with interrupts_as_keyboard_interrupt():
            while True:
                items = [
                    item
                    for item in supervisor.shortcuts_status(resolved.namespace)
                    if item["id"] in ids
                ]
                for item in items:
                    key = _status_key(item)
                    if seen.get(item["id"]) == key:
                        continue
                    seen[item["id"]] = key
                    if as_json:
                        emit_json({"event": "status", **item})
                    else:
                        detail = f" ({item['error']})" if item["error"] else ""
                        typer.echo(f"{item['id']}: {item['health']}{detail}")
                if items and all(item["state"] == "failed" for item in items):
                    failed = True
                    break
                wake.wait(poll)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        supervisor.close()
        if failed:
            say("piceli: every forward gave up (access-forwards-failed)")
            stopped = {
                "event": "stopped",
                "state": "failed",
                "reason": "access-forwards-failed",
            }
        else:
            stopped = {"event": "stopped", "state": "stopped"}
        if as_json:
            emit_json(stopped)
        else:
            typer.echo("stopped")
    if failed:
        raise typer.Exit(EXIT_FAILED)


def register(app: typer.Typer) -> None:
    """Add ``access`` and ``status`` to the root application."""
    app.command("access")(access)
    app.command("status")(status)
