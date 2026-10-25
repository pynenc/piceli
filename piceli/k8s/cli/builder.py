"""``piceli builder serve``: a build host builds what the GitOps controller waits for."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
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
    take_next: Annotated[
        bool,
        typer.Option(
            "--take-next",
            help="With --once: stand by for the next build this host may take "
            "(one waiting now, or the next one asked for), claim it before the "
            "in-cluster fallback, build it, then exit (0.29.0)",
        ),
    ] = False,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout", min=1, help="--take-next: seconds to stand by at most"
        ),
    ] = 1800.0,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Claim nothing: git ls-remote every source of the controller, "
            "write into the scratch directory and build cache from inside the "
            "builder container, list the NixReleases this host publishes, how "
            "each fetches its private flake inputs (credentials host or "
            "controller) and whether this host holds each credential, then exit "
            "(1 when anything fails)",
        ),
    ] = False,
    args_file: Annotated[
        Path | None,
        typer.Option(
            "--args-file",
            help="Read more builder serve arguments from this file at start, one "
            "per line (# comments); for a fixed service unit (NixOS) whose image "
            "and flags change without rewriting the unit (0.29.0)",
        ),
    ] = None,
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
    if args_file is not None:  # the entry point expands it (expand_args_file)
        reject("cluster-build-invalid", "--args-file is read by the piceli command")
    if take_next and not once:
        reject("cluster-build-invalid", "--take-next builds one build: add --once")
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
    from piceli.pipeline.model import ClusterRegistry
    from piceli.profiles import override, resolve

    declared = load_cluster(cluster)
    dev = declared.dev
    chosen, publish_only, images, builder_image = _select(
        declared, host, publish or [], publish_only, image
    )
    publisher = _publisher(publish or [], dev, chosen) if publish else None
    registry = declared.registry
    cli = ContainerCli(chosen) if chosen.kind == "docker" else None
    try:
        if images and cli is not None:
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
                current = channel.read_status() or {}
                releases = host_check(current, chosen.name)
            except Exception:  # a warning never stops the host from starting
                if check:
                    raise
                current, releases = {}, []
            missing = _say_inputs(releases, chosen.name, quiet=not check)
            if check:
                # 0.29.0: every source as the build fetches it, and a write
                # into the scratch directory and cache from inside the image.
                sources = _check_sources(current)
                scratch = (
                    _check_scratch(cli, str(builder_image))
                    if images and cli is not None
                    else None
                )
                failed = (
                    missing
                    or any(row["state"] != "reachable" for row in sources)
                    or (scratch is not None and scratch["state"] != "writable")
                )
                emit_json(
                    {
                        "schema": "piceli.builder-check.v1",
                        "host": chosen.name,
                        "releases": releases,
                        "sources": sources,
                        "scratch": scratch,
                        "complete": not failed,
                    }
                )
                raise typer.Exit(1 if failed else 0)
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
            built = worker.take_next(timeout=timeout) if take_next else worker.once()
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
        _stop_on_sigterm()
        try:
            worker.serve(poll=poll)
        except KeyboardInterrupt:
            say("builder: stopped")


def _check_sources(status: Any) -> list[dict[str, Any]]:
    """``--check`` (0.29.0): ``git ls-remote`` of every source the
    controller follows, said per source."""
    from piceli.infra.builder_serve import check_sources

    urls = {
        str(name): str((item or {}).get("url") or "")
        for name, item in ((status or {}).get("sources") or {}).items()
    }
    rows = check_sources(urls)
    for row in rows:
        say(
            f"builder: source {row['source']} {row['state']}"
            + (f" via {row['via']}" if row.get("via") else "")
            + (f": {row['detail']}" if row.get("detail") else "")
        )
    if not rows:
        say("builder: the controller lists no sources yet")
    return rows


def _check_scratch(cli: Any, image: str) -> dict[str, Any]:
    """``--check`` (0.29.0): the write test inside the builder container."""
    from piceli.infra.builder_serve import scratch_check

    found = scratch_check(cli, image)
    say(
        "builder: scratch directory and build cache "
        + (
            "writable from the builder container"
            if found["state"] == "writable"
            else f"NOT writable from the builder container: {found.get('detail')}"
        )
    )
    return found


def read_args_file(path: Path) -> list[str]:
    """The arguments in ``path`` (0.29.0, ``--args-file``): one per line,
    blank lines and ``#`` comments skipped; never another ``--args-file``."""
    try:
        text = path.read_text()
    except OSError as error:
        reject(
            "cluster-build-invalid",
            f"cannot read the arguments file {path} ({error.strerror})",
        )
    found = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if any(item == "--args-file" or item.startswith("--args-file=") for item in found):
        reject("cluster-build-invalid", "an arguments file names no --args-file")
    return found


def expand_args_file(argv: list[str]) -> list[str]:
    """``piceli builder serve ... --args-file PATH`` (0.29.0): ``argv`` with
    the option replaced by the file's arguments, after the command line's
    own (the entry point applies it before parsing); other argv unchanged."""
    if argv[:2] != ["builder", "serve"]:
        return argv
    kept: list[str] = []
    files: list[Path] = []
    items = iter(argv)
    for item in items:
        if item == "--args-file":
            value = next(items, None)
            if value is None:
                reject("cluster-build-invalid", "--args-file names no file")
            files.append(Path(value))
        elif item.startswith("--args-file="):
            files.append(Path(item.split("=", 1)[1]))
        else:
            kept.append(item)
    for path in files:
        kept += read_args_file(path.expanduser())
    return kept


def _stop_on_sigterm() -> None:
    """0.28.0: SIGTERM stops serve as Ctrl-C does (launchd stops a service
    with SIGTERM; the systemd unit sends SIGINT itself)."""
    import signal

    def stop(*_args: Any) -> None:
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, stop)
    except ValueError:  # not the main thread (tests)
        pass


def _select(
    declared: Any,
    host: str | None,
    publish: list[str],
    publish_only: bool | None,
    image: str | None,
) -> tuple[Any, bool, bool, str | None]:
    """The build host ``builder serve`` serves as, whether it only publishes,
    whether it builds images and the builder image; refused as serve
    refuses (shared by ``serve`` and ``install``)."""
    from piceli.pipeline.model import ClusterRegistry

    if publish_only and not publish:
        reject(
            "dev-nix-invalid",
            "--publish-only needs the caches it publishes to: --publish MODULE:ATTR",
        )
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
    # 0.25.0: --publish-only claims no image build on any kind of host.
    images = chosen.kind == "docker" and not publish_only
    registry = declared.registry
    nodes_delivery = (
        declared.controller is not None and declared.controller.delivery == "nodes"
    )
    if not isinstance(registry, ClusterRegistry) and not nodes_delivery:
        if not publish:
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
    return chosen, publish_only, images, builder_image


ClusterOption = Annotated[
    str,
    typer.Option(
        "--cluster",
        envvar="PICELI_DEV_CLUSTER",
        help="MODULE:ATTR of the piceli.infra.Cluster whose controller to serve",
    ),
]
NamespaceOption = Annotated[
    str, typer.Option("--namespace", help="The controller's namespace")
]


@app.command("install")
def install(
    cluster: ClusterOption,
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="The declared build host this machine is (one service per name)",
        ),
    ],
    publish: Annotated[
        list[str] | None,
        typer.Option(
            "--publish",
            help="MODULE:ATTR of a piceli.infra.NixCache this host publishes to "
            "(repeat); its keys must be in this machine's piceli secrets",
        ),
    ] = None,
    publish_only: Annotated[
        bool | None,
        typer.Option(
            "--publish-only/--no-publish-only",
            help="Passed on to builder serve",
        ),
    ] = None,
    image: Annotated[
        str | None,
        typer.Option("--image", help="The builder image (passed on to builder serve)"),
    ] = None,
    namespace: NamespaceOption = "piceli-system",
    user: Annotated[
        str | None,
        typer.Option(
            "--user",
            help="As root: the user the service runs as (a system service); "
            "without root the service runs as you (a user service)",
        ),
    ] = None,
    state_dir: Annotated[
        Path | None,
        typer.Option(
            "--state-dir",
            help="builder serve's state directory (Git mirrors; launchd's "
            "builder.log); default ~/.cache/piceli/builder of the service user",
        ),
    ] = None,
    print_only: Annotated[
        bool,
        typer.Option(
            "--print",
            help="Write the unit (or property list) to stdout and change nothing",
        ),
    ] = False,
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
) -> None:
    """Run builder serve as a service on this machine (systemd or launchd)."""
    from piceli import __version__
    from piceli.infra import builder_service as bs

    declared = load_cluster(cluster)
    chosen, _publish_only, _images, _image = _select(
        declared, host, publish or [], publish_only, image
    )
    serve_args = ["--cluster", cluster, "--host", chosen.name]
    for ref in publish or []:
        serve_args += ["--publish", ref]
    if publish_only is not None:
        serve_args.append("--publish-only" if publish_only else "--no-publish-only")
    if image:
        serve_args += ["--image", image]
    if namespace != "piceli-system":
        serve_args += ["--namespace", namespace]
    if transport != "https":
        serve_args += ["--transport", transport]
    if allow_exec:
        serve_args.append("--allow-exec")
    if exec_sha256:
        serve_args += ["--exec-sha256", exec_sha256]
    machine = bs.Machine.current()
    tools = bs.TOOLS if chosen.kind == "docker" else ("git", "kubectl")
    try:
        service = bs.plan(
            machine,
            host=chosen.name,
            serve=serve_args,
            user=user,
            state_dir=state_dir.expanduser().absolute() if state_dir else None,
            cwd=Path.cwd(),
            version=str(__version__),
            tools=tools,
        )
    except bs.ServiceError as error:
        reject(error.code, str(error))
    text = bs.render(service)
    if print_only:
        # 0.29.0: printed whatever this machine holds (the unit may be for
        # another machine, or NixOS writes it); the checks run where it runs.
        say(
            "builder: --print checks nothing; run piceli builder serve --check "
            "where the service runs"
        )
        typer.echo(text, nl=False)
        return
    # The same checks as builder serve --check, as the service will run:
    # the cache keys, the profile and kubeconfig reach the controller, and
    # this host holds every credential it needs.
    with _service_env(service.environment if service.scope == "system" else {}):
        _check_caches(publish or [])
        status = _read_status(declared, namespace, transport, allow_exec, exec_sha256)
        _check_credentials(status or {}, chosen.name)
    try:
        commands = bs.install(machine, service)
    except bs.ServiceError as error:
        reject(error.code, str(error))
    say(
        f"builder: {service.manager} {service.scope} service {service.name} "
        f"installed at {service.path} and started"
    )
    for note in service.notes:
        say(f"builder: note: {note}")
    say(
        "builder: logs: "
        + (
            f"journalctl {'--user ' if service.scope == 'user' else ''}"
            f"-u {service.name} -f"
            if service.manager == "systemd"
            else f"tail -f {service.log}"
        )
    )
    emit_json(
        {
            "schema": "piceli.builder-install.v1",
            "state": "installed",
            "host": service.host,
            "manager": service.manager,
            "scope": service.scope,
            "name": service.name,
            "path": str(service.path),
            "user": service.user,
            "version": service.version,
            "argv": list(service.argv),
            "commands": commands,
            "notes": service.notes,
        }
    )


@app.command("uninstall")
def uninstall(
    host: Annotated[str, typer.Option("--host", help="The build host's service")],
) -> None:
    """Stop, disable and remove the build host service builder install wrote."""
    from piceli.infra import builder_service as bs

    try:
        removed = bs.uninstall(bs.Machine.current(), host)
    except bs.ServiceError as error:
        reject(error.code, str(error))
    say(
        f"builder: service of {host} stopped and removed ({removed})"
        if removed
        else f"builder: no service of {host} is installed here"
    )
    emit_json(
        {
            "schema": "piceli.builder-uninstall.v1",
            "state": "removed" if removed else "absent",
            "host": host,
            "path": str(removed) if removed else None,
        }
    )


@app.command("status")
def status(
    cluster: ClusterOption,
    namespace: NamespaceOption = "piceli-system",
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
    as_json: JsonOption = False,
) -> None:
    """List the build hosts the controller heard from and what each last did."""
    from piceli.infra import build_host_status

    declared = load_cluster(cluster)
    dev = declared.dev
    names = [
        item.name
        for item in (dev.hosts if dev is not None else ())
        if item.kind in ("docker", "native")
    ]
    found = _read_status(declared, namespace, transport, allow_exec, exec_sha256)
    if found is None:
        reject("gitops-not-installed", "no GitOps controller status")
    document = build_host_status.view(found, names)
    for row in document["hosts"]:
        if row["state"] == "never-seen":
            say(
                f"{row['host']:<16} never seen"
                + (
                    " (declared; is builder serve running there? "
                    "piceli builder install --host " + row["host"] + ")"
                    if row["declared"]
                    else ""
                )
            )
            continue
        claim = row["claim"]
        last = row["last_build"] or {}
        line = (
            f"{row['host']:<16} {row['state']:<13} "
            f"Piceli {row['version'] or '?':<8} seen {row['last_seen']}"
        )
        if claim:
            line += f"; claim {str(claim['build'])[:12]} {claim['state']}" + (
                f" ({claim['stage']})" if claim.get("stage") else ""
            )
        if last:
            line += f"; last build {str(last.get('id'))[:12]} {last.get('outcome')}"
        if row["quarantined"]:
            quarantine = row["quarantine"]
            line += (
                f"; QUARANTINED since {quarantine['since']}: {quarantine['reason']} "
                f"(piceli gitops release-host {row['host']})"
            )
        say(line)
    if as_json:
        emit_json(document)


@contextmanager
def _service_env(environment: dict[str, str]) -> Iterator[None]:
    """The service user's ``HOME`` and Piceli directories while checking
    (a system service installed by root runs as ``--user``)."""
    from piceli.infra.builder_service import PASSED_ENV

    names = ("HOME", *PASSED_ENV)
    before = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            if name in environment:
                os.environ[name] = environment[name]
        yield
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _read_status(
    declared: Any,
    namespace: str,
    transport: str,
    allow_exec: bool,
    exec_sha256: str | None,
) -> dict[str, Any] | None:
    """The controller's published status, through the cluster's profile."""
    from piceli.gitops.state import ConfigMapChannel

    with connect(declared, transport, allow_exec, exec_sha256) as api, _cluster_guard():
        return ConfigMapChannel(api.client, namespace).read_status()


def _check_caches(refs: list[str]) -> None:
    """Every ``--publish`` cache's keys are on this machine (as serve checks
    at start), without starting any container."""
    from piceli.dev.model import DevError
    from piceli.infra.machines.model import InfraError
    from piceli.infra.nix_releases import cache_keys
    from piceli.k8s.cli.dev_nix import load_cache

    for ref in refs:
        cache = load_cache(ref)
        if cache.scheme == "https":
            reject(
                "dev-nix-cache-invalid",
                "an https:// cache is read-only; publish to its s3:// or file:// URL",
            )
        try:
            cache_keys(cache)
        except (DevError, InfraError) as error:
            reject(error.code, str(error))


def _check_credentials(status: dict[str, Any], host: str) -> None:
    """Refuse when ``host`` lacks a credential the NixReleases it publishes
    need (``builder serve --check``), naming each and how to add it."""
    from piceli.dev.nix_inputs import host_check

    lacking = [
        (release["release"], item)
        for release in host_check(status, host)
        for item in release["inputs"]
        if item["state"] in ("missing", "unusable")
    ]
    if lacking:
        reject(
            "infra-credential-missing",
            f"build host {host} lacks "
            + "; ".join(
                f"credential {item['credential']} ({item['state']}) for input "
                f"{item['input']} of nix release {release}: piceli secrets "
                f"git-token {item['credential']} --host {host} --prompt"
                for release, item in lacking
            ),
        )


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
