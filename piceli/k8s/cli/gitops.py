"""``piceli gitops …`` and ``piceli promote``: Piceli's own GitOps controller.

- ``gitops enable PIPELINE --repo URL --branches 'main,wp-*' --image REF@sha256:…``
  plans the controller's install (exit 3 with the plan hash) and installs it
  with ``--approve HASH``.
- ``gitops disable`` plans (exit 3) and, with ``--approve HASH``, removes the
  controller; never an environment.
- ``gitops status`` shows the controller's health and every branch's commit,
  state and pending approval.
- ``gitops approve ENV HASH`` releases a deploy waiting for approval.
- ``promote BRANCH@SHA`` asks the controller to deploy that commit to main
  (it then waits for ``gitops approve`` unless the owner allowed otherwise).
- ``gitops run`` is the controller itself (the Deployment's entrypoint), or
  one poll locally with ``--once``.

Cluster commands take an explicit ``--kubeconfig`` and ``--context``; the
local commands take ``--state-dir`` instead. Machine output is one JSON
object on stdout; human text on stderr.

Importing this module is side-effect free.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import EXIT_APPROVAL, emit_json, reject, say
from piceli.k8s.cli.cluster_access import AllowExecOption, ExecSha256Option, exec_policy

app = typer.Typer(
    rich_markup_mode=None,
    help="Piceli's GitOps controller: deploy branches from Git, one environment each.",
    no_args_is_help=True,
)

KubeconfigOption = Annotated[
    Path | None,
    typer.Option(
        "--kubeconfig",
        help="Explicit kubeconfig file (never ~/.kube/config or KUBECONFIG)",
    ),
]
ContextOption = Annotated[
    str | None,
    typer.Option("--context", help="Kubeconfig context (required with --kubeconfig)"),
]
NamespaceOption = Annotated[
    str, typer.Option("--namespace", help="The controller's namespace")
]
TransportOption = Annotated[
    str,
    typer.Option("--transport", help="https, or loopback-http for a local test API"),
]
StateDirOption = Annotated[
    Path | None,
    typer.Option(
        "--state-dir",
        help="A local controller's state directory instead of a cluster "
        "(the one `gitops run --once --state-dir` uses)",
    ),
]
JsonOption = Annotated[
    bool, typer.Option("--json", help="Print one JSON object on stdout")
]


@contextmanager
def _guard() -> Iterator[None]:
    from piceli.gitops import GitOpsError

    try:
        yield
    except GitOpsError as error:
        reject(error.code, str(error))


def _target(kubeconfig: Path | None, context: str | None) -> tuple[Path, str]:
    if kubeconfig is None or not context:
        reject(
            "gitops-target-required",
            "name the cluster with --kubeconfig FILE --context NAME "
            "(or a local controller with --state-dir)",
        )
    return kubeconfig, context


@contextmanager
def _api(
    kubeconfig: Path | None,
    context: str | None,
    transport: str,
    allow_exec: bool = False,
    exec_sha256: str | None = None,
) -> Iterator[Any]:
    from piceli.gitops.install import connect

    path, name = _target(kubeconfig, context)
    policy = exec_policy(allow_exec, exec_sha256)
    with _guard(), connect(path, name, transport=transport, exec_policy=policy) as api:
        yield api


@contextmanager
def _channel(
    state_dir: Path | None,
    kubeconfig: Path | None,
    context: str | None,
    namespace: str,
    transport: str,
) -> Iterator[Any]:
    from piceli.gitops.state import DirectoryChannel

    if state_dir is not None:
        if kubeconfig is not None:
            reject(
                "gitops-target-required",
                "--state-dir and --kubeconfig name two controllers; pass one",
            )
        yield DirectoryChannel(state_dir)
        return
    from piceli.gitops.install import CONFIG_MAP
    from piceli.gitops.state import ConfigMapChannel

    with _api(kubeconfig, context, transport) as api:
        found = api.call(
            f"/api/v1/namespaces/{namespace}/configmaps/{CONFIG_MAP}", "GET"
        )
        if found is None:
            reject(
                "gitops-not-installed",
                f"no GitOps controller in namespace {namespace}; install it with "
                "piceli gitops enable",
            )
        yield ConfigMapChannel(api.client, namespace)


def _describe(plan: Any) -> None:
    say(f"gitops {plan.action} plan:")
    for item in plan.objects:
        where = (
            f" in {item.manifest['metadata']['namespace']}"
            if "namespace" in item.manifest["metadata"]
            else ""
        )
        say(
            f"  {item.operation:8} {item.manifest['kind']}/{item.manifest['metadata']['name']}{where}"
        )
    say(f"plan hash: {plan.plan_hash}")


def _run_plan(
    api: Any,
    plan: Any,
    approve: str | None,
    command: str,
    extra: dict[str, Any],
) -> None:
    from piceli.gitops.install import execute

    _describe(plan)
    body = {**plan.to_dict(), **extra}
    if not plan.changes:
        say("nothing to change")
        emit_json({**body, "state": "unchanged"})
        return
    if approve is None:
        say("approve with:")
        say(f"  {command} --approve {plan.plan_hash}")
        emit_json(
            {
                **body,
                "state": "approval-required",
                "approve_command": f"{command} --approve {plan.plan_hash}",
            }
        )
        raise typer.Exit(EXIT_APPROVAL)
    if approve != plan.plan_hash:
        reject(
            "gitops-plan-changed",
            "the plan changed since it was approved (or the hash is wrong); "
            "review the new plan above",
        )
    with _guard():
        done = execute(api, plan, say)
    emit_json(
        {
            **body,
            "state": "enabled" if plan.action == "enable" else "disabled",
            "done": done,
        }
    )


@app.command("enable")
def enable(
    pipeline: Annotated[
        str,
        typer.Argument(
            help="The Pipeline in the repository: path/to/file.py:ATTR (relative "
            "to the repository root)",
            show_default=False,
        ),
    ],
    repo: Annotated[
        str,
        typer.Option(
            "--repo",
            help="Git URL the controller polls (https://, ssh://, git@host:path); "
            "no credentials in it",
        ),
    ],
    image: Annotated[
        str,
        typer.Option(
            "--image",
            help="The Piceli image the controller runs, pinned by digest "
            "(registry/repo@sha256:…); see docs/gitops.md to build one",
        ),
    ],
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    branches: Annotated[
        str,
        typer.Option(
            "--branches", help="Comma-separated branch globs, e.g. 'main,wp-*'"
        ),
    ] = "main",
    poll: Annotated[
        str, typer.Option("--poll", help="Poll interval: 60, 60s, 5m")
    ] = "60s",
    credentials_secret: Annotated[
        str | None,
        typer.Option(
            "--credentials-secret",
            help="Secret in the controller's namespace with the Git credentials "
            "(username/password or ssh-privatekey/known_hosts); mounted, never read",
        ),
    ] = None,
    namespace: NamespaceOption = "piceli-system",
    env: Annotated[
        str | None,
        typer.Option("--env", help="The pipeline's environment, if it has several"),
    ] = None,
    main_branch: Annotated[
        str, typer.Option("--main-branch", help="The branch that deploys on tags")
    ] = "main",
    tags: Annotated[
        str, typer.Option("--tags", help="Tag glob that deploys the main branch")
    ] = "v*",
    main_auto_approve: Annotated[
        bool,
        typer.Option(
            "--main-auto-approve",
            help="Owner's opt-in: main deploys without a hash approval when the "
            "plan is inside the pipeline's auto_approve policy",
        ),
    ] = False,
    storage: Annotated[
        str, typer.Option("--storage", help="Size of the state volume")
    ] = "10Gi",
    storage_class: Annotated[
        str | None,
        typer.Option("--storage-class", help="StorageClass of the state volume"),
    ] = None,
    cluster_rbac: Annotated[
        bool,
        typer.Option(
            "--cluster-rbac",
            help="Also allow ClusterRoles/ClusterRoleBindings (apps that declare them)",
        ),
    ] = False,
    platform: Annotated[
        list[str] | None,
        typer.Option(
            "--platform", help="Build platform (repeatable), e.g. linux/arm64"
        ),
    ] = None,
    approve: Annotated[
        str | None, typer.Option("--approve", help="The plan hash to execute")
    ] = None,
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
) -> None:
    """Install the GitOps controller (plan first; --approve HASH installs)."""
    from piceli.gitops.config import ControllerConfig, parse_duration
    from piceli.gitops.install import InstallSettings, plan_objects, render_controller

    with _guard():
        config = ControllerConfig(
            pipeline=pipeline,
            repo=repo,
            branches=tuple(branches.split(",")),
            env=env,
            poll_seconds=parse_duration(poll),
            main_branch=main_branch,
            tags=tags,
            main_auto_approve=main_auto_approve,
            platforms=tuple(platform or ()),
            namespace=namespace,
        )
        settings = InstallSettings(
            image=image,
            credentials_secret=credentials_secret,
            storage=storage,
            storage_class=storage_class,
            cluster_rbac=cluster_rbac,
        )
    if not config.deploys_main:
        say(f"note: {main_branch} matches no --branches glob; tags will not deploy it")
    objects = render_controller(config, settings)
    command = _command_line(
        "piceli gitops enable", pipeline, kubeconfig, context, namespace,
        repo=repo, branches=branches, poll=poll, image=image,
        credentials_secret=credentials_secret, env=env, main_branch=main_branch,
        tags=tags, storage=storage, storage_class=storage_class,
        flags={"--main-auto-approve": main_auto_approve, "--cluster-rbac": cluster_rbac, "--allow-exec": allow_exec},
        platforms=platform or [], transport=transport,
    )  # fmt: skip
    with _api(kubeconfig, context, transport, allow_exec, exec_sha256) as api:
        with _guard():
            plan = plan_objects(
                api,
                objects,
                action="enable",
                config={"controller": config.to_dict(), "install": settings.to_dict()},
            )
        _run_plan(api, plan, approve, command, {"controller": config.to_dict()})


def _command_line(
    head: str,
    pipeline: str | None,
    kubeconfig: Path | None,
    context: str | None,
    namespace: str,
    *,
    flags: dict[str, bool],
    platforms: list[str],
    transport: str,
    **options: str | None,
) -> str:
    import shlex

    parts = [head] + ([pipeline] if pipeline else [])
    parts += ["--kubeconfig", str(kubeconfig), "--context", str(context)]
    if namespace != "piceli-system":
        parts += ["--namespace", namespace]
    defaults = {
        "branches": "main",
        "poll": "60s",
        "main_branch": "main",
        "tags": "v*",
        "storage": "10Gi",
    }
    for key, value in options.items():
        if value is not None and defaults.get(key) != value:
            parts += ["--" + key.replace("_", "-"), value]
    for platform in platforms:
        parts += ["--platform", platform]
    parts += [flag for flag, on in flags.items() if on]
    if transport != "https":
        parts += ["--transport", transport]
    return shlex.join(parts)


def controller_refs(namespace: str) -> list[dict[str, Any]]:
    """The controller's objects by name (what ``disable`` looks for)."""
    from piceli.gitops.install import CONFIG_MAP, DEPLOYER, NAME, STATE_CLAIM

    rbac = "rbac.authorization.k8s.io/v1"
    named = [
        ("v1", "ServiceAccount", NAME, namespace),
        (rbac, "Role", NAME, namespace),
        (rbac, "RoleBinding", NAME, namespace),
        (rbac, "ClusterRole", NAME, None),
        (rbac, "ClusterRoleBinding", NAME, None),
        (rbac, "ClusterRole", DEPLOYER, None),
        ("v1", "PersistentVolumeClaim", STATE_CLAIM, namespace),
        ("v1", "ConfigMap", CONFIG_MAP, namespace),
        ("v1", "ConfigMap", "piceli-gitops-status", namespace),
        ("v1", "ConfigMap", "piceli-gitops-requests", namespace),
        ("apps/v1", "Deployment", NAME, namespace),
    ]
    return [
        {
            "apiVersion": api,
            "kind": kind,
            "metadata": {"name": name, **({"namespace": ns} if ns else {})},
        }
        for api, kind, name, ns in named
    ]


@app.command("disable")
def disable(
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    namespace: NamespaceOption = "piceli-system",
    delete_state: Annotated[
        bool,
        typer.Option(
            "--delete-state",
            help="Also delete the state volume (Git mirror, receipts, last-seen commits)",
        ),
    ] = False,
    approve: Annotated[
        str | None, typer.Option("--approve", help="The plan hash to execute")
    ] = None,
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
) -> None:
    """Remove the controller (plan first; --approve HASH removes). Never an environment."""
    from piceli.gitops.install import plan_objects, removable

    objects = removable(controller_refs(namespace), delete_state=delete_state)
    command = _command_line(
        "piceli gitops disable", None, kubeconfig, context, namespace,
        flags={"--delete-state": delete_state, "--allow-exec": allow_exec},
        platforms=[], transport=transport,
    )  # fmt: skip
    with _api(kubeconfig, context, transport, allow_exec, exec_sha256) as api:
        with _guard():
            plan = plan_objects(
                api,
                objects,
                action="disable",
                config={"namespace": namespace, "delete_state": delete_state},
            )
        _run_plan(
            api,
            plan,
            approve,
            command,
            {
                "kept": ["namespaces of the environments", "Namespace/" + namespace]
                + (
                    []
                    if delete_state
                    else ["PersistentVolumeClaim/piceli-gitops-state"]
                )
            },
        )


def _health(status: dict[str, Any] | None, ready: bool | None, now: float) -> str:
    from datetime import datetime

    if status is None:
        return "starting" if ready else "down"
    controller = status.get("controller") or {}
    if ready is False:
        return "down"
    last = controller.get("last_poll")
    if last:
        seconds = (
            now - datetime.fromisoformat(str(last).replace("Z", "+00:00")).timestamp()
        )
        if seconds > 3 * int(controller.get("poll_seconds") or 60) + 60:
            return "stale"
    return "degraded" if controller.get("last_error") else "healthy"


@app.command("status")
def status(
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    namespace: NamespaceOption = "piceli-system",
    state_dir: StateDirOption = None,
    transport: TransportOption = "https",
    as_json: JsonOption = False,
) -> None:
    """Controller health, repository, last poll and every branch's state."""
    import time

    with _channel(state_dir, kubeconfig, context, namespace, transport) as channel:
        with _guard():
            document = channel.read_status()
        ready: bool | None = None
        api_client = getattr(channel, "client", None)
        if api_client is not None:
            from piceli.gitops.install import NAME, Api

            with _guard():
                deployment = Api(api_client).call(
                    f"/apis/apps/v1/namespaces/{namespace}/deployments/{NAME}", "GET"
                )
            ready = bool(
                ((deployment or {}).get("status") or {}).get("availableReplicas")
            )
    health = _health(document, ready, time.time())
    body: dict[str, Any] = {
        "schema": "piceli.gitops-status.v1",
        "health": health,
        "deployment_ready": ready,
        **(document or {"controller": None, "envs": {}, "rejected_requests": []}),
    }
    controller = body.get("controller") or {}
    say(
        f"gitops controller: {health}"
        + (f" (last poll {controller.get('last_poll')})" if controller else "")
    )
    if controller:
        say(
            f"  repo {controller.get('repo')} branches {','.join(controller.get('branches') or [])}"
        )
        if controller.get("last_error"):
            say(f"  last error: {controller['last_error']}")
    for branch, env in (body.get("envs") or {}).items():
        line = f"  {branch}: {env.get('state')} {str(env.get('commit') or '')[:12]}"
        if env.get("namespace"):
            line += f" in {env['namespace']}"
        if env.get("state") == "approval-required":
            line += f"; approve: piceli gitops approve {branch} {env.get('plan_hash')}"
        elif env.get("reason"):
            line += f" ({env['reason']})"
        say(line)
    if as_json:
        emit_json(body)


@app.command("approve")
def approve_env(
    env: Annotated[
        str, typer.Argument(help="The branch (environment) waiting for approval")
    ],
    plan_hash: Annotated[
        str, typer.Argument(help="The plan hash `gitops status` shows")
    ],
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    namespace: NamespaceOption = "piceli-system",
    state_dir: StateDirOption = None,
    transport: TransportOption = "https",
) -> None:
    """Approve the pending plan of ENV; the controller applies it on its next poll."""
    from piceli.gitops.state import approve_request

    with _guard():
        key, body = approve_request(env, plan_hash)
    with _channel(state_dir, kubeconfig, context, namespace, transport) as channel:
        with _guard():
            channel.add_request(key, body)
    say(
        f"approval of {env} recorded; the controller applies it on its next poll if the plan is unchanged"
    )
    emit_json(
        {"state": "requested", "request": key, "env": env, "plan_hash": plan_hash}
    )


def promote(
    target: Annotated[
        str, typer.Argument(help="BRANCH@SHA: a commit the controller saw on BRANCH")
    ],
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    namespace: NamespaceOption = "piceli-system",
    state_dir: StateDirOption = None,
    transport: TransportOption = "https",
) -> None:
    """Ask the GitOps controller to deploy BRANCH@SHA to the main branch's environment."""
    from piceli.gitops.state import promote_request

    with _guard():
        key, body = promote_request(target)
    with _channel(state_dir, kubeconfig, context, namespace, transport) as channel:
        with _guard():
            channel.add_request(key, body)
    say(
        "promotion recorded; main deploys it after `piceli gitops approve` (see `piceli gitops status`)"
    )
    emit_json(
        {
            "state": "requested",
            "request": key,
            "branch": body["branch"],
            "commit": body["commit"],
        }
    )


@app.command("run")
def run(
    config_file: Annotated[
        Path,
        typer.Option(
            "--config", help="The controller config (config.json of the ConfigMap)"
        ),
    ],
    state_dir: Annotated[
        Path,
        typer.Option(
            "--state-dir", help="The controller's state directory (its volume)"
        ),
    ],
    once: Annotated[
        bool, typer.Option("--once", help="Poll once, print the status and exit")
    ] = False,
    service_account: Annotated[
        bool,
        typer.Option(
            "--service-account",
            help="Inside the controller's pod: reach the API with the pod's "
            "service account (an explicit kubeconfig is written for it)",
        ),
    ] = False,
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    namespace: Annotated[
        str | None,
        typer.Option(
            "--namespace",
            help="Publish status and read requests as ConfigMaps in this namespace "
            "(else files in --state-dir)",
        ),
    ] = None,
    credentials_dir: Annotated[
        Path | None,
        typer.Option("--credentials-dir", help="The mounted Git credentials Secret"),
    ] = None,
    transport: TransportOption = "https",
) -> None:
    """Run the controller loop (the Deployment's entrypoint); --once for one poll."""
    from piceli.gitops.config import ControllerConfig
    from piceli.gitops.controller import Controller
    from piceli.gitops.repo import GitRemote
    from piceli.gitops.state import controller_lock

    if service_account and kubeconfig is not None:
        reject(
            "gitops-target-required",
            "--service-account and --kubeconfig exclude each other",
        )
    with _guard():
        config = ControllerConfig.load(config_file)
    with (
        tempfile.TemporaryDirectory(prefix="piceli-gitops-") as private,
        _guard(),
        controller_lock(state_dir),
    ):
        target = _run_target(service_account, kubeconfig, context, Path(private))
        remote = GitRemote(
            config.repo, state_dir / "mirror", credentials_dir=credentials_dir
        )
        channel, closer = _run_channel(target, namespace, state_dir, transport)
        try:
            ports = _ports(target, state_dir, config.namespace, transport)
            controller = Controller(
                config,
                state_dir=state_dir,
                source=remote,
                ports=ports,
                channel=channel,
                log=say,
            )
            if once:
                emit_json(controller.poll_once())
                return
            say(f"gitops controller polling {config.repo} every {config.poll_seconds}s")
            refresh = _refresher(service_account, Path(private), channel, transport)
            _forever(controller, refresh)
        finally:
            closer()


def _run_target(
    service_account: bool, kubeconfig: Path | None, context: str | None, private: Path
) -> tuple[Path, str] | None:
    from piceli.gitops.install import service_account_kubeconfig

    if service_account:
        path = private / "kubeconfig"
        return path, service_account_kubeconfig(path)
    if kubeconfig is not None:
        return _target(kubeconfig, context)
    return None


def _run_channel(
    target: tuple[Path, str] | None,
    namespace: str | None,
    state_dir: Path,
    transport: str,
) -> tuple[Any, Callable[[], None]]:
    from piceli.gitops.state import ConfigMapChannel, DirectoryChannel

    if namespace is None or target is None:
        return DirectoryChannel(state_dir), lambda: None
    from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

    client = api_client_from_kubeconfig(target[0], target[1], transport=transport)  # type: ignore[arg-type]
    return ConfigMapChannel(client, namespace), client.close


def _ports(
    target: tuple[Path, str] | None, state_dir: Path, namespace: str, transport: str
) -> Any:
    from piceli.gitops.ports import DefaultPorts

    if target is None:
        reject(
            "gitops-target-required",
            "the controller deploys to a cluster: pass --service-account (in its pod) "
            "or --kubeconfig FILE --context NAME",
        )
    return DefaultPorts(
        target[0], target[1], state_dir, namespace=namespace, transport=transport
    )


def _refresher(
    service_account: bool, private: Path, channel: Any, transport: str
) -> Callable[[], None]:
    """Before each poll: rewrite the kubeconfig (the kubelet rotates the token)."""
    from piceli.gitops.install import service_account_kubeconfig

    def refresh() -> None:
        if not service_account:
            return
        path = private / "kubeconfig"
        context = service_account_kubeconfig(path)
        client = getattr(channel, "client", None)
        if client is not None:
            from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

            channel.client = api_client_from_kubeconfig(
                path,
                context,
                transport=transport,  # type: ignore[arg-type]
            )
            client.close()

    return refresh


def _forever(
    controller: Any, refresh: Callable[[], None]
) -> None:  # pragma: no cover - the pod's loop
    import signal
    import time

    stopping = {"now": False}

    def stop(_signal: int, _frame: Any) -> None:
        stopping["now"] = True

    signal.signal(signal.SIGTERM, stop)
    while not stopping["now"]:
        started = time.time()
        try:
            refresh()
        except Exception as error:  # noqa: BLE001 - never crash-loop on a refresh
            say(f"service account refresh failed ({type(error).__name__})")
        controller.poll_once()
        deadline = started + controller.config.poll_seconds
        while not stopping["now"] and time.time() < deadline:
            time.sleep(1)


def register(root: typer.Typer) -> None:
    """Add ``gitops`` and ``promote`` to the root application."""
    root.add_typer(app, name="gitops")
    root.command("promote")(promote)
