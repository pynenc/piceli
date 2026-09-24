"""Human and agent-friendly read-only operations commands."""

from __future__ import annotations

import errno
import json
import signal
from pathlib import Path
from collections.abc import Callable
from types import FrameType
from typing import Annotated, Optional

import typer

from piceli.k8s.observe import (
    KubernetesDynamicInventoryReader,
    ForwardSupervisor,
    PortForward,
    PreferenceStore,
    UserPreferences,
    archive_resources,
    kubectl_logs_command,
    observe_session,
    run_logs,
    run_port_forward,
)
from piceli.k8s.observe_server import LocalObserveServer
from piceli.k8s.ops.session import DeploymentSessionArchive
from piceli.k8s.ui_config import UI_CONFIG_ENV, load_ui_config

MaybeString = Optional[str]  # noqa: UP007 - Typer 0.9 cannot inspect ``str | None``.
MaybePath = Optional[Path]  # noqa: UP007 - Typer 0.9 cannot inspect ``Path | None``.

app = typer.Typer(
    help="Inspect a deployed Piceli session and local access preferences."
)


def _archive(path: Path) -> DeploymentSessionArchive:
    return DeploymentSessionArchive.from_json(path.read_text())


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
            raise typer.BadParameter(
                f"local port {port} is already in use; choose another port or "
                "stop the process that owns it",
                param_hint="--port",
            ) from None
        raise


def serve_until_interrupted(
    server: LocalObserveServer, supervisor: ForwardSupervisor | None
) -> None:
    """Serve until SIGINT/SIGTERM/SIGHUP, then stop owned forwards."""

    def stop_server(_signal: int, _frame: FrameType | None) -> None:
        """Turn terminal shutdown into normal owned-forward cleanup."""
        raise KeyboardInterrupt

    previous_handlers = {
        signal_number: signal.signal(signal_number, stop_server)
        for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for signal_number, handler in previous_handlers.items():
            signal.signal(signal_number, handler)
        server.server_close()
        if supervisor:
            supervisor.close()


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
        KubernetesDynamicInventoryReader(kubeconfig=kubeconfig, context=context),
        include_common_types=include_common_types,
    )
    typer.echo(json.dumps(report.to_dict(), sort_keys=True))


@app.command("forward-save")
def forward_save(
    user: Annotated[str, typer.Option()],
    name: Annotated[str, typer.Option()],
    namespace: Annotated[str, typer.Option()],
    target: Annotated[str, typer.Option(help="service/NAME or pod/NAME")],
    local_port: Annotated[int, typer.Option()],
    remote_port: Annotated[int, typer.Option()],
    preferences: Annotated[MaybePath, typer.Option()] = None,
) -> None:
    """Persist one harmless port-forward preference for a local user."""
    store = PreferenceStore(preferences)
    existing = store.load().get(user, UserPreferences(user))
    updated = {item.name: item for item in existing.forwards}
    updated[name] = PortForward(name, namespace, target, local_port, remote_port)
    store.replace_user(
        UserPreferences(
            user, tuple(sorted(updated.values(), key=lambda item: item.name))
        )
    )
    typer.echo(json.dumps({"saved": name, "user": user}, sort_keys=True))


@app.command("forward-list")
def forward_list(
    user: Annotated[str, typer.Option()],
    preferences: Annotated[MaybePath, typer.Option()] = None,
) -> None:
    """List a user's saved port-forward preferences without starting a process."""
    preference = PreferenceStore(preferences).load().get(user, UserPreferences(user))
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
    preference = PreferenceStore(preferences).load().get(user)
    if preference is None:
        raise typer.BadParameter("user has no saved port forwards")
    for forward in preference.forwards:
        if forward.name == name:
            typer.echo(
                json.dumps(
                    forward.command(
                        kubectl=kubectl, kubeconfig=kubeconfig, context=context
                    )
                )
            )
            return
    raise typer.BadParameter("saved port forward does not exist")


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
    preference = PreferenceStore(preferences).load().get(user)
    if preference is None:
        raise typer.BadParameter("user has no saved port forwards")
    for forward in preference.forwards:
        if forward.name == name:
            raise typer.Exit(
                run_port_forward(
                    forward.command(
                        kubectl=kubectl, kubeconfig=kubeconfig, context=context
                    )
                )
            )
    raise typer.BadParameter("saved port forward does not exist")


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
    typer.echo(
        json.dumps(
            kubectl_logs_command(
                kubectl=kubectl,
                kubeconfig=kubeconfig,
                context=context,
                namespace=namespace,
                target=target,
                tail=tail,
                container=container,
                previous=previous,
            )
        )
    )


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
    raise typer.Exit(
        run_logs(
            kubectl_logs_command(
                kubectl=kubectl,
                kubeconfig=kubeconfig,
                context=context,
                namespace=namespace,
                target=target,
                tail=tail,
                container=container,
                previous=previous,
            )
        )
    )


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
        typer.Option(exists=True, readable=True, envvar=UI_CONFIG_ENV, help=UI_CONFIG_HELP),
    ] = None,
) -> None:
    """Open the local operations dashboard and optionally restore saved forwards."""
    config = load_ui_config(ui_config)
    session_archive = _archive(archive)
    if namespace is None:
        archive_namespaces = {
            ref.namespace for ref in archive_resources(session_archive) if ref.namespace
        }
        namespace = archive_namespaces.pop() if len(archive_namespaces) == 1 else ""
    reader = KubernetesDynamicInventoryReader(kubeconfig=kubeconfig, context=context)
    store = PreferenceStore(preferences)
    supervisor = None
    if user:
        supervisor = ForwardSupervisor(
            preferences=store,
            user=user,
            kubeconfig=kubeconfig,
            context=context,
            shortcuts=config.shortcuts,
            namespace=namespace or None,
        )
        supervisor.restore()
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
