"""Local web application with explicit target registration."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from piceli.cli_contract import reject, say
from piceli.k8s.cli.ui_remote import connect as connect_remote

app = typer.Typer(rich_markup_mode=None, help="Piceli delivery web application.")

app.command("connect")(connect_remote)


@app.command()
def serve(
    kubeconfig: Annotated[
        Path | None, typer.Option(help="Explicit kubeconfig file")
    ] = None,
    context: Annotated[
        str | None, typer.Option(help="Explicit kubeconfig context")
    ] = None,
    namespace: Annotated[str | None, typer.Option(help="Inventory namespace")] = None,
    definition: Annotated[
        Path | None, typer.Option(help="Existing release TOML definition")
    ] = None,
    name: Annotated[str, typer.Option(help="Application display name")] = "my-app",
    host: Annotated[str, typer.Option(help="Loopback bind address")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
    url_prefix: Annotated[str, typer.Option(help="URL prefix, e.g. /piceli")] = "",
    allow_exec: Annotated[
        bool, typer.Option(help="Authorize the target's credential plugin")
    ] = False,
    exec_sha256: Annotated[str | None, typer.Option()] = None,
    control_dir: Annotated[
        Path | None, typer.Option(help="Private durable UI operation state directory")
    ] = None,
    source_root: Annotated[
        Path | None,
        typer.Option(help="Explicit root of allowed definition source files"),
    ] = None,
    source_file: Annotated[
        list[str] | None,
        typer.Option(help="Allowed relative source file; repeat for each file"),
    ] = None,
    renderer_image: Annotated[
        str | None,
        typer.Option(help="Pinned local isolated renderer image ID (sha256:...)"),
    ] = None,
    renderer_platform: Annotated[
        str | None, typer.Option(help="Renderer platform: linux/amd64 or linux/arm64")
    ] = None,
    docker: Annotated[
        Path | None,
        typer.Option(help="Docker executable to pin for isolated evaluation"),
    ] = None,
    docker_socket: Annotated[
        Path, typer.Option(help="Explicit local Docker daemon socket")
    ] = Path("/var/run/docker.sock"),
    state_dir: Annotated[
        Path | None,
        typer.Option(
            help="Private UI state directory (default: $XDG_STATE_HOME/piceli/ui)"
        ),
    ] = None,
) -> None:
    """Register an existing definition or inventory scope and serve the bundled UI."""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        reject("ui-request-rejected")
    try:
        import uvicorn

        from piceli.server.app import create_app
        from piceli.services.query import QueryService
        from piceli.services.registration import Registration
    except ImportError:
        say("Install piceli[ui] to serve the web application.")
        reject("ui-assets-unavailable")
    from piceli.k8s.ops.provider_factory import KubeconfigTarget
    from piceli.k8s.release_spec import ReleaseSpec

    try:
        if definition is not None:
            # The explicit target lives in the existing definition. Do not
            # silently reinterpret its namespace or credentials from UI flags.
            if (
                any(value is not None for value in (kubeconfig, context, namespace))
                or allow_exec
                or exec_sha256
            ):
                reject("ui-invalid-request")
            registration = Registration.from_release(
                "my-app", name, ReleaseSpec.from_toml(definition)
            )
        else:
            if kubeconfig is None or not context or not namespace:
                reject("ui-invalid-request")
            target = KubeconfigTarget(
                kubeconfig=kubeconfig.resolve(),
                context=context,
                namespace=namespace,
                allow_exec=allow_exec,
                exec_sha256=exec_sha256,
            )
            registration = Registration(id="my-app", name=name, target=target)
        query = QueryService([registration])
        from piceli.k8s.owned_processes import OwnedProcessRegistry
        from piceli.k8s.ui_state import private_ui_state_dir
        from piceli.services.access import AccessService
        from piceli.services.logs import LogService

        private_state = private_ui_state_dir(state_dir)
        forwards = OwnedProcessRegistry(private_state / "forwards")
        # Forwards left running by a UI server that crashed or was killed.
        reaped = forwards.reap_orphans()
        if reaped:
            say(f"Stopped {len(reaped)} port forward(s) left by a previous Piceli UI.")
        kubectl_path = shutil.which("kubectl")
        access = AccessService(
            query,
            kubectl=Path(kubectl_path) if kubectl_path else None,
            registry=forwards,
        )
        logs = LogService(query)
        operations = None
        delivery_requested = any(
            item is not None
            for item in (
                control_dir,
                source_root,
                source_file,
                renderer_image,
                renderer_platform,
                docker,
            )
        )
        if delivery_requested:
            spec = registration.release_spec
            if (
                spec is None
                or not source_root
                or not source_file
                or not renderer_image
                or not renderer_platform
            ):
                reject("ui-invalid-request")
            from piceli.artifacts.process import ToolPin
            from piceli.services.contracts import Principal
            from piceli.services.evaluation import (
                DockerEvaluator,
                RendererConfig,
                SourceSelection,
                denied_paths,
            )
            from piceli.services.operations import OperationService
            from piceli.services.store import Store

            executable = docker or (
                Path(found) if (found := shutil.which("docker")) else None
            )
            if executable is None:
                reject("ui-operation-unavailable")
            source_root = source_root.resolve(strict=True)
            state = (control_dir or spec.state_dir / "ui-control").resolve()
            entrypoint = spec.model.release.composition
            module, separator, attribute = entrypoint.rpartition(":")
            if module.endswith(".py") or "/" in module:
                module = (
                    spec.resolve(Path(module))
                    .resolve(strict=True)
                    .relative_to(source_root)
                    .as_posix()
                )
                entrypoint = module + separator + attribute
            selection = SourceSelection(
                root=source_root,
                files=tuple(source_file),
                entrypoint=entrypoint,
                forbidden_paths=(*denied_paths(spec), state),
            )
            renderer = RendererConfig(
                image_id=renderer_image,
                platform=renderer_platform,
                docker=ToolPin.capture(executable),
                socket=docker_socket,
            )
            operations = OperationService(
                query,
                Store(state / "operations.sqlite3"),
                DockerEvaluator(state / "evaluations", renderer),
                {registration.id: selection},
                principal=Principal(id="local", name="Local user"),
            )
        origin = f"http://{'[' + host + ']' if ':' in host else host}:{port}"
        server = create_app(
            query,
            origin=origin,
            url_prefix=url_prefix,
            operations=operations,
            access=access,
            logs=logs,
        )
        from piceli.k8s.ui_state import write_launch_token
        from piceli.server.security import uvicorn_log_config

        token_file = write_launch_token(
            private_state, port, server.state.security.launch_token
        )
    except (ValueError, OSError):
        reject("ui-invalid-request")
    try:
        # Like a notebook server: only a browser that opens this address gets
        # the local session. The token is also in a 0600 file while running.
        say(f"Piceli UI: {server.state.security.launch_url()}")
        say(f"Launch token file: {token_file}")
        uvicorn.run(
            server,
            host=host,
            port=port,
            log_level="warning",
            log_config=uvicorn_log_config(),
        )
    finally:
        token_file.unlink(missing_ok=True)


@app.command("cluster-serve")
@app.command("cluster-observe")
def cluster_observe(
    api_server: Annotated[
        str, typer.Option(help="Explicit in-cluster HTTPS Kubernetes API origin")
    ],
    ca_file: Annotated[Path, typer.Option(help="Mounted service-account CA file")],
    token_file: Annotated[
        Path, typer.Option(help="Mounted rotating service-account token file")
    ],
    namespace: Annotated[str, typer.Option(help="One authorized namespace")],
    control_dir: Annotated[
        Path, typer.Option(help="Private directory for the generated kubeconfig")
    ],
    origin: Annotated[str, typer.Option(help="Public HTTPS browser origin")],
    oidc_issuer: Annotated[str, typer.Option(help="Trusted OIDC issuer")],
    oidc_metadata_url: Annotated[
        str, typer.Option(help="Explicit OIDC discovery document URL")
    ],
    oidc_client_id: Annotated[str, typer.Option(help="Registered public OIDC client")],
    authorized_sub: Annotated[
        list[str], typer.Option(help="OIDC subject granted inspection; repeat")
    ],
    authorized_deploy_sub: Annotated[
        list[str] | None,
        typer.Option(help="OIDC subject granted manual delivery; repeat"),
    ] = None,
    authorized_access_sub: Annotated[
        list[str] | None,
        typer.Option(help="Inspection subject allowed local-client forwarding; repeat"),
    ] = None,
    definition: Annotated[
        Path | None, typer.Option(help="Mounted release TOML definition")
    ] = None,
    source_root: Annotated[
        Path | None, typer.Option(help="Mounted root of allowed source files")
    ] = None,
    source_file: Annotated[
        list[str] | None, typer.Option(help="Allowed source file; repeat")
    ] = None,
    renderer_image: Annotated[
        str | None, typer.Option(help="Immutable repository@sha256 renderer image")
    ] = None,
    renderer_platform: Annotated[
        str | None, typer.Option(help="Renderer platform")
    ] = None,
    name: Annotated[str, typer.Option(help="Application display name")] = "cluster",
    host: Annotated[
        str, typer.Option(help="Loopback bind for a TLS gateway sidecar")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
    url_prefix: Annotated[str, typer.Option()] = "",
) -> None:
    """Serve scoped cluster observation and configured manual delivery."""
    if (
        host not in {"127.0.0.1", "::1"}
        or not authorized_sub
        or len(authorized_sub) > 100
        or any(not subject or len(subject) > 256 for subject in authorized_sub)
        or not all(path.is_absolute() for path in (ca_file, token_file, control_dir))
        or any(
            not subject or len(subject) > 256
            for subject in (authorized_deploy_sub or ())
        )
        or not set(authorized_deploy_sub or ()).issubset(set(authorized_sub))
        or any(
            not subject or len(subject) > 256
            for subject in (authorized_access_sub or ())
        )
        or not set(authorized_access_sub or ()).issubset(set(authorized_sub))
    ):
        reject("ui-invalid-request")
    try:
        import uvicorn

        from piceli.server.app import create_app
        from piceli.server.cluster_security import (
            ClusterSecurity,
            ClusterSecurityConfig,
        )
        from piceli.server.incluster import InClusterCredential
        from piceli.services.authority import ScopePolicy
        from piceli.services.logs import LogService
        from piceli.services.query import QueryService
        from piceli.services.registration import Registration
        from piceli.services.remote_access import RemoteAccessService
    except ImportError:
        say("Install piceli[ui] to serve the web application.")
        reject("ui-assets-unavailable")
    try:
        if not origin.startswith("https://"):
            reject("ui-invalid-request")
        identity = InClusterCredential(
            api_server,
            ca_file,
            token_file,
            namespace,
        )
        target = identity.write_kubeconfig(control_dir / "target.kubeconfig")
        delivery_requested = any(
            value is not None
            for value in (
                definition,
                source_root,
                source_file,
                renderer_image,
                renderer_platform,
            )
        ) or bool(authorized_deploy_sub)
        if delivery_requested:
            if (
                definition is None
                or source_root is None
                or not source_file
                or renderer_image is None
                or renderer_platform is None
                or not authorized_deploy_sub
                or not definition.is_absolute()
                or not source_root.is_absolute()
            ):
                reject("ui-invalid-request")
            from piceli.k8s.ops.provider_factory import (
                api_client_from_kubeconfig,
                read_cluster_identity,
            )
            from piceli.k8s.release_spec import ReleaseSpec

            api = api_client_from_kubeconfig(target.kubeconfig, target.context)
            try:
                observed = read_cluster_identity(api, target)
            finally:
                api.close()
            target = replace(
                target,
                cluster_uid=observed.cluster_uid,
                namespace_uid=observed.namespace_uid,
            )
            spec = ReleaseSpec.from_toml(definition)
            declared = spec.kubeconfig_target()
            if (
                declared.kubeconfig.resolve() != target.kubeconfig.resolve()
                or declared.context != target.context
                or declared.namespace != target.namespace
                or not spec.state_dir.resolve().is_relative_to(control_dir.resolve())
            ):
                reject("ui-invalid-request")
            registration = replace(
                Registration.from_release("cluster", name, spec), target=target
            )
        else:
            registration = Registration("cluster", name, target)
        registration = replace(
            registration,
            kinds=tuple(
                item
                for item in registration.kinds
                if item[1] not in {"Secret", "ConfigMap"}
            ),
        )
        delivery_actions = frozenset(
            {
                "inspect",
                "logs",
                "activity",
                "evaluate",
                "plan",
                "deploy",
                "rollback",
                "resume",
                "cancel",
            }
        )
        grants = {
            hashlib.sha256((oidc_issuer + "\0" + subject).encode()).hexdigest(): {
                registration.id: (
                    delivery_actions
                    if subject in (authorized_deploy_sub or ())
                    else frozenset({"inspect", "logs"})
                )
                | (
                    frozenset({"access"})
                    if subject in (authorized_access_sub or ())
                    else frozenset()
                )
            }
            for subject in authorized_sub
        }
        query = QueryService([registration], scope_policy=ScopePolicy(grants))
        logs = LogService(query)
        remote_access = RemoteAccessService(query) if authorized_access_sub else None
        operations = None
        if delivery_requested:
            assert source_root is not None
            assert source_file is not None
            assert renderer_image is not None
            assert renderer_platform is not None
            from piceli.services.cluster_evaluation import (
                KubernetesJobEvaluator,
                KubernetesRendererConfig,
            )
            from piceli.services.evaluation import SourceSelection, denied_paths
            from piceli.services.operations import OperationService
            from piceli.services.store import Store

            source_root = source_root.resolve(strict=True)
            state = control_dir.resolve()
            entrypoint = spec.model.release.composition
            module, separator, attribute = entrypoint.rpartition(":")
            if module.endswith(".py") or "/" in module:
                module = (
                    spec.resolve(Path(module))
                    .resolve(strict=True)
                    .relative_to(source_root)
                    .as_posix()
                )
                entrypoint = module + separator + attribute
            selection = SourceSelection(
                root=source_root,
                files=tuple(source_file),
                entrypoint=entrypoint,
                forbidden_paths=(*denied_paths(spec), state, ca_file, token_file),
            )
            operations = OperationService(
                query,
                Store(state / "operations.sqlite3"),
                KubernetesJobEvaluator(
                    state / "evaluations",
                    KubernetesRendererConfig(
                        image_id=renderer_image,
                        platform=renderer_platform,
                        target=target,
                    ),
                ),
                {registration.id: selection},
                principal=None,
            )
        security = ClusterSecurity(
            ClusterSecurityConfig(
                origin=origin,
                issuer=oidc_issuer,
                metadata_url=oidc_metadata_url,
                client_id=oidc_client_id,
                prefix=url_prefix,
            )
        )
        server = create_app(
            query,
            origin=origin,
            url_prefix=url_prefix,
            logs=logs,
            operations=operations,
            remote_access=remote_access,
            cluster_security=security,
        )
    except (ValueError, OSError):
        reject("ui-invalid-request")
    mode = "manual delivery" if operations is not None else "observation"
    if remote_access is not None:
        mode += " + local-client access"
    say(f"Piceli cluster UI: {origin}{url_prefix}/ ({mode})")
    uvicorn.run(server, host=host, port=port, log_level="warning")
