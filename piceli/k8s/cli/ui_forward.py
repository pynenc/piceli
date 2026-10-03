"""The in-cluster UI of a composition: ``piceli ui forward-serve`` and ``piceli access ui``.

``piceli cluster init`` installs the UI in ``piceli-system`` (see
:mod:`piceli.infra.ui_install`). In its pod, ``piceli ui forward-serve``
serves the bundled UI on the pod's loopback only, with a fresh launch token
that it writes into the Secret ``piceli-ui-launch`` (never into a log).

On a laptop, ``piceli access ui --cluster MODULE:ATTR`` (or ``--profile
NAME``) reads that token with the profile's explicit kubeconfig and
context, starts a supervised ``kubectl port-forward`` to the UI Service on
``127.0.0.1:8790`` and prints the one URL that opens it. Ctrl-C stops the
forward.

Importing this module is side-effect free.
"""

from __future__ import annotations

import base64
import secrets
import shutil
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import EXIT_FAILED, emit_json, reject, say
from piceli.infra.ui_install import LAUNCH_KEY, LAUNCH_SECRET, NAME, NAMESPACE, PORT

SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class LaunchError(Exception):
    """The installed UI cannot be reached yet (``code`` is fixed)."""

    def __init__(self, *, code: str) -> None:
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------- the API


class _Conflict(Exception):
    pass


def _call(client: Any, path: str, method: str, body: Any = None) -> Any:
    """One API call; ``None`` on 404, :class:`LaunchError` otherwise."""
    import json

    from kubernetes.client.exceptions import ApiException

    try:
        response = client.call_api(
            path,
            method,
            query_params=[("fieldManager", "piceli-ui")] if method == "PATCH" else [],
            header_params={
                "Accept": "application/json",
                "Content-Type": "application/merge-patch+json",
            },
            body=body,
            auth_settings=["BearerToken"],
            _preload_content=False,
            _request_timeout=10,
        )
    except ApiException as error:
        if error.status == 404:
            return None
        if error.status == 409:
            raise _Conflict() from None
        if error.status in {401, 403}:
            raise LaunchError(code="access-ui-forbidden") from None
        raise LaunchError(code="access-ui-unreachable") from None
    except OSError:
        raise LaunchError(code="access-ui-unreachable") from None
    raw = response[0] if isinstance(response, tuple) else response
    return json.loads(raw.data or b"null")


def publish_token(client: Any, token: str, namespace: str = NAMESPACE) -> None:
    """Write ``token`` into the UI's launch Secret (or clear it with ``""``)."""
    value = base64.b64encode(token.encode()).decode() if token else None
    path = f"/api/v1/namespaces/{namespace}/secrets/{LAUNCH_SECRET}"
    for _ in range(5):
        current = _call(client, path, "GET")
        if current is None:
            raise LaunchError(code="access-ui-not-installed")
        meta = current.get("metadata") or {}
        body = {
            "metadata": {
                "name": LAUNCH_SECRET,
                "uid": meta.get("uid"),
                "resourceVersion": meta.get("resourceVersion"),
            },
            "data": {LAUNCH_KEY: value},
        }
        try:
            if _call(client, path, "PATCH", body) is None:
                raise LaunchError(code="access-ui-not-installed")
            return
        except _Conflict:
            continue
    raise LaunchError(code="access-ui-unreachable")


def read_launch(client: Any, namespace: str = NAMESPACE) -> str:
    """The installed UI's current launch token (never logged)."""
    base = f"/api/v1/namespaces/{namespace}"
    if _call(client, f"{base}/services/{NAME}", "GET") is None:
        raise LaunchError(code="access-ui-not-installed")
    secret = _call(client, f"{base}/secrets/{LAUNCH_SECRET}", "GET")
    if secret is None:
        raise LaunchError(code="access-ui-not-installed")
    encoded = (secret.get("data") or {}).get(LAUNCH_KEY)
    try:
        token = base64.b64decode(encoded or "", validate=True).decode()
    except ValueError:
        token = ""
    if not token or not token.replace("-", "").replace("_", "").isalnum():
        raise LaunchError(code="access-ui-not-ready")
    return token


def _read_with_profile(kubeconfig: Path, context: str) -> str:
    from piceli.gitops import GitOpsError
    from piceli.gitops.install import connect

    try:
        with connect(kubeconfig, context) as api:
            return read_launch(api.client)
    except GitOpsError:
        raise LaunchError(code="access-kubeconfig-invalid") from None


#: Reads the launch token; replaced in tests (never a real cluster).
_launch_reader: Any = _read_with_profile


# ------------------------------------------------------- in the pod


def forward_serve(
    namespace: Annotated[
        str, typer.Option(help="The controller's namespace (status and requests)")
    ] = NAMESPACE,
    port: Annotated[
        int, typer.Option(min=1, max=65535, help="Loopback port in the pod")
    ] = PORT,
    launch_secret: Annotated[
        str, typer.Option(help="Secret that receives the launch token")
    ] = LAUNCH_SECRET,
) -> None:
    """Serve the composition UI in its pod, reached only through `piceli access ui`.

    Runs with the pod's service account. Listens on the pod's loopback; writes
    a fresh launch token into the launch Secret and never prints it.
    """
    import os

    if launch_secret != LAUNCH_SECRET:
        reject("ui-invalid-request")
    try:
        import uvicorn

        from piceli.gitops.install import connect
        from piceli.gitops.state import ConfigMapChannel
        from piceli.server.app import create_app
        from piceli.server.incluster import InClusterCredential
        from piceli.server.security import uvicorn_log_config
        from piceli.services.composition_control import CompositionControl
        from piceli.services.logs import LogService
        from piceli.services.query import QueryService
        from piceli.services.registration import Registration
    except ImportError:
        say("Install piceli[ui] to serve the web application.")
        reject("ui-assets-unavailable")
    with tempfile.TemporaryDirectory(prefix="piceli-ui-") as private:
        try:
            host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
            api_port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
            credential = InClusterCredential(
                f"https://{f'[{host}]' if ':' in host else host}:{api_port}",
                SERVICE_ACCOUNT_DIR / "ca.crt",
                SERVICE_ACCOUNT_DIR / "token",
                namespace,
            )
            target = credential.write_kubeconfig(Path(private) / "config")
            base = Registration("cluster", namespace, target)
            registration = Registration(
                "cluster",
                namespace,
                target,
                kinds=tuple(
                    item
                    for item in base.kinds
                    if item[1] not in {"Secret", "ConfigMap"}
                ),
            )
            query = QueryService([registration])

            @contextmanager
            def channel() -> Iterator[Any]:
                with connect(target.kubeconfig, target.context) as api:
                    yield ConfigMapChannel(api.client, namespace)

            token = secrets.token_urlsafe(32)
            server = create_app(
                query,
                origin=f"http://127.0.0.1:{port}",
                logs=LogService(query),
                composition_control=CompositionControl(query, "cluster", channel),
                launch_token=token,
            )
            with connect(target.kubeconfig, target.context) as api:
                publish_token(api.client, token, namespace)
        except LaunchError as error:
            reject(error.code)
        except (ValueError, OSError):
            reject("ui-invalid-request")
        say(
            f"Piceli UI: serving on 127.0.0.1:{port} in this pod; "
            "open it with `piceli access ui`"
        )
        try:
            uvicorn.run(
                server,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                log_config=uvicorn_log_config(),
            )
        finally:
            # A stopped server's token opens nothing; clear it anyway.
            try:
                with connect(target.kubeconfig, target.context) as api:
                    publish_token(api.client, "", namespace)
            except Exception:  # best effort on the way out
                pass


# --------------------------------------------------------- on a laptop


def ui_registry() -> Any:
    """The private record of the UI forwards ``access ui`` starts, or ``None``.

    The same directory as ``piceli ui serve`` (``$XDG_STATE_HOME/piceli/ui/
    forwards``); ``None`` when it cannot be created privately.
    """
    from piceli.k8s.owned_processes import OwnedProcessRegistry
    from piceli.k8s.ui_state import private_ui_state_dir

    try:
        return OwnedProcessRegistry(private_ui_state_dir() / "forwards")
    except (OSError, ValueError):
        return None


def ui_holder_check(
    kubeconfig: Path, context: str, *, registry: Any = None, port: int = PORT
) -> Any:
    """Recognise Piceli's own UI forward on ``port`` (see ``port_owner.recognise``).

    The ``kubectl port-forward`` that ``piceli access ui`` starts for this
    kubeconfig and context (supervised by a ``piceli access ui`` that is
    still running, or orphaned), or a child the registry recorded for that
    exact forward. Anything else is not Piceli's.
    """
    from piceli.k8s.port_owner import PortOwner, recognise

    forwards = ((NAMESPACE, f"service/{NAME}", port, PORT),)

    def check(found: PortOwner | None) -> Any:
        return recognise(
            found,
            kubeconfig=str(kubeconfig),
            context=context,
            forwards=forwards,
            registry=registry,
        )

    return check


def stale_ui_command(cluster: str | None, profile: str | None) -> str:
    """The ``piceli access stop --stale`` line for this UI."""
    which = f"--cluster {cluster}" if cluster is not None else f"--profile {profile}"
    return f"piceli access stop --stale {which}"


def _credentials(cluster: str | None, profile: str | None) -> tuple[Path, str]:
    from piceli.profiles import ProfileError, resolve

    if (cluster is None) == (profile is None):
        say("piceli: name the cluster with --cluster MODULE:ATTR or --profile NAME")
        reject("access-ui-target-required")
    name = profile
    if cluster is not None:
        from piceli.app.render import load_target
        from piceli.infra import Cluster

        try:
            declared = load_target(cluster, Path.cwd())
        except Exception:
            declared = None
        if not isinstance(declared, Cluster):
            say(f"piceli: {cluster} is not a piceli.infra.Cluster")
            reject("access-ui-cluster-invalid")
        if declared.ui is None:
            say(f"piceli: cluster {declared.name} declares no Ui(...)")
            reject("access-ui-not-declared")
        name = declared.credentials
    assert name is not None
    try:
        found = resolve(name)
    except ProfileError as error:
        say(f"piceli: {error}")
        reject(error.code)
    return found.kubeconfig, found.context


def access_ui(
    cluster: Annotated[
        str | None,
        typer.Option(
            "--cluster",
            help="The composition's Cluster (MODULE:ATTR); its credentials profile is used",
            show_default=False,
        ),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="A `piceli login` credential profile of the cluster",
            show_default=False,
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print JSON lines: started, status, stopped"),
    ] = False,
    kubectl: Annotated[str, typer.Option(help="kubectl executable")] = "kubectl",
    poll: Annotated[
        float, typer.Option(min=0.1, max=60, help="Status report cadence (seconds)")
    ] = 1.0,
) -> None:
    """Open the UI that `piceli cluster init` installed: forward it and print its URL.

    Reads the UI's launch token with the profile's explicit kubeconfig and
    context, forwards 127.0.0.1:8790 to the UI Service in piceli-system and
    supervises the forward until Ctrl-C. The printed URL is the only place the
    token appears.
    """
    from piceli.k8s.access import port_conflicts
    from piceli.k8s.cli.observe import interrupts_as_keyboard_interrupt
    from piceli.k8s.observe import ForwardSupervisor
    from piceli.k8s.ui_config import UiShortcut

    kubeconfig, context = _credentials(cluster, profile)
    executable = shutil.which(kubectl)
    if executable is None:
        reject("access-kubectl-missing")
    try:
        token = _launch_reader(kubeconfig, context)
    except LaunchError as error:
        reject(error.code)
    shortcut = UiShortcut(
        id="ui",
        label="Piceli UI",
        target=f"service/{NAME}",
        namespace=NAMESPACE,
        local_port=PORT,
        remote_port=PORT,
    )
    registry = ui_registry()
    if registry is not None:
        registry.reap_orphans()  # forwards of an `access ui` that crashed
    conflicts = port_conflicts(
        [shortcut],
        recognise_owner=ui_holder_check(kubeconfig, context, registry=registry),
    )
    if conflicts:
        say(f"piceli: {conflicts[0].describe()}")
        if any(getattr(getattr(c, "holder", None), "piceli", False) for c in conflicts):
            say(
                "piceli: stop piceli's stale processes with: "
                + stale_ui_command(cluster, profile)
            )
        reject(
            "access-port-conflict",
            conflicts=[item.to_dict() for item in conflicts],
        )
    supervisor = ForwardSupervisor(
        kubeconfig=kubeconfig,
        context=context,
        kubectl=executable,
        shortcuts=[shortcut],
        namespace=NAMESPACE,
        registry=registry,
    )
    url = f"http://127.0.0.1:{PORT}/?token={token}"
    failed = False
    seen: tuple[Any, ...] | None = None
    try:
        supervisor.quick_start(shortcut.id, NAMESPACE)
        if as_json:
            emit_json(
                {
                    "event": "started",
                    "namespace": NAMESPACE,
                    "service": NAME,
                    "local_port": PORT,
                    "url": url,
                }
            )
        else:
            say(f"Forwarding the Piceli UI (namespace {NAMESPACE}); Ctrl-C stops it.")
            typer.echo(f"Piceli UI: {url}")
        wake = threading.Event()
        with interrupts_as_keyboard_interrupt():
            while True:
                items = supervisor.shortcuts_status(NAMESPACE)
                for item in items:
                    key = (item["state"], item["health"], item["error"])
                    if key == seen:
                        continue
                    seen = key
                    if as_json:
                        emit_json({"event": "status", **item})
                    else:
                        detail = f" ({item['error']})" if item["error"] else ""
                        say(f"ui: {item['health']}{detail}")
                if items and all(item["state"] == "failed" for item in items):
                    failed = True
                    break
                wake.wait(poll)
    except KeyboardInterrupt:
        pass
    finally:
        supervisor.close()
        stopped: dict[str, Any] = (
            {"event": "stopped", "state": "failed", "reason": "access-forwards-failed"}
            if failed
            else {"event": "stopped", "state": "stopped"}
        )
        if as_json:
            emit_json(stopped)
        else:
            say("stopped")
    if failed:
        raise typer.Exit(EXIT_FAILED)
