"""``piceli gitops …`` and ``piceli promote``: Piceli's own GitOps controller.

- ``gitops enable PIPELINE --repo URL --branches 'main,wp-*' --image REF@sha256:…``
  plans the controller's install (exit 3 with the plan hash) and installs it
  with ``--approve HASH``; ``gitops enable infra.py --image …`` installs it
  for a composition (every source, component and environment of the module).
- ``gitops sync [ENV] [--component NAME]`` asks the controller to deploy an
  environment now (and rebuild one component).
- ``gitops disable`` plans (exit 3) and, with ``--approve HASH``, removes the
  controller; never an environment.
- ``gitops status`` shows the controller's health and every branch's commit,
  state and pending approval.
- ``gitops approve ENV HASH`` releases a deploy waiting for approval.
- ``promote [ENV] BRANCH@SHA`` asks the controller to deploy that commit to
  main, or to the named environment ENV (it then waits for ``gitops
  approve`` unless the owner allowed otherwise).
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
            "to the repository root), or a composition module infra.py (no :ATTR)",
            show_default=False,
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
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo",
            help="Git URL the controller polls (https://, ssh://, git@host:path); "
            "no credentials in it. Required for a pipeline; for a composition, its "
            "own repository (default: the origin remote of --root), followed when "
            "an environment deploys a pipeline",
        ),
    ] = None,
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
        str,
        typer.Option(
            "--main-branch",
            help="The branch that deploys on tags; for a composition, the branch "
            "of its own repository the controller follows",
        ),
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
    builder_image: Annotated[
        str | None,
        typer.Option(
            "--builder-image",
            help="Image of the cluster build Job, pinned by digest; without it "
            "branches deploy only images pushed with `piceli env push`",
        ),
    ] = None,
    build_git_secret: Annotated[
        str | None,
        typer.Option(
            "--build-git-secret",
            help="Secret (username/password) the build Job fetches the "
            "repository with (default piceli-build-git)",
        ),
    ] = None,
    builder_selector: Annotated[
        list[str] | None,
        typer.Option(
            "--builder-selector",
            help="key=value label of the builder node (repeatable; default "
            "piceli.io/builder=true on amd64)",
        ),
    ] = None,
    build_storage: Annotated[
        str, typer.Option("--build-storage", help="Size of a branch's build cache")
    ] = "20Gi",
    build_registry: Annotated[
        str | None,
        typer.Option(
            "--build-registry",
            help="oci://host[:port]/prefix the build pushes to (default: the "
            "pipeline's delivery)",
        ),
    ] = None,
    node_registry: Annotated[
        str | None,
        typer.Option(
            "--node-registry", help="host[:port] nodes pull from, when different"
        ),
    ] = None,
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="The repository's working tree: the pipeline's named "
            "environments and idle_stop are read from it (when the file is there)",
        ),
    ] = Path("."),
    approve: Annotated[
        str | None, typer.Option("--approve", help="The plan hash to execute")
    ] = None,
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
) -> None:
    """Install the GitOps controller (plan first; --approve HASH installs).

    The pipeline's named environments (EnvConfig(environments=...)) and its
    idle_stop are read from the working tree (--root) into the controller's
    config, so the plan hash covers every environment's trigger. Given a
    composition module (infra.py), every source, component and environment
    of it goes into the config instead (and --branches, --env and --tags are
    not used). A composition with an Environment(pipeline=...) is also
    followed in its own repository (--repo, default the origin remote of
    --root; branch --main-branch): the controller imports the module at that
    branch's commit, and the module path is relative to --root.
    """
    from piceli.gitops.config import (
        ControllerConfig,
        parse_duration,
        parse_selector,
    )
    from piceli.gitops.install import InstallSettings, plan_objects, render_controller

    if ":" not in pipeline and pipeline.endswith(".py"):
        _enable_composition(
            pipeline, root=root, image=image, kubeconfig=kubeconfig, context=context,
            poll=poll, credentials_secret=credentials_secret, namespace=namespace,
            storage=storage, storage_class=storage_class, cluster_rbac=cluster_rbac,
            platform=platform, builder_image=builder_image,
            build_git_secret=build_git_secret, builder_selector=builder_selector,
            build_storage=build_storage, approve=approve, allow_exec=allow_exec,
            exec_sha256=exec_sha256, transport=transport, repo=repo,
            main_branch=main_branch,
        )  # fmt: skip
        return
    if repo is None:
        reject("gitops-config-invalid", "--repo is required for a pipeline")
    with _guard():
        rules, idle = environment_rules(pipeline, root, env)
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
            builder_image=builder_image,
            build_git_secret=build_git_secret,
            builder_selector=parse_selector(builder_selector or []),
            build_storage=build_storage,
            build_registry=build_registry,
            node_registry=node_registry,
            namespace=namespace,
            environments=rules,
            idle_stop_seconds=idle,
        )
        settings = InstallSettings(
            image=image,
            credentials_secret=credentials_secret,
            storage=storage,
            storage_class=storage_class,
            cluster_rbac=cluster_rbac,
        )
    if not config.deploys_main and config.rule(main_branch) is None:
        say(f"note: {main_branch} matches no --branches glob; tags will not deploy it")
    for rule in config.environments:
        say(
            f"environment {rule.name}: follows "
            + (
                ", ".join(
                    [f"branch {b}" for b in rule.branches]
                    + [f"tag {t}" for t in rule.tags]
                    + (["promote"] if rule.promote else [])
                )
                or "nothing"
            )
        )
    objects = render_controller(config, settings)
    command = _command_line(
        "piceli gitops enable", pipeline, kubeconfig, context, namespace,
        repo=repo, branches=branches, poll=poll, image=image,
        credentials_secret=credentials_secret, env=env, main_branch=main_branch,
        tags=tags, storage=storage, storage_class=storage_class,
        builder_image=builder_image, build_git_secret=build_git_secret,
        build_storage=build_storage, build_registry=build_registry,
        node_registry=node_registry,
        root=None if root == Path(".") else str(root),
        flags={"--main-auto-approve": main_auto_approve, "--cluster-rbac": cluster_rbac, "--allow-exec": allow_exec},
        repeat={"--platform": platform or [], "--builder-selector": builder_selector or []},
        transport=transport,
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


def _enable_composition(
    entry: str,
    *,
    root: Path,
    image: str,
    kubeconfig: Path | None,
    context: str | None,
    poll: str,
    credentials_secret: str | None,
    namespace: str,
    storage: str,
    storage_class: str | None,
    cluster_rbac: bool,
    platform: list[str] | None,
    builder_image: str | None,
    build_git_secret: str | None,
    builder_selector: list[str] | None,
    build_storage: str,
    approve: str | None,
    allow_exec: bool,
    exec_sha256: str | None,
    transport: str,
    repo: str | None = None,
    main_branch: str = "main",
) -> None:
    """``gitops enable infra.py``: the controller of a composition."""
    from piceli.gitops.config import parse_duration, parse_selector
    from piceli.gitops.install import InstallSettings, plan_objects, render_controller
    from piceli.infra import CompositionError
    from piceli.infra.composition import load_composition
    from piceli.infra.controller import CompositionConfig

    try:
        composition = load_composition(entry, root)
        followed = (
            composition_repo(composition, entry, root, repo, main_branch)
            if composition.pipelines or repo is not None
            else None
        )
    except CompositionError as error:
        reject(error.code, str(error))
    with _guard():
        config = CompositionConfig(
            composition=composition.to_dict(),
            repo=None if followed is None else followed.to_dict(),
            namespace=namespace,
            poll_seconds=parse_duration(poll),
            platforms=tuple(platform or ("linux/amd64",)),
            builder_image=builder_image,
            build_git_secret=build_git_secret or "piceli-build-git",
            builder_selector=parse_selector(builder_selector or []),
            build_storage=build_storage,
        )
        controller = (
            composition.cluster.controller if composition.cluster is not None else None
        )
        settings = InstallSettings(
            image=image,
            credentials_secret=credentials_secret,
            storage=storage,
            storage_class=storage_class,
            cluster_rbac=cluster_rbac,
            node=controller.on if controller is not None else None,
        )
    if kubeconfig is None and composition.cluster is not None:
        # The cluster's credential profile (`piceli login`), never in Git.
        from piceli.profiles import ProfileError, resolve

        try:
            found = resolve(composition.cluster.credentials)
        except (ProfileError, ValueError):
            reject(
                "gitops-target-required",
                "the cluster's credential profile is unknown here: run piceli login "
                "or pass --kubeconfig FILE --context NAME",
            )
        kubeconfig, context = found.kubeconfig, found.context
    say(f"composition {composition.name}: sources " + ", ".join(
        f"{source.key} ({source.url})" for source in composition.sources
    ))  # fmt: skip
    if followed is not None:
        say(
            f"composition repository {followed.name} ({followed.url}): follows "
            f"branch {followed.branch}, imports {followed.entry}"
        )
    for item in composition.environments:
        rules = ", ".join(
            f"{source.key} " + "|".join(_rule_text(rule) for rule in rules)
            for source, rules in item.sources
        )
        say(f"environment {item.name} ({item.namespace}): follows {rules}")
    objects = render_controller(config, settings)
    command = _command_line(
        "piceli gitops enable", entry, kubeconfig, context, namespace,
        poll=poll, image=image, credentials_secret=credentials_secret,
        repo=repo, main_branch=None if main_branch == "main" else main_branch,
        storage=storage, storage_class=storage_class, builder_image=builder_image,
        build_git_secret=build_git_secret, build_storage=build_storage,
        root=None if root == Path(".") else str(root),
        flags={"--cluster-rbac": cluster_rbac, "--allow-exec": allow_exec},
        repeat={"--platform": platform or [], "--builder-selector": builder_selector or []},
        transport=transport,
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


def composition_repo(
    composition: Any, entry: str, root: Path, repo: str | None, branch: str
) -> Any:
    """The composition's own repository: ``--repo`` or the origin remote of ``root``.

    Also checks every pipeline environment's host builds: each spec is inside
    the repository and each context reads a source the environment follows
    (or the repository itself).

    :raises CompositionError: ``composition-invalid``,
        ``component-build-unsupported``.
    """
    import os
    import subprocess

    from piceli.infra import CompositionError, Source
    from piceli.infra.pipelines import RepoSettings, spec_paths

    def invalid(message: str) -> CompositionError:
        return CompositionError("composition-invalid", message)

    base = root.resolve()
    if repo is None:
        try:
            found = subprocess.run(
                ["git", "-C", str(base), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.TimeoutExpired):
            found = None
        if found is None or found.returncode != 0 or not found.stdout.strip():
            raise invalid(
                "an environment deploys a pipeline: the controller follows the "
                "composition's repository; run from its clone (--root) with an "
                "origin remote, or pass --repo URL"
            )
        repo = found.stdout.strip()
    try:
        url = Source(repo).url
    except CompositionError:
        raise invalid("--repo is not a Git URL without credentials") from None
    path = (base / entry).resolve()
    try:
        relative = path.relative_to(base).as_posix()
    except ValueError:
        raise invalid("the composition module is outside --root") from None
    same = [source for source in composition.sources if source.url == url]
    name = same[0].key if same else Source(url).key
    settings = RepoSettings(name=name, url=url, branch=branch, entry=relative)
    for item in composition.environments:
        if item.pipeline is None:
            continue
        followed = {source.key for source, _ in item.sources} | {name}
        for _, build in spec_paths(item.pipeline, base):
            try:
                spec = build.load()
            except Exception as error:
                code = getattr(error, "code", type(error).__name__)
                raise invalid(
                    f"environment {item.name!r}: its build spec ({code})"
                ) from None
            for context in spec.contexts:
                if context.source is not None and context.source not in followed:
                    raise invalid(
                        f"environment {item.name!r}: context {context.name!r} reads "
                        f"source {context.source!r}, which it does not follow"
                    )
    return settings


def _rule_text(rule: Any) -> str:
    from piceli.envs.model import BRANCH, Branch, Tag

    if rule == BRANCH:
        return "{branch}"
    if isinstance(rule, Branch):
        return f"branch {rule.name}"
    if isinstance(rule, Tag):
        return f"tag {rule.pattern}"
    return "promote"


def environment_rules(
    entry: str, root: Path, env: str | None
) -> tuple[tuple[Any, ...], int | None]:
    """The named environments' rules and ``idle_stop`` of the pipeline at ``root``.

    Nothing when the pipeline file is not in ``root`` (0.13 behaviour: the
    controller then deploys branches and main by tags only).

    :raises GitOpsError: ``gitops-pipeline-invalid`` when it is there and
        does not load.
    """
    from piceli.gitops import GitOpsError
    from piceli.gitops.config import EnvRule

    module = entry.rpartition(":")[0]
    if not module.endswith(".py") or not (root / module).is_file():
        return (), None
    from piceli.app.render import load_target
    from piceli.pipeline import Pipeline

    try:
        value = load_target(entry, root)
        if isinstance(value, Pipeline) and env is not None:
            value = value.for_environment(env)
    except Exception as error:  # the module raised while importing
        raise GitOpsError(
            "gitops-pipeline-invalid",
            f"cannot read the environments of {entry}: {type(error).__name__}",
        ) from None
    if not isinstance(value, Pipeline):
        raise GitOpsError("gitops-pipeline-invalid", f"{entry} is not a Pipeline")
    config = value.envs
    if config is None:
        return (), None
    rules = tuple(EnvRule.from_environment(item) for item in config.environments)
    return rules, config.idle_stop_seconds


def _command_line(
    head: str,
    pipeline: str | None,
    kubeconfig: Path | None,
    context: str | None,
    namespace: str,
    *,
    flags: dict[str, bool],
    repeat: dict[str, list[str]],
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
        "build_storage": "20Gi",
    }
    for key, value in options.items():
        if value is not None and defaults.get(key) != value:
            parts += ["--" + key.replace("_", "-"), value]
    for option, values in repeat.items():
        for value in values:
            parts += [option, value]
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
        repeat={}, transport=transport,
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
            rollout = (deployment or {}).get("status") or {}
            ready = bool(
                rollout.get("availableReplicas") or rollout.get("readyReplicas")
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


@app.command("sync")
def sync(
    env: Annotated[
        str | None,
        typer.Argument(help="The environment to deploy now (default: every one)"),
    ] = None,
    component: Annotated[
        str | None,
        typer.Option(
            "--component",
            help="Also rebuild this component, even when its source did not change "
            "(a composition controller)",
        ),
    ] = None,
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    namespace: NamespaceOption = "piceli-system",
    state_dir: StateDirOption = None,
    transport: TransportOption = "https",
) -> None:
    """Ask the controller to deploy ENV now at its revision (--component: rebuild one)."""
    from piceli.gitops.state import sync_request

    with _guard():
        key, body = sync_request(env, component)
    with _channel(state_dir, kubeconfig, context, namespace, transport) as channel:
        with _guard():
            channel.add_request(key, body)
    say(
        f"sync of {env or 'every environment'} recorded; the controller deploys it "
        "on its next poll (an approval may still be required)"
    )
    emit_json(
        {
            "state": "requested",
            "request": key,
            "env": env,
            "component": component,
        }
    )


def promote(
    target: Annotated[
        str,
        typer.Argument(
            help="BRANCH@SHA (to main), or the named environment followed by BRANCH@SHA"
        ),
    ],
    source: Annotated[
        str | None,
        typer.Argument(
            help="BRANCH@SHA when the first argument names an environment",
            show_default=False,
        ),
    ] = None,
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    namespace: NamespaceOption = "piceli-system",
    state_dir: StateDirOption = None,
    transport: TransportOption = "https",
) -> None:
    """Ask the GitOps controller to deploy BRANCH@SHA to main, or to environment ENV.

    ``piceli promote wp-1@abc1234`` targets main; ``piceli promote rc
    main@abc1234`` targets the named environment ``rc`` (it must follow
    ``Promote()``).
    """
    from piceli.gitops.state import promote_request

    env = target if source is not None else None
    with _guard():
        key, body = promote_request(source or target, env)
    with _channel(state_dir, kubeconfig, context, namespace, transport) as channel:
        with _guard():
            channel.add_request(key, body)
    say(
        f"promotion recorded; {env or 'main'} deploys it after `piceli gitops "
        "approve` (see `piceli gitops status`)"
    )
    emit_json(
        {
            "state": "requested",
            "request": key,
            "branch": body["branch"],
            "commit": body["commit"],
            **({"env": env} if env is not None else {}),
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
    local_build: Annotated[
        bool,
        typer.Option(
            "--local-build",
            help="A composition controller run outside the cluster: build "
            "components on this machine (host builds) and push them through a "
            "port-forward to the in-cluster registry, instead of a build Job",
        ),
    ] = False,
    transport: TransportOption = "https",
) -> None:
    """Run the controller loop (the Deployment's entrypoint); --once for one poll."""
    import json

    from piceli.gitops.config import ControllerConfig
    from piceli.gitops.controller import Controller
    from piceli.gitops.repo import GitRemote
    from piceli.gitops.state import controller_lock
    from piceli.infra.controller import is_composition_config

    if service_account and kubeconfig is not None:
        reject(
            "gitops-target-required",
            "--service-account and --kubeconfig exclude each other",
        )
    try:
        document = json.loads(config_file.read_text())
    except (OSError, ValueError):
        document = None
    if is_composition_config(document):
        _run_composition(
            config_file, state_dir, once=once, service_account=service_account,
            kubeconfig=kubeconfig, context=context, namespace=namespace,
            credentials_dir=credentials_dir, local_build=local_build,
            transport=transport,
        )  # fmt: skip
        return
    if local_build:
        reject("gitops-config-invalid", "--local-build needs a composition controller")
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
            ports = _ports(target, state_dir, config, transport)
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


def _run_composition(
    config_file: Path,
    state_dir: Path,
    *,
    once: bool,
    service_account: bool,
    kubeconfig: Path | None,
    context: str | None,
    namespace: str | None,
    credentials_dir: Path | None,
    local_build: bool,
    transport: str,
) -> None:
    """``gitops run`` of a composition controller (every source, change-aware)."""
    from piceli.gitops.state import controller_lock
    from piceli.infra.controller import CompositionController, load_config
    from piceli.infra.sources import SourceSet

    with _guard():
        config = load_config(config_file)
    with (
        tempfile.TemporaryDirectory(prefix="piceli-gitops-") as private,
        _guard(),
        controller_lock(state_dir),
    ):
        target = _run_target(service_account, kubeconfig, context, Path(private))
        if target is None:
            reject(
                "gitops-target-required",
                "the controller deploys to a cluster: pass --service-account (in its "
                "pod) or --kubeconfig FILE --context NAME",
            )
        sources = SourceSet(
            config.sources, state_dir / "sources", credentials_dir=credentials_dir
        )
        channel, closer = _run_channel(target, namespace, state_dir, transport)
        try:
            ports = _composition_ports(
                target, state_dir, config, transport, local_build=local_build
            )
            controller = CompositionController(
                config,
                state_dir=state_dir,
                sources=sources,
                ports=ports,
                channel=channel,
                log=say,
            )
            if once:
                emit_json(controller.poll_once())
                return
            say(
                f"gitops controller polling {len(sources.remotes)} source(s) every "
                f"{config.poll_seconds}s"
            )
            refresh = _refresher(service_account, Path(private), channel, transport)
            _forever(controller, refresh)
        finally:
            closer()


class _NoBuilder:
    """A composition controller without --builder-image: mirrors, never builds."""

    def __init__(self, mirror: Any) -> None:
        self._mirror = mirror

    def build(self, items: Any, checkout: Any) -> Any:
        from piceli.gitops import GitOpsError

        raise GitOpsError(
            "gitops-config-invalid",
            "no --builder-image: this controller cannot build components",
        )

    def build_spec(self, request: Any, checkout: Any) -> Any:
        return self.build((), checkout)

    def mirror(self, items: Any) -> Any:
        return self._mirror.mirror(items)


def _composition_ports(
    target: tuple[Path, str],
    state_dir: Path,
    config: Any,
    transport: str,
    *,
    local_build: bool,
) -> Any:
    from piceli.gitops.ports import DefaultPorts
    from piceli.infra import CompositionError
    from piceli.infra.builders import JobBuilder, JobSettings, LocalBuilder
    from piceli.infra.controller import DefaultCompositionPorts
    from piceli.pipeline.backend import RegistryRoute

    registry = config.registry
    if registry is None:
        raise CompositionError(
            "composition-invalid",
            "the composition's Cluster needs registry=Registry.in_cluster(...)",
        )
    envs = DefaultPorts(
        target[0],
        target[1],
        state_dir,
        namespace=config.namespace,
        config=config,
        transport=transport,
        log=say,
    )
    if local_build:
        route = RegistryRoute(
            push=None,
            node_registry=registry.host,
            forward=f"service/{registry.name}",
            namespace=registry.namespace,
            remote_port=registry.port,
            kubeconfig=target[0],
            context=target[1],
        )
        builder: Any = LocalBuilder(
            route,
            platform=config.platforms[0],
            cache_dir=state_dir / "build-cache",
            work_dir=state_dir / "work",
            say=say,
        )
    else:
        # In the cluster: the registry by its Service name, plain HTTP.
        route = RegistryRoute(
            push=registry.host, node_registry=registry.host, tls=False
        )
        mirror = LocalBuilder(
            route,
            platform=config.platforms[0],
            cache_dir=state_dir / "build-cache",
            work_dir=state_dir / "work",
            say=say,
        )
        if config.builder_image is None:
            builder = _NoBuilder(mirror)
        else:
            from piceli.artifacts.cluster_build import BuildCluster
            from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

            cluster = BuildCluster(
                api_client_from_kubeconfig(target[0], target[1], transport=transport),  # type: ignore[arg-type]
                config.namespace,
            )
            settings = JobSettings(
                image=config.builder_image,
                namespace=config.namespace,
                registry_url=registry.url,
                node_registry=registry.host,
                git_secret=config.build_git_secret,
                selector=dict(config.builder_selector) or {"piceli.io/builder": "true"},
                storage=config.build_storage,
                platforms=tuple(config.platforms),
            )
            builder = JobBuilder(
                settings,
                {source.key: source.url for source in config.sources},
                cluster,
                mirror_route=route,
                say=say,
            )
    return DefaultCompositionPorts(
        envs,
        builder,
        kubeconfig=target[0],
        context=target[1],
        state_dir=state_dir,
        transport=transport,
    )


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
    target: tuple[Path, str] | None, state_dir: Path, config: Any, transport: str
) -> Any:
    from piceli.gitops.ports import DefaultPorts

    if target is None:
        reject(
            "gitops-target-required",
            "the controller deploys to a cluster: pass --service-account (in its pod) "
            "or --kubeconfig FILE --context NAME",
        )
    return DefaultPorts(
        target[0],
        target[1],
        state_dir,
        namespace=config.namespace,
        config=config,
        transport=transport,
        log=say,
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


#: After a failed poll the loop waits this long, doubling per failure.
RETRY_FIRST_SECONDS = 5.0
#: The longest wait between failed polls.
RETRY_MAX_SECONDS = 300.0


def _forever(
    controller: Any,
    refresh: Callable[[], None],
    *,
    stop: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    handle_signals: bool = True,
) -> None:
    """The pod's loop: poll every ``poll_seconds`` until SIGTERM.

    A failed poll (an API timeout, a dropped connection) never ends the
    process, which would crash-loop the pod: it is logged by type and
    retried after 5 s, doubling per consecutive failure up to 5 minutes; a
    successful poll restores the normal cadence.
    """
    import signal
    import time

    sleep = sleep or time.sleep
    clock = clock or time.time
    stopping = {"now": False}

    def stopped() -> bool:
        return stopping["now"] or (stop is not None and stop())

    if handle_signals:

        def on_term(_signal: int, _frame: Any) -> None:
            stopping["now"] = True

        signal.signal(signal.SIGTERM, on_term)
    failures = 0
    while not stopped():
        started = clock()
        try:
            refresh()
        except Exception as error:  # never crash-loop on a refresh
            say(f"service account refresh failed ({type(error).__name__})")
        try:
            controller.poll_once()
        except Exception as error:  # never crash-loop on a poll
            failures += 1
            wait = min(RETRY_MAX_SECONDS, RETRY_FIRST_SECONDS * 2 ** (failures - 1))
            say(f"poll failed ({type(error).__name__}); retrying in {wait:.0f}s")
        else:
            failures = 0
            wait = max(1.0, controller.config.poll_seconds - (clock() - started))
        # Sleep in short steps so SIGTERM stops the pod promptly.
        while wait > 0 and not stopped():
            step = min(wait, 1.0) if handle_signals else wait
            sleep(step)
            wait -= step


def register(root: typer.Typer) -> None:
    """Add ``gitops`` and ``promote`` to the root application."""
    root.add_typer(app, name="gitops")
    root.command("promote")(promote)
