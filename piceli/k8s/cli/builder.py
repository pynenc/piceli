"""``piceli builder serve``: a build host builds what the GitOps controller waits for."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import emit_json, reject, say
from piceli.k8s.cli.cluster import JsonOption, TransportOption, connect, load_cluster
from piceli.k8s.cli.cluster import _guard as _cluster_guard
from piceli.k8s.cli.cluster_access import AllowExecOption, ExecSha256Option

app = typer.Typer(
    rich_markup_mode=None,
    help="Build hosts: build the images the GitOps controller waits for (0.21.0).",
    no_args_is_help=True,
)


@app.command("serve")
def serve(
    cluster: Annotated[
        str,
        typer.Option(
            "--cluster",
            envvar="PICELI_DEV_CLUSTER",
            help="MODULE:ATTR of the piceli.infra.Cluster whose controller to serve",
        ),
    ],
    host: Annotated[
        str | None,
        typer.Option(
            "--host",
            help="The docker build host to build on (DevBuilds(hosts=[...]); "
            "default: the first docker host)",
        ),
    ] = None,
    image: Annotated[
        str | None,
        typer.Option(
            "--image",
            help="The builder image (default: the host's, else DevBuilds(image=)); "
            "the same Piceli version as the controller",
        ),
    ] = None,
    namespace: Annotated[
        str, typer.Option("--namespace", help="The controller's namespace")
    ] = "piceli-system",
    once: Annotated[
        bool, typer.Option("--once", help="Build at most one wanted build, then exit")
    ] = False,
    poll: Annotated[
        float, typer.Option("--poll", min=5, help="Seconds between looks when idle")
    ] = 15.0,
    state_dir: Annotated[
        Path | None,
        typer.Option(
            "--state-dir",
            help="Where Git mirrors live (default ~/.cache/piceli/builder)",
        ),
    ] = None,
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
    as_json: JsonOption = False,
) -> None:
    """Take the builds the controller publishes, build and deliver them, answer it."""
    from piceli.dev.hosts import ContainerCli
    from piceli.dev.model import DevError
    from piceli.gitops.state import ConfigMapChannel
    from piceli.infra.builder_serve import (
        BuilderServe,
        BuildFailed,
        docker_container,
        ensure_image,
        node_importer,
        prepare_cache,
    )
    from piceli.pipeline.backend import Backend, RegistryRoute
    from piceli.profiles import override, resolve

    declared = load_cluster(cluster)
    dev = declared.dev
    hosts = [
        item for item in (dev.hosts if dev is not None else ()) if item.kind == "docker"
    ]
    chosen = next((item for item in hosts if host in (None, item.name)), None)
    if chosen is None:
        reject(
            "dev-host-unknown",
            f"no docker build host {host or ''} is declared: add "
            "DevBuilds(hosts=[BuildHost.docker(...)])",
        )
    from piceli.pipeline.model import ClusterRegistry

    registry = declared.registry
    nodes_delivery = (
        declared.controller is not None and declared.controller.delivery == "nodes"
    )
    if not isinstance(registry, ClusterRegistry) and not nodes_delivery:
        reject(
            "composition-invalid",
            f"{declared.name} needs registry=Registry.in_cluster(...): built images "
            'go there (or Controller(delivery="nodes") with Node(target=...))',
        )
    builder_image = image or chosen.image or (dev.image if dev is not None else None)
    if not builder_image:
        reject("cluster-build-invalid", "name the builder image with --image")
    cli = ContainerCli(chosen)
    try:
        cli.platform()
        ensure_image(cli, builder_image, say)
        prepare_cache(cli, builder_image)
    except DevError as error:
        reject(error.code, str(error))
    except BuildFailed as error:
        reject("dev-host-unavailable", str(error))
    profile = resolve(override() or declared.credentials)
    backend = Backend()
    push: Any = None
    index: Any = None
    if isinstance(registry, ClusterRegistry):
        route = RegistryRoute(
            push=None,
            node_registry=registry.host,
            forward=f"service/{registry.name}",
            namespace=registry.namespace,
            remote_port=registry.port,
            kubeconfig=profile.kubeconfig,
            context=profile.context,
        )

        def push(image_id: str, repository: str, archive: Path) -> str:
            delivered: dict[str, Any] = backend.registry_deliver(
                route, image_id, repository, archive=archive
            )
            if delivered.get("state") != "succeeded" or not delivered.get("pull_ref"):
                raise BuildFailed(
                    f"push of {repository} ended "
                    f"{delivered.get('result') or delivered.get('state')}"
                )
            return str(delivered["pull_ref"])

        def index(repository: str, digests: Any) -> str:
            return str(backend.registry_index(route, repository, digests))

    # 0.22.0: the nodes this host imports into (Controller(delivery="nodes")),
    # over its own SSH access (its keys, its agent).
    targets = {node.name: node.target for node in declared.nodes if node.target}
    agent = os.environ.get("SSH_AUTH_SOCK") or ""
    node_import = (
        node_importer(
            targets,
            backend,
            Path(agent) if agent and Path(agent).is_absolute() else None,
        )
        if targets
        else None
    )

    root = (state_dir or Path.home() / ".cache" / "piceli" / "builder").expanduser()
    from piceli.dev.health import Health

    health = Health.of(chosen, cli)

    def attest(repository: str, digest: str, body: bytes) -> Any:
        return backend.registry_attach(route, repository, digest, body)

    with connect(declared, transport, allow_exec, exec_sha256) as api, _cluster_guard():
        worker = BuilderServe(
            host=chosen.name,
            image=builder_image,
            channel=ConfigMapChannel(api.client, namespace),
            container=docker_container(cli, builder_image),
            push=push,
            index=index,
            root=root,
            say=say,
            node_import=node_import,
            health=health,
            attest=attest,
        )
        if once:
            built = worker.once()
            if as_json:
                emit_json(
                    {
                        "schema": "piceli.builder-serve.v1",
                        "built": built,
                        # 0.22.0: why nothing was claimed, when quarantined.
                        "quarantined": worker.quarantine,
                    }
                )
            return
        say(f"builder: serving {declared.name} from {chosen.name} (Ctrl-C stops)")
        try:
            worker.serve(poll=poll)
        except KeyboardInterrupt:
            say("builder: stopped")


def register(root: typer.Typer) -> None:
    root.add_typer(app, name="builder")
