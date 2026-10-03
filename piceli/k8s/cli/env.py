"""``piceli env …``, ``piceli envs`` and ``piceli logs``: per-branch environments.

A pipeline that declares ``envs=EnvConfig(...)`` runs one namespace per Git
branch (see :mod:`piceli.envs` and ``docs/environments.md``). The ``env``
group holds the commands that change an environment (``up``, ``down``,
``seed``); other modules may add commands to :data:`app` (``env push``).

Every command takes the pipeline with ``--pipeline MODULE:ATTR`` (or the
``PICELI_PIPELINE`` variable). Output follows the CLI contract: one JSON
object on stdout, human text on stderr; exit ``0`` done, ``1`` ran but did not
succeed, ``2`` rejected, ``3`` approval required (the printed ``env_hash``).

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from piceli.cli_contract import (
    EXIT_APPROVAL,
    EXIT_OK,
    emit_json,
    fail,
    reject,
    say,
)

app = typer.Typer(
    rich_markup_mode=None,
    help=(
        "Per-branch environments of a pipeline that declares envs=EnvConfig(...): "
        "one namespace per Git branch."
    ),
    no_args_is_help=True,
)

PIPELINE_ENV = "PICELI_PIPELINE"
PIPELINE_HELP = (
    "The pipeline: MODULE:ATTR or path/to/file.py:ATTR naming a Pipeline that "
    f"declares envs=EnvConfig(...) (default: ${PIPELINE_ENV})"
)

PipelineOption = Annotated[
    str | None,
    typer.Option(
        "--pipeline", envvar=PIPELINE_ENV, help=PIPELINE_HELP, show_default=False
    ),
]
BranchArgument = Annotated[
    str, typer.Argument(help="The Git branch (its environment)", show_default=False)
]
ApproveOption = Annotated[
    str | None,
    typer.Option("--approve", help="The env_hash to execute (from the plan)"),
]
PolicyOption = Annotated[
    bool,
    typer.Option(
        "--approve-if-policy",
        help="Execute without --approve when the owner allows it: "
        "EnvConfig(auto_approve=True) for branch environments, or the "
        "pipeline's auto_approve policy for the deploy (never for main's "
        "teardown or seed)",
    ),
]


def load(entry: str | None) -> Any:
    """The pipeline named by ``--pipeline`` or ``$PICELI_PIPELINE`` (rejects)."""
    if not entry:
        reject(
            "env-pipeline-missing",
            f"name the pipeline with --pipeline MODULE:ATTR or ${PIPELINE_ENV}",
        )
    from piceli.k8s.cli.deploy_pipeline import load_pipeline

    return load_pipeline(entry)


def _refuse(error: Any) -> NoReturn:
    details = dict(getattr(error, "details", {}) or {})
    if getattr(error, "failed", False):
        fail(error.code, str(error), **details)
    reject(error.code, str(error), **details)


def _command(
    verb: str, branch: str, entry: str | None, extra: list[str], hash_: str
) -> str:
    parts = ["piceli", "env", verb, branch]
    if entry:
        parts += ["--pipeline", entry]
    return shlex.join([*parts, *extra, "--approve", hash_])


def _finish(body: dict[str, Any], command: str) -> NoReturn:
    if body.get("state") == "approval-required":
        if body.get("reason"):
            say(f"outside the owner's approval policy ({body['reason']})")
        say("approve with:")
        say(f"  {command}")
        emit_json(body)
        raise typer.Exit(EXIT_APPROVAL)
    emit_json(body)
    raise typer.Exit(EXIT_OK)


def _digests(values: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values or ():
        name, sep, reference = value.partition("=")
        if not sep or not name or not reference:
            reject("env-image-invalid", f"--digest takes NAME=REFERENCE, not {value!r}")
        result[name] = reference
    return result


def _receipt(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        reject("env-image-invalid", "the build receipt is not a readable JSON file")
    if not isinstance(value, dict):
        reject("env-image-invalid", "the build receipt is not a JSON object")
    return value


@app.command("up")
def up(
    branch: BranchArgument,
    pipeline: PipelineOption = None,
    commit: Annotated[
        str | None,
        typer.Option("--commit", help="The commit deployed (recorded, shown)"),
    ] = None,
    digest: Annotated[
        list[str] | None,
        typer.Option(
            "--digest",
            metavar="NAME=REF",
            help="The image of build NAME as repository@sha256:… (repeatable); "
            "branch environments never build",
        ),
    ] = None,
    receipt: Annotated[
        Path | None,
        typer.Option("--receipt", help='A build receipt: JSON {"images": {NAME: REF}}'),
    ] = None,
    seed_from: Annotated[
        str | None,
        typer.Option(
            "--seed-from",
            help="Restore this branch's latest restore point (main) into the "
            "environment's claims after the deploy",
        ),
    ] = None,
    plan: Annotated[
        bool, typer.Option("--plan", help="Plan only; print the env_hash")
    ] = False,
    approve: ApproveOption = None,
    approve_if_policy: PolicyOption = False,
    wait: Annotated[
        bool,
        typer.Option(
            "--wait",
            help="When the budget is full, refuse (env-budget-full) instead of "
            "stopping the least recently pushed environment",
        ),
    ] = False,
) -> None:
    """Deploy BRANCH into its namespace (created when absent), isolated.

    Plans the namespace, the environments the budget stops, the deploy and
    the seed; approve the printed env_hash (or --approve-if-policy).
    """
    from piceli.envs import EnvError, env_up
    from piceli.pipeline import PipelineError

    declared = load(pipeline)
    images = _digests(digest)
    document = _receipt(receipt)
    try:
        body = env_up(
            declared,
            branch,
            commit=commit or (document or {}).get("commit"),
            digests=images,
            build_receipt=document,
            approve=approve,
            approve_if_policy=approve_if_policy,
            plan_only=plan,
            wait=wait,
            seed_from=seed_from,
            say=say,
        )
    except (EnvError, PipelineError) as error:
        _refuse(error)
    extra = [f"--digest={name}={ref}" for name, ref in images.items()]
    if receipt is not None:
        extra.append(f"--receipt={receipt}")
    if commit:
        extra.append(f"--commit={commit}")
    if seed_from:
        extra.append(f"--seed-from={seed_from}")
    say(
        f"environment {body['branch']} -> namespace {body['namespace']}"
        + (" (new)" if body.get("create_namespace") else "")
    )
    for item in body.get("stop") or ():
        say(f"  budget: stops {item} (least recently pushed)")
    if body["state"] == "ready":
        say(f"environment {body['namespace']} ready")
    _finish(body, _command("up", branch, pipeline, extra, body["env_hash"]))


@app.command("down")
def down(
    branch: BranchArgument,
    pipeline: PipelineOption = None,
    approve: ApproveOption = None,
    approve_if_policy: PolicyOption = False,
) -> None:
    """Delete BRANCH's environment: its claims, namespace and volumes (never main's)."""
    from piceli.envs import EnvError, env_down

    declared = load(pipeline)
    try:
        body = env_down(
            declared,
            branch,
            approve=approve,
            approve_if_policy=approve_if_policy,
            say=say,
        )
    except EnvError as error:
        _refuse(error)
    if body["state"] == "absent":
        say(f"branch {branch} has no environment (namespace {body['namespace']})")
        _finish(body, "")
    delete = body["delete"]
    say(
        f"deletes namespace {body['namespace']}, {len(delete['claims'])} claim(s) "
        f"and {len(delete['volumes'])} volume(s)"
    )
    _finish(body, _command("down", branch, pipeline, [], body["env_hash"]))


@app.command("seed")
def seed(
    branch: BranchArgument,
    pipeline: PipelineOption = None,
    source: Annotated[
        str | None,
        typer.Option(
            "--from", help="The branch whose latest restore point is restored (main)"
        ),
    ] = None,
    approve: ApproveOption = None,
    approve_if_policy: PolicyOption = False,
) -> None:
    """Restore main's latest restore point into BRANCH's claims (replaces their content)."""
    from piceli.envs import EnvError, seed_env

    declared = load(pipeline)
    try:
        body = seed_env(
            declared,
            branch,
            source or "",
            approve=approve,
            approve_if_policy=approve_if_policy,
            say=say,
        )
    except EnvError as error:
        _refuse(error)
    say(
        f"seeds {', '.join(body['claims']) or 'no claim'} of {body['namespace']} from "
        f"restore point {body['source']['point']} of {body['source']['namespace']}"
    )
    extra = [f"--from={source}"] if source else []
    _finish(body, _command("seed", branch, pipeline, extra, body["env_hash"]))


# ----------------------------------------------------------- envs, logs


def envs(
    pipeline: PipelineOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print one piceli.envs.v1 JSON object")
    ] = False,
) -> None:
    """List every environment: branch, namespace, commit, deploy state, health, age."""
    from piceli.envs import EnvError, list_envs
    from piceli.envs.ops import LIST_SCHEMA

    declared = load(pipeline)
    try:
        found = list_envs(declared)
    except EnvError as error:
        _refuse(error)
    rows = [item.to_dict() for item in found]
    for row in rows:
        age = row["age_seconds"]
        gitops = row.get("gitops") or {}
        say(
            f"{row['branch']:<24} {row['namespace']:<32} {row['state']:<8} "
            f"{row['health']:<9} {(row['commit'] or '-')[:12]:<12} "
            f"{row['deploy'] or '-':<10} {'-' if age is None else f'{age // 3600}h'}"
            + (
                f"  gitops {gitops.get('state')} {str(gitops.get('commit') or '')[:12]}"
                if gitops
                else ""
            )
        )
    if as_json:
        emit_json({"schema": LIST_SCHEMA, "app": declared.name, "envs": rows})


def logs(
    env: Annotated[
        str, typer.Argument(help="The branch (its environment)", show_default=False)
    ],
    workload: Annotated[
        str, typer.Argument(help="The workload name", show_default=False)
    ],
    pipeline: PipelineOption = None,
    previous: Annotated[
        bool, typer.Option("--previous", help="The previous container's logs")
    ] = False,
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Stream new lines until Ctrl-C")
    ] = False,
    tail: Annotated[int, typer.Option("--tail", min=1, max=10_000)] = 200,
    container: Annotated[str | None, typer.Option("--container")] = None,
    kubectl: Annotated[str, typer.Option(help="kubectl executable")] = "kubectl",
) -> None:
    """Print one workload's logs in an environment (kubectl, explicit context)."""
    from piceli.envs import EnvError
    from piceli.envs.ops import env_access_target
    from piceli.k8s.access import AccessTargetError
    from piceli.k8s.cli.access import _check_kubeconfig
    from piceli.k8s.observe import kubectl_logs_command, run_logs

    declared = load(pipeline)
    try:
        resolved = env_access_target(declared, env, port=lambda: 1)
    except EnvError as error:
        _refuse(error)
    except AccessTargetError as error:
        reject(error.code, str(error))
    kinds = {
        name: kind
        for kind, name in resolved.workloads
        if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}
    }
    if workload not in kinds:
        reject(
            "env-workload-unknown",
            f"the app declares no workload {workload!r}",
            workloads=sorted(kinds),
        )
    executable = shutil.which(kubectl)
    if executable is None:
        reject("access-kubectl-missing")
    _check_kubeconfig(resolved)
    command = kubectl_logs_command(
        kubectl=executable,
        kubeconfig=resolved.kubeconfig,
        context=resolved.context,
        namespace=resolved.namespace,
        target=f"{kinds[workload].lower()}/{workload}",
        tail=tail,
        container=container,
        previous=previous,
        follow=follow,
    )
    say(f"logs of {kinds[workload]}/{workload} in {resolved.namespace}")
    raise typer.Exit(run_logs(command))


def register(root: typer.Typer) -> None:
    """Add ``env`` (``up``, ``down``, ``seed``, ``push``, ``stop``, ``start``),
    ``envs`` and ``logs`` to the root."""
    from piceli.k8s.cli.env_push import push
    from piceli.k8s.cli.env_stop import start, stop

    registered = {command.name for command in app.registered_commands}
    if "push" not in registered:
        app.command("push")(push)
    if "stop" not in registered:
        app.command("stop")(stop)
        app.command("start")(start)
    root.add_typer(app, name="env")
    root.command("envs")(envs)
    root.command("logs")(logs)
