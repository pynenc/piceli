"""Local web application with explicit target registration."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tarfile
import threading
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Literal, cast
from urllib.parse import urlsplit

import typer

from piceli.cli_contract import emit_json, reject, say
from piceli.k8s.cli.ui_forward import forward_serve
from piceli.k8s.cli.ui_remote import connect as connect_remote

app = typer.Typer(rich_markup_mode=None, help="Piceli delivery web application.")

app.command("connect")(connect_remote)
app.command("forward-serve")(forward_serve)


@app.command("backup")
def backup_ui(
    control_dir: Annotated[Path, typer.Option(help="Stopped UI control directory")],
    output: Annotated[Path, typer.Option(help="New private backup archive")],
) -> None:
    """Back up an offline UI control store and its release journal."""
    from piceli.server.state_archive import backup

    try:
        created = backup(control_dir, output)
    except (ValueError, OSError):
        reject("ui-state-invalid")
    say(f"UI state backup: {created}")
    emit_json({"state": "backed-up", "archive": str(created)})


@app.command("restore")
def restore_ui(
    archive: Annotated[Path, typer.Option(help="Verified UI backup archive")],
    destination: Annotated[Path, typer.Option(help="Empty new control directory")],
) -> None:
    """Restore UI control state before starting a single new server."""
    from piceli.server.state_archive import restore

    try:
        restored = restore(archive, destination)
    except (ValueError, OSError, tarfile.TarError):
        reject("ui-state-invalid")
    say(f"UI state restored: {restored}")
    emit_json({"state": "restored", "destination": str(restored)})


@app.command()
def serve(
    kubeconfig: Annotated[
        Path | None, typer.Option(help="Explicit kubeconfig file")
    ] = None,
    context: Annotated[
        str | None, typer.Option(help="Explicit kubeconfig context")
    ] = None,
    namespace: Annotated[str | None, typer.Option(help="Inventory namespace")] = None,
    profile: Annotated[
        str | None,
        typer.Option(help="Local credential profile (piceli profiles --json)"),
    ] = None,
    definition: Annotated[
        Path | None, typer.Option(help="Existing release TOML definition")
    ] = None,
    pipeline: Annotated[
        str | None,
        typer.Option(help="Trusted Pipeline MODULE:ATTR configured by the UI owner"),
    ] = None,
    gitops_namespace: Annotated[
        str | None,
        typer.Option(help="Show the GitOps controller in this explicit namespace"),
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
    """Register an existing definition or inventory scope and serve the bundled UI.

    Release definitions and configured Pipelines can include pre-rollout checks.
    """
    if profile is not None:
        if kubeconfig is not None or context is not None:
            reject("profile-conflict")
        from piceli.profiles import ProfileError, resolve, set_override

        try:
            selected = resolve(profile)
        except ProfileError as error:
            reject(error.code)
        if pipeline is None and definition is None:
            kubeconfig, context = selected.kubeconfig, selected.context
            namespace = namespace or "default"
        else:
            set_override(profile)
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
        declared_pipeline = None
        if pipeline is not None:
            if (
                definition is not None
                or any(value is not None for value in (kubeconfig, context, namespace))
                or allow_exec
                or exec_sha256
                or any(
                    value is not None
                    for value in (
                        source_root,
                        source_file,
                        renderer_image,
                        renderer_platform,
                        docker,
                    )
                )
            ):
                reject("ui-invalid-request")
            from piceli.app.render import load_target
            from piceli.pipeline import Pipeline

            declared_pipeline = load_target(pipeline, Path.cwd())
            if (
                not isinstance(declared_pipeline, Pipeline)
                or declared_pipeline.needs_environment
            ):
                reject("ui-invalid-request")
            target_config = declared_pipeline.target
            target = KubeconfigTarget(
                kubeconfig=target_config.kubeconfig.absolute(),
                context=target_config.context,
                namespace=target_config.namespace,
                cluster_uid=target_config.cluster_uid,
                namespace_uid=target_config.namespace_uid,
                transport=cast(
                    Literal["https", "loopback-http"], target_config.transport
                ),
                allow_exec=target_config.allow_exec,
                exec_sha256=target_config.exec_sha256,
                exec_pass_env=tuple(target_config.exec_pass_env),
                exec_timeout_seconds=target_config.exec_timeout_seconds or 60,
            )
            registration = Registration(
                id="my-app",
                name=name,
                target=target,
                definition_kind="pipeline",
                ownership="native",
            )
        elif definition is not None:
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
        if delivery_requested and declared_pipeline is None:
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
        control = None
        if declared_pipeline is not None or gitops_namespace is not None:
            from piceli.services.environment_control import EnvironmentControl

            control = EnvironmentControl(
                query,
                registration.id,
                pipeline=declared_pipeline
                if declared_pipeline is not None and declared_pipeline.envs is not None
                else None,
                controller_target=registration.target,
                controller_namespace=gitops_namespace or "piceli-system",
            )
        pipeline_delivery = None
        if declared_pipeline is not None:
            from piceli.services.pipeline_control import PipelineControl

            pipeline_delivery = PipelineControl(
                query,
                registration.id,
                declared_pipeline,
                pipeline or "",
                (control_dir or declared_pipeline.state_dir / "ui-control").resolve(),
            )
        requested_profile: list[str] = []
        runner: uvicorn.Server | None = None

        def switch_session(selected_name: str) -> None:
            # Stop the listener and close its access/session services before
            # a fresh process opens credentials for the selected profile.
            requested_profile[:] = [selected_name]
            if runner is not None:
                timer = threading.Timer(
                    0.2, lambda: setattr(runner, "should_exit", True)
                )
                timer.daemon = True
                timer.start()

        server = create_app(
            query,
            origin=origin,
            url_prefix=url_prefix,
            operations=operations,
            environment_control=control,
            pipeline_control=pipeline_delivery,
            access=access,
            logs=logs,
            active_profile=profile,
            profile_switch=switch_session,
        )
        from piceli.k8s.ui_state import remove_launch_token, write_launch_token
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
        runner = uvicorn.Server(
            uvicorn.Config(
                server,
                host=host,
                port=port,
                log_level="warning",
                log_config=uvicorn_log_config(),
            )
        )
        runner.run()
    finally:
        remove_launch_token(token_file)
    if requested_profile:
        # The old ASGI lifespan has finished: no forward or session survives.
        # Keep the owner's other trusted flags, replacing only --profile.
        argv = sys.argv[1:]
        cleaned: list[str] = []
        index = 0
        while index < len(argv):
            token = argv[index]
            if token == "--profile":
                index += 2
                continue
            if token.startswith("--profile="):
                index += 1
                continue
            cleaned.append(token)
            index += 1
        if cleaned[:2] != ["ui", "serve"]:
            reject("ui-operation-unavailable")
        os.execv(
            sys.executable,
            [
                sys.executable,
                "-m",
                "piceli",
                *cleaned,
                "--profile",
                requested_profile[0],
            ],
        )


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
    gitops_namespace: Annotated[
        str | None,
        typer.Option(help="Controller namespace for scoped GitOps status and requests"),
    ] = None,
    grant_file: Annotated[
        Path | None, typer.Option(help="Projected operator-owned grant map")
    ] = None,
    build_spec: Annotated[
        str | None, typer.Option(help="Trusted relative host-build.toml path in Git")
    ] = None,
    build_image: Annotated[
        str | None, typer.Option(help="Pinned cluster builder image")
    ] = None,
    build_repo: Annotated[
        str | None, typer.Option(help="Credential-free Git remote URL")
    ] = None,
    build_registry_url: Annotated[
        str | None, typer.Option(help="Node registry OCI URL")
    ] = None,
    build_git_secret: Annotated[str, typer.Option()] = "piceli-build-git",
    build_cache_size: Annotated[str, typer.Option()] = "20Gi",
    build_node_arch: Annotated[str, typer.Option()] = "amd64",
    build_platform: Annotated[list[str] | None, typer.Option()] = None,
    name: Annotated[str, typer.Option(help="Application display name")] = "cluster",
    host: Annotated[
        str, typer.Option(help="Loopback bind for a TLS gateway sidecar")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
    url_prefix: Annotated[str, typer.Option()] = "",
    experimental: Annotated[
        bool,
        typer.Option(help="Deprecated compatibility option; no longer required"),
    ] = False,
) -> None:
    """Serve OIDC-scoped cluster observation and configured delivery."""
    if (
        host not in {"127.0.0.1", "::1"}
        or not authorized_sub
        or len(authorized_sub) > 100
        or any(not subject or len(subject) > 256 for subject in authorized_sub)
        or not all(path.is_absolute() for path in (ca_file, token_file, control_dir))
        or (grant_file is not None and not grant_file.is_absolute())
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
    build_values = (build_spec, build_image, build_repo, build_registry_url)
    if any(value is not None for value in build_values) and (
        not all(value is not None for value in build_values)
        or not authorized_deploy_sub
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
    stage = "origin"
    try:
        if not origin.startswith("https://"):
            reject("ui-invalid-request")
        stage = "credential"
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
        stage = "identity"
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
            stage = "definition"
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
        if grant_file is not None:
            from piceli.services.authority import FileScopePolicy

            policy = FileScopePolicy(grant_file, registration.id)
        else:
            policy = ScopePolicy(grants)
        query = QueryService([registration], scope_policy=policy)
        logs = LogService(query)
        remote_access = RemoteAccessService(query) if authorized_access_sub else None
        operations = None
        cluster_build_control = None
        if delivery_requested:
            stage = "renderer"
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
                        egress_probe=(
                            urlsplit(api_server).hostname or "",
                            urlsplit(api_server).port or 443,
                        ),
                    ),
                ),
                {registration.id: selection},
                principal=None,
            )
            if build_spec is not None:
                stage = "build"
                assert build_image is not None
                assert build_repo is not None
                assert build_registry_url is not None
                from piceli.artifacts.cluster_build import ClusterBuildConfig
                from piceli.services.cluster_build_control import (
                    ClusterBuildControl,
                    ClusterBuildProfile,
                )

                build_config = ClusterBuildConfig(
                    image=build_image,
                    repo=build_repo,
                    git_secret=build_git_secret,
                    namespace=namespace,
                    registry_url=build_registry_url,
                    storage=build_cache_size,
                    selector={
                        "piceli.io/builder": "true",
                        "kubernetes.io/arch": build_node_arch,
                    },
                    repo_root=state / "build-specs",
                    service_account="piceli-builder",
                )
                cluster_build_control = ClusterBuildControl(
                    query,
                    registration.id,
                    ClusterBuildProfile(
                        build_spec,
                        build_config,
                        tuple(build_platform or ("linux/amd64",)),
                    ),
                    operations.store,
                    state,
                )
        stage = "security"
        security = ClusterSecurity(
            ClusterSecurityConfig(
                origin=origin,
                issuer=oidc_issuer,
                metadata_url=oidc_metadata_url,
                client_id=oidc_client_id,
                prefix=url_prefix,
            )
        )
        control = None
        if gitops_namespace is not None:
            from piceli.services.environment_control import EnvironmentControl

            control = EnvironmentControl(
                query,
                registration.id,
                controller_target=target,
                controller_namespace=gitops_namespace,
            )
        stage = "app"
        server = create_app(
            query,
            origin=origin,
            url_prefix=url_prefix,
            logs=logs,
            operations=operations,
            remote_access=remote_access,
            cluster_build_control=cluster_build_control,
            environment_control=control,
            cluster_security=security,
        )
    except (ValueError, OSError) as error:
        code = getattr(error, "code", "")
        say(f"Piceli cluster UI startup: {stage} {type(error).__name__} {code}")
        reject("ui-invalid-request")
    mode = "manual delivery" if operations is not None else "observation"
    if remote_access is not None:
        mode += " + local-client access"
    say(f"Piceli cluster UI: {origin}{url_prefix}/ ({mode})")
    uvicorn.run(server, host=host, port=port, log_level="warning")
