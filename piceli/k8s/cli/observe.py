"""Human and agent-friendly read-only operations commands.

Machine JSON goes to stdout (JSON lines for ``forwards apply``), human text to
stderr. Refusals print ``{"state": "rejected", "reason": "<code>", "message":
…}`` and exit 2 (see ``piceli explain <code>``).
"""

from __future__ import annotations

import errno
import json
import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Annotated

import typer

from piceli.cli_contract import EXIT_FAILED, reject, rejecting, say
from piceli.k8s.observe import (
    AccessPlan,
    ForwardSupervisor,
    KubernetesDynamicInventoryReader,
    PortForward,
    PreferenceStore,
    UserPreferences,
    archive_resources,
    kubectl_logs_command,
    observe_session,
    preflight_shortcuts,
    probe_endpoint,
    run_logs,
    run_port_forward,
)
from piceli.k8s.observe_server import LocalObserveServer
from piceli.k8s.ops.session import DeploymentSessionArchive
from piceli.k8s.ui_config import (
    UI_CONFIG_ENV,
    UiConfig,
    load_access_profile,
    load_ui_config,
)

MaybeString = str | None
MaybePath = Path | None

app = typer.Typer(
    help="Inspect a deployed Piceli session and local access preferences."
)
forwards_app = typer.Typer(
    help="Supervise the port forwards declared in an access profile, with health probes."
)
app.add_typer(forwards_app, name="forwards")


def _archive(path: Path) -> DeploymentSessionArchive:
    with rejecting(((ValueError, OSError), "invalid-session-archive")):
        return DeploymentSessionArchive.from_json(path.read_text())


def _reader(kubeconfig: Path, context: str | None) -> KubernetesDynamicInventoryReader:
    """The inventory reader; an unusable kubeconfig or context is a rejection."""
    with rejecting((Exception, "kubeconfig-rejected")):
        return KubernetesDynamicInventoryReader(kubeconfig=kubeconfig, context=context)


def _profile(path: Path | None, *, access: bool = False) -> UiConfig:
    with rejecting(((ValueError, OSError), "invalid-access-profile")):
        return load_access_profile(path) if access and path else load_ui_config(path)


def _preferences(store: PreferenceStore) -> dict[str, UserPreferences]:
    with rejecting(((ValueError, OSError), "invalid-preference-store")):
        return store.load()


def _saved(preferences: Path | None, user: str, name: str) -> PortForward:
    """The user's saved forward ``name``, or a rejection."""
    preference = _preferences(PreferenceStore(preferences)).get(user)
    if preference is None:
        reject("no-saved-forwards", "user has no saved port forwards", user=user)
    for forward in preference.forwards:
        if forward.name == name:
            return forward
    reject("unknown-forward", "saved port forward does not exist", name=name)


UI_CONFIG_HELP = (
    "TOML file with dashboard shortcuts, topology tiers, and badges "
    f"(also ${UI_CONFIG_ENV})"
)


def bind_local_server(
    factory: Callable[[], LocalObserveServer],
    port: int,
    supervisor: ForwardSupervisor | None,
) -> LocalObserveServer:
    """Create the loopback server, turning an occupied port into a CLI error."""
    try:
        return factory()
    except OSError as error:
        if supervisor:
            supervisor.close()
        if error.errno == errno.EADDRINUSE:
            reject(
                "local-port-in-use",
                f"local port {port} is already in use; choose another port or "
                "stop the process that owns it",
                port=port,
            )
        raise


def serve_until_interrupted(
    server: LocalObserveServer, supervisor: ForwardSupervisor | None
) -> None:
    """Serve until SIGINT/SIGTERM/SIGHUP, then stop owned forwards."""

    try:
        with interrupts_as_keyboard_interrupt():
            server.serve_forever()
    finally:
        server.server_close()
        if supervisor:
            supervisor.close()


@contextmanager
def interrupts_as_keyboard_interrupt() -> Iterator[None]:
    """Turn SIGINT/SIGTERM/SIGHUP into a swallowed ``KeyboardInterrupt``.

    The previous handlers are restored on exit so owned-forward cleanup in the
    caller's ``finally`` always runs.
    """

    def stop(_signal: int, _frame: FrameType | None) -> None:
        """Turn terminal shutdown into normal owned-forward cleanup."""
        raise KeyboardInterrupt

    previous_handlers = {
        signal_number: signal.signal(signal_number, stop)
        for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    try:
        yield
    except KeyboardInterrupt:
        pass
    finally:
        for signal_number, handler in previous_handlers.items():
            signal.signal(signal_number, handler)


@app.command("status")
def status(
    archive: Annotated[Path, typer.Option(exists=True, readable=True)],
    kubeconfig: Annotated[Path, typer.Option(exists=True, readable=True)],
    context: Annotated[MaybeString, typer.Option()] = None,
    include_common_types: Annotated[bool, typer.Option()] = True,
) -> None:
    """Print declared resources, live state, and objects absent from the archive."""
    report = observe_session(
        _archive(archive),
        _reader(kubeconfig, context),
        include_common_types=include_common_types,
    )
    typer.echo(json.dumps(report.to_dict(), sort_keys=True))


@app.command("forward-save")
def forward_save(
    user: Annotated[str, typer.Option()],
    name: Annotated[str, typer.Option()],
    namespace: Annotated[str, typer.Option()],
    target: Annotated[
        str, typer.Option(help="service/NAME, deployment/NAME or pod/NAME")
    ],
    local_port: Annotated[int, typer.Option()],
    remote_port: Annotated[int, typer.Option()],
    preferences: Annotated[MaybePath, typer.Option()] = None,
) -> None:
    """Persist one harmless port-forward preference for a local user."""
    store = PreferenceStore(preferences)
    with rejecting((ValueError, "invalid-forward-preference")):
        existing = _preferences(store).get(user, UserPreferences(user))
        updated = {item.name: item for item in existing.forwards}
        updated[name] = PortForward(name, namespace, target, local_port, remote_port)
        replacement = UserPreferences(
            user, tuple(sorted(updated.values(), key=lambda item: item.name))
        )
    with rejecting(((ValueError, OSError), "invalid-preference-store")):
        store.replace_user(replacement)
    typer.echo(json.dumps({"saved": name, "user": user}, sort_keys=True))


@app.command("forward-list")
def forward_list(
    user: Annotated[str, typer.Option()],
    preferences: Annotated[MaybePath, typer.Option()] = None,
) -> None:
    """List a user's saved port-forward preferences without starting a process."""
    preference = _preferences(PreferenceStore(preferences)).get(
        user, UserPreferences(user)
    )
    typer.echo(
        json.dumps(
            {"user": user, "forwards": [item.__dict__ for item in preference.forwards]},
            sort_keys=True,
        )
    )


@app.command("forward-command")
def forward_command(
    user: Annotated[str, typer.Option()],
    name: Annotated[str, typer.Option()],
    kubeconfig: Annotated[Path, typer.Option(exists=True, readable=True)],
    context: Annotated[MaybeString, typer.Option()] = None,
    kubectl: Annotated[str, typer.Option()] = "kubectl",
    preferences: Annotated[MaybePath, typer.Option()] = None,
) -> None:
    """Print a JSON argv array for one explicit loopback-only port forward."""
    forward = _saved(preferences, user, name)
    typer.echo(
        json.dumps(
            forward.command(kubectl=kubectl, kubeconfig=kubeconfig, context=context)
        )
    )


@app.command("forward-run")
def forward_run(
    user: Annotated[str, typer.Option()],
    name: Annotated[str, typer.Option()],
    kubeconfig: Annotated[Path, typer.Option(exists=True, readable=True)],
    context: Annotated[MaybeString, typer.Option()] = None,
    kubectl: Annotated[str, typer.Option()] = "kubectl",
    preferences: Annotated[MaybePath, typer.Option()] = None,
) -> None:
    """Run one saved loopback-only port forward until the caller interrupts it."""
    forward = _saved(preferences, user, name)
    with rejecting((ValueError, "invalid-forward-preference")):
        command = forward.command(
            kubectl=kubectl, kubeconfig=kubeconfig, context=context
        )
    raise typer.Exit(run_port_forward(command))


@app.command("logs-command")
def logs_command(
    namespace: Annotated[str, typer.Option()],
    target: Annotated[
        str, typer.Option(help="pod/NAME, deployment/NAME, or another workload")
    ],
    kubeconfig: Annotated[Path, typer.Option(exists=True, readable=True)],
    context: Annotated[MaybeString, typer.Option()] = None,
    tail: Annotated[int, typer.Option(min=1, max=10_000)] = 200,
    container: Annotated[MaybeString, typer.Option()] = None,
    previous: Annotated[bool, typer.Option()] = False,
    kubectl: Annotated[str, typer.Option()] = "kubectl",
) -> None:
    """Print JSON argv for a bounded, explicit workload-log request."""
    with rejecting((ValueError, "invalid-log-request")):
        command = kubectl_logs_command(
            kubectl=kubectl,
            kubeconfig=kubeconfig,
            context=context,
            namespace=namespace,
            target=target,
            tail=tail,
            container=container,
            previous=previous,
        )
    typer.echo(json.dumps(command))


@app.command("logs-run")
def logs_run(
    namespace: Annotated[str, typer.Option()],
    target: Annotated[
        str, typer.Option(help="pod/NAME, deployment/NAME, or another workload")
    ],
    kubeconfig: Annotated[Path, typer.Option(exists=True, readable=True)],
    context: Annotated[MaybeString, typer.Option()] = None,
    tail: Annotated[int, typer.Option(min=1, max=10_000)] = 200,
    container: Annotated[MaybeString, typer.Option()] = None,
    previous: Annotated[bool, typer.Option()] = False,
    kubectl: Annotated[str, typer.Option()] = "kubectl",
) -> None:
    """Run a bounded workload-log request in the caller's foreground terminal."""
    with rejecting((ValueError, "invalid-log-request")):
        command = kubectl_logs_command(
            kubectl=kubectl,
            kubeconfig=kubeconfig,
            context=context,
            namespace=namespace,
            target=target,
            tail=tail,
            container=container,
            previous=previous,
        )
    raise typer.Exit(run_logs(command))


@app.command("serve")
def serve(
    archive: Annotated[Path, typer.Option(exists=True, readable=True)],
    kubeconfig: Annotated[Path, typer.Option(exists=True, readable=True)],
    context: Annotated[MaybeString, typer.Option()] = None,
    namespace: Annotated[
        MaybeString,
        typer.Option(help="Namespace for shortcuts and pods (default: the archive's)"),
    ] = None,
    preferences: Annotated[MaybePath, typer.Option()] = None,
    user: Annotated[
        MaybeString, typer.Option(help="Restore this user's saved forwards")
    ] = None,
    port: Annotated[int, typer.Option(min=1, max=65535)] = 9876,
    ui_config: Annotated[
        MaybePath,
        typer.Option(
            exists=True, readable=True, envvar=UI_CONFIG_ENV, help=UI_CONFIG_HELP
        ),
    ] = None,
    start_shortcuts: Annotated[
        bool,
        typer.Option(
            help="Start and health-supervise every configured shortcut "
            "(port-conflict preflight first)"
        ),
    ] = False,
) -> None:
    """Open the local operations dashboard and optionally restore saved forwards."""
    config = _profile(ui_config)
    session_archive = _archive(archive)
    if namespace is None:
        archive_namespaces = {
            ref.namespace for ref in archive_resources(session_archive) if ref.namespace
        }
        namespace = archive_namespaces.pop() if len(archive_namespaces) == 1 else ""
    reader = _reader(kubeconfig, context)
    store = PreferenceStore(preferences)
    plan_ids: tuple[str, ...] = ()
    if start_shortcuts:
        plan_ids = _preflight_or_exit(config, namespace or None).start
    supervisor = None
    if user or start_shortcuts:
        supervisor = ForwardSupervisor(
            preferences=store,
            user=user or "",
            kubeconfig=kubeconfig,
            context=context,
            shortcuts=config.shortcuts,
            namespace=namespace or None,
        )
        if user:
            with rejecting((ValueError, "invalid-preference-store")):
                supervisor.restore()
        for shortcut_id in plan_ids:
            supervisor.quick_start(shortcut_id, namespace or None)
    server = bind_local_server(
        lambda: LocalObserveServer(
            ("127.0.0.1", port),
            lambda: observe_session(session_archive, reader),
            store,
            supervisor,
            user,
            namespace=namespace,
            ui_config=config,
        ),
        port,
        supervisor,
    )
    typer.echo(json.dumps({"address": f"http://127.0.0.1:{port}", "local_only": True}))
    serve_until_interrupted(server, supervisor)


def _selected(config: UiConfig, only: list[str] | None) -> UiConfig:
    if not only:
        return config
    unknown = sorted(set(only) - {shortcut.id for shortcut in config.shortcuts})
    if unknown:
        reject(
            "unknown-shortcut",
            f"unknown shortcut id(s): {', '.join(unknown)}",
            unknown=unknown,
        )
    return UiConfig(
        shortcuts=tuple(item for item in config.shortcuts if item.id in set(only))
    )


def _preflight_or_exit(config: UiConfig, namespace: str | None) -> AccessPlan:
    """Refuse to start anything when a required local port is already taken."""
    plan = preflight_shortcuts(config.shortcuts, namespace=namespace)
    if plan.errors:
        reject(
            "forward-port-conflict",
            "; ".join(str(item) for item in plan.errors),
            ok=False,
            preflight=plan.to_dict(),
        )
    return plan


def _status_key(item: dict[str, object]) -> tuple[object, ...]:
    return (item["state"], item["health"], item["restarts"], item["error"])


@forwards_app.command("apply")
def forwards_apply(
    profile: Annotated[
        Path,
        typer.Option(
            exists=True,
            readable=True,
            help="Access profile: the --ui-config TOML format ([[shortcuts]] "
            "with optional [shortcuts.health] and [shortcuts.restart])",
        ),
    ],
    kubeconfig: Annotated[Path, typer.Option(exists=True, readable=True)],
    context: Annotated[MaybeString, typer.Option()] = None,
    namespace: Annotated[
        MaybeString, typer.Option(help="Namespace for shortcuts that pin none")
    ] = None,
    only: Annotated[
        list[str] | None, typer.Option(help="Supervise only these shortcut ids")
    ] = None,
    kubectl: Annotated[str, typer.Option()] = "kubectl",
    poll: Annotated[
        float, typer.Option(min=0.1, max=60, help="Status report cadence (seconds)")
    ] = 1.0,
) -> None:
    """Start every declared forward and supervise it in the foreground.

    Prints one JSON line when supervision starts and one per status change
    (state, health, restarts); stops every owned forward on Ctrl-C, SIGTERM or
    SIGHUP.  Exits 2 without starting anything when the port-conflict
    preflight fails.
    """
    config = _selected(_profile(profile, access=True), only)
    plan = _preflight_or_exit(config, namespace)
    plan_ids = plan.start
    supervisor = ForwardSupervisor(
        kubeconfig=kubeconfig,
        context=context,
        kubectl=kubectl,
        shortcuts=config.shortcuts,
        namespace=namespace,
    )
    try:
        for shortcut_id in plan_ids:
            supervisor.quick_start(shortcut_id, namespace)
        typer.echo(
            json.dumps(
                {
                    "event": "started",
                    "forwards": list(plan_ids),
                    "external": list(plan.external),
                },
                sort_keys=True,
            )
        )
        seen: dict[str, tuple[object, ...]] = {}
        wake = threading.Event()
        with interrupts_as_keyboard_interrupt():
            while True:
                for item in supervisor.shortcuts_status(namespace):
                    if item["id"] not in plan_ids:
                        continue
                    key = _status_key(item)
                    if seen.get(item["id"]) != key:
                        seen[item["id"]] = key
                        typer.echo(
                            json.dumps({"event": "status", **item}, sort_keys=True)
                        )
                wake.wait(poll)
    finally:
        supervisor.close()
        typer.echo(json.dumps({"event": "stopped"}))


@forwards_app.command("status")
def forwards_status(
    profile: Annotated[Path, typer.Option(exists=True, readable=True)],
    only: Annotated[
        list[str] | None, typer.Option(help="Check only these shortcut ids")
    ] = None,
) -> None:
    """Probe each declared forward's loopback endpoint once and print JSON.

    It contacts only ``127.0.0.1``, never the cluster, so it works against a
    running ``forwards apply`` (or any other owner of the ports).  Exits 1 when
    a required forward is unhealthy.
    """
    config = _selected(_profile(profile, access=True), only)
    items = []
    healthy_required = True
    for shortcut in config.shortcuts:
        outcome = probe_endpoint(shortcut.local_port, shortcut.probe)
        healthy = outcome is None
        if shortcut.required and not healthy:
            healthy_required = False
        items.append(
            {
                "id": shortcut.id,
                "local_port": shortcut.local_port,
                "target": shortcut.target,
                "url": shortcut.url,
                "required": shortcut.required,
                "health": "healthy" if healthy else "unhealthy",
                "error": outcome,
                "probe": shortcut.probe.public_dict(),
            }
        )
    result: dict[str, object] = {"ok": healthy_required, "forwards": items}
    if not healthy_required:
        unhealthy = [item["id"] for item in items if item["health"] != "healthy"]
        typer.echo(
            json.dumps(
                {**result, "state": "failed", "reason": "forward-unhealthy"},
                sort_keys=True,
            )
        )
        say(f"unhealthy forwards: {', '.join(map(str, unhealthy))} [forward-unhealthy]")
        raise typer.Exit(EXIT_FAILED)
    typer.echo(json.dumps({**result, "state": "healthy"}, sort_keys=True))
