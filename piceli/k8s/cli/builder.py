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
            "default: the first docker host); with --publish also a native "
            "host, which then only publishes (as any host with --publish-only)",
        ),
    ] = None,
    publish: Annotated[
        list[str] | None,
        typer.Option(
            "--publish",
            help="MODULE:ATTR of a piceli.infra.NixCache this host publishes "
            "NixReleases to (repeat); its signing key and S3 write keys must "
            "be in this machine's piceli secrets (checked at start, never sent)",
        ),
    ] = None,
    publish_only: Annotated[
        bool | None,
        typer.Option(
            "--publish-only/--no-publish-only",
            help="Claim only the --publish NixReleases, never an image build, "
            "whatever the host's kind (a docker host that only publishes); "
            "default: on for a host declared runs=False, else off",
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
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Claim nothing: list the NixReleases this host publishes, how "
            "each fetches its private flake inputs (credentials host or "
            "controller) and whether this host holds each credential, then exit "
            "(1 when one is missing)",
        ),
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

    if publish_only and not publish:
        reject(
            "dev-nix-invalid",
            "--publish-only needs the caches it publishes to: --publish MODULE:ATTR",
        )
    declared = load_cluster(cluster)
    dev = declared.dev
    kinds = ("docker", "native") if publish else ("docker",)
    hosts = [
        item for item in (dev.hosts if dev is not None else ()) if item.kind in kinds
    ]
    # 0.25.0: the default host of image builds is never one declared
    # runs=False (it only publishes); --host names it.
    chosen = next(
        (
            item
            for item in hosts
            if item.name == host or (host is None and (item.runs or publish))
        ),
        None,
    )
    if chosen is None:
        reject(
            "dev-host-unknown",
            f"no docker build host {host or ''} is declared: add "
            "DevBuilds(hosts=[BuildHost.docker(...)])",
        )
    if not chosen.runs and (publish_only is False or not publish):
        reject(
            "dev-host-publish-only",
            f"build host {chosen.name} is declared runs=False: it only publishes "
            "Nix releases. Run builder serve on it with --publish MODULE:ATTR "
            "(it then implies --publish-only), and build images on another host",
        )
    if publish_only is None:
        publish_only = not chosen.runs
    publisher = _publisher(publish or [], dev, chosen) if publish else None
    # 0.25.0: --publish-only claims no image build on any kind of host.
    images = chosen.kind == "docker" and not publish_only
    from piceli.pipeline.model import ClusterRegistry

    registry = declared.registry
    nodes_delivery = (
        declared.controller is not None and declared.controller.delivery == "nodes"
    )
    if not isinstance(registry, ClusterRegistry) and not nodes_delivery:
        if publisher is None:
            reject(
                "composition-invalid",
                f"{declared.name} needs registry=Registry.in_cluster(...): built "
                'images go there (or Controller(delivery="nodes") with '
                "Node(target=...))",
            )
        images = False  # 0.25.0: nowhere to deliver images; it only publishes
    builder_image = image or chosen.image or (dev.image if dev is not None else None)
    if images and not builder_image:
        reject("cluster-build-invalid", "name the builder image with --image")
    cli = ContainerCli(chosen) if chosen.kind == "docker" else None
    try:
        if images and cli is not None and not check:
            cli.platform()
            ensure_image(cli, str(builder_image), say)
            prepare_cache(cli, str(builder_image))
    except DevError as error:
        reject(error.code, str(error))
    except BuildFailed as error:
        reject("dev-host-unavailable", str(error))
    profile = resolve(override() or declared.credentials)
    backend = Backend()
    push: Any = None
    index: Any = None
    if images and isinstance(registry, ClusterRegistry):
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

    health = Health.of(chosen, cli) if cli is not None else None

    def attest(repository: str, digest: str, body: bytes) -> Any:
        return backend.registry_attach(route, repository, digest, body)

    with connect(declared, transport, allow_exec, exec_sha256) as api, _cluster_guard():
        channel = ConfigMapChannel(api.client, namespace)
        if check or publisher is not None:
            from piceli.dev.nix_inputs import host_check

            try:
                releases = host_check(channel.read_status() or {}, chosen.name)
            except Exception:  # a warning never stops the host from starting
                if check:
                    raise
                releases = []
            missing = _say_inputs(releases, chosen.name, quiet=not check)
            if check:
                emit_json(
                    {
                        "schema": "piceli.builder-check.v1",
                        "host": chosen.name,
                        "releases": releases,
                        "complete": not missing,
                    }
                )
                raise typer.Exit(1 if missing else 0)
        worker = BuilderServe(
            host=chosen.name,
            image=str(builder_image or ""),
            channel=channel,
            container=docker_container(cli, str(builder_image))
            if cli is not None
            else _no_container,
            push=push,
            index=index,
            root=root,
            say=say,
            node_import=node_import,
            health=health,
            attest=attest,
            publisher=publisher,
            images=images,
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
        say(
            f"builder: serving {declared.name} from {chosen.name}"
            + ("" if images else ", publishing NixReleases only")
            + (
                f"; publishes to {', '.join(c.store_url for c in publisher.caches)}"
                if publisher is not None
                else ""
            )
            + " (Ctrl-C stops)"
        )
        try:
            worker.serve(poll=poll)
        except KeyboardInterrupt:
            say("builder: stopped")


def _say_inputs(releases: list[Any], host: str, *, quiet: bool) -> bool:
    """Say, per release, the private inputs this host has no credential for
    (0.26.0; with ``quiet`` only those); whether one is missing."""
    missing = False
    for release in releases:
        lacking = [
            i for i in release["inputs"] if i["state"] in ("missing", "unusable")
        ]
        missing = missing or bool(lacking)
        for item in lacking:
            say(
                f"builder: warning: nix release {release['release']} fetches input "
                f"{item['input']} {item['url']} with credential {item['credential']}, "
                f"which {host} does not hold ({item['state']}): piceli secrets "
                f"git-token {item['credential']} --host HOST --prompt, or restore it"
            )
        if not quiet and not lacking:
            inputs = ", ".join(
                f"{i['input']} ({i['credential']}: {i['state']})"
                for i in release["inputs"]
            )
            say(
                f"builder: nix release {release['release']} (credentials "
                f"{release['credentials']}): "
                + (
                    inputs
                    or (
                        "no private inputs" if release["planned"] else "not planned yet"
                    )
                )
            )
    return missing


def _no_container(*_args: Any) -> int:  # a native host builds no image
    from piceli.infra.builder_serve import BuildFailed

    raise BuildFailed("this build host builds no images")


def _publisher(refs: list[str], dev: Any, chosen: Any) -> Any:
    """The NixRelease publisher of ``--publish`` (0.25.0): every cache's keys
    are read now, so a missing or wrong key refuses before any claim."""
    from piceli.dev import nix_inputs
    from piceli.dev.model import DevError
    from piceli.dev.nix_step import check_image, step_image
    from piceli.infra import nix_releases
    from piceli.infra.machines.model import InfraError
    from piceli.infra.nix_releases import Publisher, dev_build
    from piceli.k8s.cli.dev_nix import load_cache

    caches = [load_cache(ref) for ref in refs]
    for cache in caches:
        if cache.scheme == "https":
            reject(
                "dev-nix-cache-invalid",
                "an https:// cache is read-only; publish to its s3:// or file:// URL",
            )
    declared = next((item for item in dev.profiles if item.nix), None)
    if declared is None:
        reject(
            "dev-nix-invalid",
            "publishing builds with the host's Nix: declare DevProfile.for_nix()",
        )
    try:
        # 0.26.1: the profile with DevBuilds' defaults (its image), and the
        # Nix steps in the image the build run uses, checked now.
        profile = dev.profile(declared.name)
        image = step_image(chosen, profile)
        if chosen.kind == "docker":
            check_image(chosen, image)
        return Publisher(
            caches,
            build=dev_build(dev, chosen, profile),
            push=nix_releases.dev_push(chosen, image),
            host=chosen.name,
            # 0.26.0: private flake inputs, fetched on this host first.
            fetch=nix_inputs.fetch_inputs(chosen, image),
        )
    except (DevError, InfraError) as error:
        reject(error.code, str(error))


def register(root: typer.Typer) -> None:
    root.add_typer(app, name="builder")
