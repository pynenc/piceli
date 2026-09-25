"""``piceli release``: plan, apply, rollback, resume, stop and status from a spec.

``--spec`` names the release either way ``piceli status``/``access`` name a
target: a ``release.toml`` path, or ``MODULE:ATTR`` (``path/to/file.py:ATTR``)
naming a :class:`~piceli.pipeline.Pipeline`. For a pipeline the commands
operate on the release ``piceli deploy`` manages, with the same state
directory, release name, target and composition
(:mod:`piceli.pipeline.operate`); nothing is built.

JSON goes to stdout; a short human summary goes to stderr. Exit codes:
``0`` success, ``1`` the execution did not become ready
(``{"state": "failed", "reason": "<code>", …}``), ``2`` rejected
(``{"state": "rejected", "reason": "<code>", "message": …}``; invalid spec,
identity mismatch, unknown plan), ``3`` approval required. Every ``reason`` is
a registered error code (``piceli explain <code>``).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

app = typer.Typer(
    rich_markup_mode=None,
    help=(
        "Plan, apply and roll back releases described by a release.toml spec "
        "or deployed by a pipeline (--spec MODULE:ATTR)."
    ),
    no_args_is_help=True,
)

SpecOption = Annotated[
    str,
    typer.Option(
        "--spec",
        help=(
            "path/to/release.toml, or MODULE:ATTR (path/to/file.py:ATTR) naming "
            "a piceli Pipeline: the release `piceli deploy` manages"
        ),
        show_default=False,
    ),
]
ApproveOption = Annotated[
    str | None,
    typer.Option(
        "--approve",
        help="Plan hash to execute (from a previous `plan`/`rollback` output)",
    ),
]
AutoApproveOption = Annotated[
    bool,
    typer.Option("--auto-approve", help="Plan and execute without confirmation (CI)"),
]
RotateOption = Annotated[
    list[str] | None,
    typer.Option(
        "--rotate",
        help="Regenerate this secret generator's values in the new release (repeatable)",
    ),
]
AdoptOption = Annotated[
    list[str] | None,
    typer.Option(
        "--adopt",
        help=(
            "Authorize adopting this existing object, as Kind/name or "
            "apiVersion/Kind/name (repeatable; adds to [release] adopt)"
        ),
    ),
]
ReplaceOption = Annotated[
    list[str] | None,
    typer.Option(
        "--replace",
        help=(
            "Authorize deleting this existing unmanaged object (or a managed "
            "Job or StatefulSet whose immutable fields change) and creating it "
            "from the release, after writing a restorable backup (repeatable; "
            "adds to [release] replace; never retained objects)"
        ),
    ),
]
AdoptAllOption = Annotated[
    bool,
    typer.Option(
        "--adopt-all-desired",
        help=(
            "Authorize adopting every existing unmanaged object the composition "
            "declares (each is listed in the plan and bound to its hash)"
        ),
    ),
]
SkipChecksOption = Annotated[
    bool,
    typer.Option(
        "--skip-checks",
        help=(
            "Do not run the spec's [[checks]] after readiness (emergencies only; "
            "recorded in the release history)"
        ),
    ),
]
EnvOption = Annotated[
    str | None,
    typer.Option(
        "--env",
        help=(
            "Environment of a pipeline (--spec MODULE:ATTR): its app overrides, "
            "target and state (required when the pipeline has one target per "
            "environment)"
        ),
        show_default=False,
    ),
]
ReleaseOption = Annotated[
    str | None,
    typer.Option("--release", help="Release name (default: the latest execution)"),
]

EXIT_NOT_READY = 1
EXIT_REFUSED = 2
EXIT_APPROVAL = 3


def _emit(value: dict[str, Any]) -> None:
    typer.echo(json.dumps(value, sort_keys=True, indent=2))


def _say(message: str) -> None:
    typer.echo(message, err=True)


def is_pipeline_target(spec: str) -> bool:
    """``MODULE:ATTR`` names a pipeline; anything else is a release.toml path."""
    return not spec.endswith(".toml") and ":" in spec


@contextmanager
def _runner(
    spec: str,
    *,
    current: bool = False,
    write: bool = False,
    env: str | None = None,
) -> Iterator[Any]:
    """A runner whose state is open for the command.

    ``write``: the command changes state or the cluster. For a pipeline it
    holds the pipeline's run lock, so it never races a ``piceli deploy`` of
    the same state (``pipeline-locked``). With shared state (``state =
    "cluster"``) the working copy is refreshed from the cluster first; a
    writing command holds the release lock and writes the state back at every
    apply step and when it ends. Local ``release.toml`` state is used as is.
    """
    from piceli.k8s.release_runner import ReleaseRunner
    from piceli.k8s.release_spec import ReleaseSpec, ReleaseSpecError
    from piceli.state import session
    from piceli.state.scopes import pipeline_scope, release_scope

    pipeline = None
    loaded: Any = None
    if is_pipeline_target(spec):
        from piceli.k8s.cli.deploy_pipeline import load_pipeline

        pipeline = load_pipeline(spec, env)
        scope = pipeline_scope(pipeline)
    else:
        if env is not None:
            raise ReleaseSpecError(
                "--env selects an environment of a pipeline (--spec MODULE:ATTR); "
                "a release.toml describes one release",
                code="environment-unsupported",
            )
        path = Path(spec).expanduser()
        if not path.is_file():
            raise ReleaseSpecError(f"release spec not found: {spec}")
        loaded = ReleaseSpec.from_toml(path)
        scope = release_scope(loaded)
    local = scope.settings.backend == "local"
    if local and (pipeline is None or not write):
        if pipeline is not None:
            from piceli.pipeline.operate import operations_spec

            loaded = operations_spec(pipeline, current=current)
        yield ReleaseRunner(loaded, progress=_progress)
        return
    with session(scope, write=write, say=_say) as held:
        if pipeline is not None:
            from piceli.pipeline.operate import operations_spec

            loaded = operations_spec(pipeline, current=current)

        def progress(text: str) -> None:
            _progress(text)
            held.checkpoint()

        runner = ReleaseRunner(loaded, progress=progress)
        runner.checkpoint = lambda: held.checkpoint(force=True)
        yield runner


def _locked_runner(spec: str, *, current: bool, env: str | None = None) -> Any:
    """A runner for a command that changes the cluster (see :func:`_runner`)."""
    return _runner(spec, current=current, write=True, env=env)


def _progress(text: str) -> None:
    _say(f"  {text}")


def _refusals() -> tuple[type[BaseException], ...]:
    from piceli.k8s.ops.kubernetes_provider import ProviderError

    return (ValueError, OSError, ProviderError)


def _refuse(error: BaseException) -> NoReturn:
    """Print ``{"state": "rejected", "reason": <code>, …}`` and exit 2.

    ``code`` repeats ``reason`` for one release (0.3.0 printed ``code`` next to
    a free-text ``reason``); ``details`` (such as ``blocking``) are kept.
    """
    from piceli.cli_contract import error_code, reject

    default = (
        "release-state-unavailable" if isinstance(error, OSError) else "release-refused"
    )
    reason = error_code(error, default)
    details = dict(getattr(error, "details", None) or {})
    hints = [
        f"blocking {item['kind']}/{item['name']}: {item['message']}"
        + (f" -> {' or '.join(item['suggest'])}" if item["suggest"] else "")
        for item in details.get("blocking", ())
    ]
    reject(
        reason,
        str(error) or type(error).__name__,
        hints=hints,
        **details,
        code=reason,
    )


#: Field changes printed per object in the human plan summary.
MAX_CHANGE_LINES = 12


def _say_changes(diff: dict[str, Any] | None, limit: int | None) -> None:
    from piceli.k8s.ops.field_diff import describe_change

    if diff is None:
        return
    changes = diff["changes"]
    shown = changes if limit is None else changes[:limit]
    for change in shown:
        _say(f"            {describe_change(change, indent=' ' * 12)}")
    if len(changes) > len(shown):
        _say(
            f"            ... {len(changes) - len(shown)} more "
            "(`piceli release diff` prints them all)"
        )
    if diff["not_compared"]:
        _say(
            "            secret-bound values not shown: "
            + ", ".join(diff["not_compared"])
        )


def _diff_index(report: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (item["resource"]["kind"], item["resource"]["name"]): item
        for item in report.get("diffs", ())
    }


def _describe_plan(
    result: Any, spec: str, command: str, env: str | None = None
) -> None:
    counts = ", ".join(f"{n} {op}" for op, n in result.counts.items()) or "no actions"
    _say(f"release {result.release} ({result.mode}, {result.intent}): {counts}")
    report = result.to_dict()
    diffs = _diff_index(report)
    for action in report["actions"]:
        if action["operation"] != "no-op":
            _say(
                f"  {action['operation']:>7} {action['kind']}/{action['name']}"
                + _adoption_note(action.get("adoption"))
                + _write_note(action)
                + ("  [cluster-scoped]" if action.get("cluster_scoped") else "")
            )
            _say_changes(diffs.get((action["kind"], action["name"])), MAX_CHANGE_LINES)
    for item in report["drift"]:
        resource = item["resource"]
        _say(
            f"  drift   {resource['kind']}/{resource['name']}: desired fields "
            f"also managed by {', '.join(item['managers'])}"
        )
    for item in report.get("autoscaled", ()):
        if item["mode"] == "initial":
            continue
        resource = item["resource"]
        _say(
            f"  replicas {resource['kind']}/{resource['name']}: "
            + (
                "left to "
                if item["mode"] == "yielded"
                else "kept at the live value for "
            )
            + ", ".join(item["autoscalers"])
        )
    for entry in report["adopt_not_needed"]:
        _say(f"  adopt {entry}: not needed (absent or already managed)")
    for entry in report["authorized"].get("replace_not_needed", ()):
        _say(f"  replace {entry}: not needed (absent, or managed and unchanged)")
    for name, origin in result.secrets.items():
        _say(f"  secret {name}: {origin}")
    checks = report.get("checks") or {}
    if checks.get("names"):
        _say(
            f"  checks after readiness: {', '.join(checks['names'])}"
            + (
                "; on failure the previous ready release is re-applied automatically"
                if checks.get("rollback_on_failed_checks")
                else ""
            )
        )
    _say(f"plan hash: {result.plan_hash} (valid until {result.expires_at})")
    _say(
        f"approve with: piceli release {command} --spec {spec}"
        + (f" --env {env}" if env else "")
        + f" --approve {result.plan_hash}"
    )


def _adoption_note(adoption: dict[str, Any] | None) -> str:
    if not adoption:
        return ""
    owner = adoption.get("previous_owner") or "none"
    if adoption["mode"] == "takeover":
        moved = ", ".join(adoption["transferred_managers"]) or "none"
        return (
            f"  [takeover: transfers field managers: {moved}; fields they own "
            f"that the release does not declare will be REMOVED; previous owner: "
            f"{owner}]"
        )
    changes = adoption.get("metadata_changes")
    return (
        "  [metadata-only: owner annotation"
        + (f" and {', '.join(changes)}" if changes else " only")
        + f"; previous owner: {owner}]"
    )


def _write_note(action: dict[str, Any]) -> str:
    if action.get("metadata_only"):
        return (
            "  [retained, metadata-only: sets "
            + ", ".join(action["metadata_only"])
            + "; spec and data untouched]"
        )
    replace = action.get("replace")
    if replace:
        return (
            f"  [DELETES uid {replace['deletes_uid']} and recreates it from the "
            f"release; backup written first; dependents: "
            f"{'deleted' if replace['propagation'] == 'Background' else 'orphaned'}]"
        )
    removes = action.get("removes")
    if removes:
        return (
            "  [removes fields an earlier release declared: " + ", ".join(removes) + "]"
        )
    return ""


def _confirm(result: Any) -> bool:
    if not sys.stdin.isatty():
        return False
    answer = typer.prompt(
        "Type the first 12 characters of the plan hash to execute", default=""
    )
    return bool(answer) and len(answer) >= 12 and result.plan_hash.startswith(answer)


def _describe_checks(checks: dict[str, Any] | None, indent: str = "  ") -> None:
    if not checks:
        return
    if checks.get("skipped"):
        _say(f"{indent}checks skipped ({checks.get('flag')})")
        return
    for item in checks["results"]:
        _say(
            f"{indent}check {item['name']}: {'passed' if item['passed'] else 'FAILED'}"
            + (f" [{item['code']}]" if item.get("code") else "")
            + f" {item['detail']}"
        )


def _describe_rollback(rollback: dict[str, Any] | None) -> None:
    if not rollback:
        return
    if rollback["state"] == "rolled-back":
        _say(f"  rolled back automatically to {rollback['target']}: ready")
    else:
        _say(
            f"  automatic rollback {rollback['state']}"
            + (f" ({rollback['reason']})" if rollback.get("reason") else "")
            + (f": {rollback['detail']}" if rollback.get("detail") else "")
        )
    _describe_checks(rollback.get("checks"), "    ")


def _finish(outcome: dict[str, Any]) -> None:
    from piceli.errors import ERRORS

    execution = outcome["execution"]
    # A ready execution whose post-deploy checks failed is still a failure.
    state = outcome.get("release_state", execution["state"])
    failed = state != "ready" and outcome["intent"] != "stop"
    if failed:
        category = execution.get("failure_category")
        if state == "checks-failed":
            reason = "check-failed"
        else:
            reason = category if category in ERRORS else "execution-not-ready"
        _emit({**outcome, "state": "failed", "reason": reason})
    else:
        _emit({**outcome, "state": "succeeded"})
    for item in outcome.get("adopted", ()):
        if item["mode"] == "replace":
            _say(
                f"  replaced {item['kind']}/{item['name']} (deleted uid "
                f"{item['deleted_uid']}; backup: {item['backup']}; to restore the "
                f"previous object: kubectl delete {item['kind']}/{item['name']}, "
                f"then kubectl create -f {item['backup']})"
            )
            continue
        moved = item.get("completed_transfer")
        _say(
            f"  adopted {item['kind']}/{item['name']} ({item['mode']}"
            + (f"; transferred field managers: {', '.join(moved)}" if moved else "")
            + ")"
        )
    _describe_checks(outcome.get("checks"))
    _say(
        f"{outcome['intent']} {outcome['release']}: {state}"
        + (
            f" ({execution['failure_category']})"
            if "failure_category" in execution
            else ""
        )
    )
    _describe_rollback(outcome.get("rollback"))
    if failed:
        raise typer.Exit(EXIT_NOT_READY)


def _plan_then_execute(
    spec: str,
    *,
    command: str,
    approve: str | None,
    auto_approve: bool,
    rotate: list[str] | None = None,
    rollback_to: str | None = None,
    adopt: list[str] | None = None,
    replace: list[str] | None = None,
    adopt_all_desired: bool = False,
    skip_checks: bool = False,
    env: str | None = None,
) -> None:
    if approve is not None and (
        auto_approve or rotate or adopt or replace or adopt_all_desired
    ):
        from piceli.cli_contract import reject

        reject(
            "approve-with-planning-flags",
            "--approve cannot be combined with planning flags",
            code="approve-with-planning-flags",
        )
    try:
        # A rollback re-applies a catalogued release (its recorded images).
        with _locked_runner(spec, current=rollback_to is None, env=env) as runner:
            if approve is not None:
                expected = None
                if rollback_to is not None:
                    expected = runner.resolve_rollback_target(rollback_to)
                outcome = runner.apply(
                    approve,
                    expected_intent="rollback" if rollback_to is not None else None,
                    expected_release=expected,
                    skip_checks=skip_checks,
                )
            else:
                result = runner.plan(
                    rotate=rotate or (),
                    rollback_to=rollback_to,
                    adopt=adopt or (),
                    replace=replace or (),
                    adopt_all_desired=adopt_all_desired,
                )
                _describe_plan(result, spec, command, env)
                if not auto_approve and not _confirm(result):
                    _emit({"state": "approval-required", **result.to_dict()})
                    raise typer.Exit(EXIT_APPROVAL)
                outcome = runner.apply(result.plan_hash, skip_checks=skip_checks)
    except _refusals() as error:
        _refuse(error)
        return
    _finish(outcome)


@app.command("plan")
def plan(
    spec: SpecOption,
    rotate: RotateOption = None,
    adopt: AdoptOption = None,
    replace: ReplaceOption = None,
    adopt_all_desired: AdoptAllOption = False,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Also write the full redacted plan JSON here"),
    ] = None,
    env: EnvOption = None,
) -> None:
    """Capture live discovery and persist an approvable plan (prints its hash)."""
    try:
        with _runner(spec, current=True, write=True, env=env) as runner:
            result = runner.plan(
                rotate=rotate or (),
                adopt=adopt or (),
                replace=replace or (),
                adopt_all_desired=adopt_all_desired,
            )
        if out is not None:
            out.write_text(
                json.dumps(result.to_dict(full=True), sort_keys=True, indent=2)
            )
    except _refusals() as error:
        _refuse(error)
        return
    _describe_plan(result, spec, "apply", env)
    _emit({"state": "planned", **result.to_dict()})


app.command("preview", help="Alias of `plan`.")(plan)


@app.command("diff")
def diff(
    spec: SpecOption,
    adopt: AdoptOption = None,
    replace: ReplaceOption = None,
    adopt_all_desired: AdoptAllOption = False,
    exit_code: Annotated[
        bool,
        typer.Option(
            "--exit-code", help="Exit 1 when the release would change something"
        ),
    ] = False,
    env: EnvOption = None,
) -> None:
    """Show what `plan` would change, field by field (read-only, nothing stored)."""
    try:
        with _runner(spec, current=True, env=env) as runner:
            value = runner.diff(
                adopt=adopt or (),
                replace=replace or (),
                adopt_all_desired=adopt_all_desired,
            )
    except _refusals() as error:
        _refuse(error)
        return
    diffs = _diff_index(value)
    for action in value["actions"]:
        if action["operation"] == "no-op":
            continue
        item = diffs.get((action["kind"], action["name"]))
        _say(f"{action['operation']} {action['kind']}/{action['name']}")
        if item is None:
            continue
        if item["unified"]:
            _say(item["unified"].rstrip("\n"))
        if item["not_compared"]:
            _say("secret-bound values not shown: " + ", ".join(item["not_compared"]))
    counts = ", ".join(f"{n} {op}" for op, n in value["summary"].items())
    _say(f"release {value['release']}: {counts or 'no actions'}")
    if exit_code and value["changes"]:
        # Exit 1 names its code, as every "ran but did not succeed" result does.
        _emit({**value, "state": "diffed", "reason": "release-changes-pending"})
        _say("changes pending [release-changes-pending]")
        raise typer.Exit(EXIT_NOT_READY)
    _emit({**value, "state": "diffed"})


@app.command("apply")
def apply(
    spec: SpecOption,
    approve: ApproveOption = None,
    auto_approve: AutoApproveOption = False,
    rotate: RotateOption = None,
    adopt: AdoptOption = None,
    replace: ReplaceOption = None,
    adopt_all_desired: AdoptAllOption = False,
    skip_checks: SkipChecksOption = False,
    env: EnvOption = None,
) -> None:
    """Execute an approved plan (``--approve HASH``), or plan and confirm.

    When the execution is ready, the spec's ``[[checks]]`` run; the release
    is ready only when they pass (``release_state``: ``ready`` or
    ``checks-failed``). With ``[release] rollback_on_failed_checks = true`` the
    previous ready release is re-applied automatically (``rollback`` in the
    output).
    """
    _plan_then_execute(
        spec,
        command="apply",
        env=env,
        approve=approve,
        auto_approve=auto_approve,
        rotate=rotate,
        adopt=adopt,
        replace=replace,
        adopt_all_desired=adopt_all_desired,
        skip_checks=skip_checks,
    )


@app.command("rollback")
def rollback(
    target: Annotated[
        str, typer.Argument(help="Release name to re-apply, or `previous`")
    ],
    spec: SpecOption,
    approve: ApproveOption = None,
    auto_approve: AutoApproveOption = False,
    adopt: AdoptOption = None,
    replace: ReplaceOption = None,
    adopt_all_desired: AdoptAllOption = False,
    skip_checks: SkipChecksOption = False,
    env: EnvOption = None,
) -> None:
    """Re-plan and re-apply an earlier release against current cluster state.

    The spec's ``[[checks]]`` run after readiness, as for ``apply``.
    """
    _plan_then_execute(
        spec,
        command=f"rollback {target}",
        env=env,
        approve=approve,
        auto_approve=auto_approve,
        rollback_to=target,
        adopt=adopt,
        replace=replace,
        adopt_all_desired=adopt_all_desired,
        skip_checks=skip_checks,
    )


@app.command("resume")
def resume(
    spec: SpecOption,
    release: ReleaseOption = None,
    skip_checks: SkipChecksOption = False,
    env: EnvOption = None,
) -> None:
    """Resume an interrupted apply of a created release (same grant and ids)."""
    try:
        with _locked_runner(spec, current=False, env=env) as runner:
            outcome = runner.resume(release, skip_checks=skip_checks)
    except _refusals() as error:
        _refuse(error)
        return
    _finish(outcome)


@app.command("stop")
def stop(
    spec: SpecOption, release: ReleaseOption = None, env: EnvOption = None
) -> None:
    """Cancel the latest execution of a release (exact owner only)."""
    try:
        with _locked_runner(spec, current=False, env=env) as runner:
            outcome = runner.stop(release)
    except _refusals() as error:
        _refuse(error)
        return
    _finish(outcome)


@app.command("check")
def check(
    spec: SpecOption,
    release: Annotated[
        str | None,
        typer.Option("--release", help="Release to check (default: the selected one)"),
    ] = None,
    env: EnvOption = None,
) -> None:
    """Run the spec's [[checks]] now against a release; changes nothing.

    Exit code ``0`` when every check passed (``"state": "succeeded"``), ``1``
    when one failed (``"state": "failed", "reason": "check-failed"``).
    """
    try:
        with _runner(spec, write=True, env=env) as runner:
            outcome = runner.check(release)
    except _refusals() as error:
        _refuse(error)
        return
    passed = outcome["checks"]["passed"]
    if passed:
        _emit({**outcome, "state": "succeeded"})
    else:
        _emit({**outcome, "state": "failed", "reason": "check-failed"})
    _describe_checks(outcome["checks"])
    _say(
        f"check {outcome['release']}: "
        + ("passed" if passed else "checks-failed [check-failed]")
    )
    if not passed:
        raise typer.Exit(EXIT_NOT_READY)


@app.command("status")
def status(spec: SpecOption, env: EnvOption = None) -> None:
    """Show catalogued releases, their executions and history (no cluster access)."""
    try:
        with _runner(spec, env=env) as runner:
            value = runner.status()
    except _refusals() as error:
        _refuse(error)
        return
    _say(
        f"deployed: {value.get('deployed')}  previous: {value.get('previous')}  "
        f"releases: {len(value['releases'])}"
    )
    for item in value["releases"]:
        checks = item.get("checks")
        if checks:
            outcome = (
                f"skipped ({checks.get('flag')})"
                if checks.get("skipped")
                else "passed"
                if checks["passed"]
                else f"FAILED: {', '.join(checks['failed'])}"
            )
            _say(f"  {item['name']}: checks {outcome}")
    _emit(value)


secret_app = typer.Typer(
    rich_markup_mode=None,
    help="Inspect the secret values of a release (owner only; values need --reveal).",
    no_args_is_help=True,
)
app.add_typer(secret_app, name="secret")


def _confirm_reveal(name: str) -> bool:
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return False
    answer = typer.prompt(
        f"Type the secret name ({name}) to print its value to this terminal",
        default="",
        err=True,
    )
    return answer == name


@secret_app.command("show")
def secret_show(
    name: Annotated[str, typer.Argument(help="Secret generator name from [secrets]")],
    spec: SpecOption,
    key: Annotated[
        str | None,
        typer.Option("--key", help="One output, e.g. crt, key, ca.crt, cache.crt"),
    ] = None,
    release: ReleaseOption = None,
    reveal: Annotated[
        bool,
        typer.Option("--reveal", help="Print the value (otherwise asks on a terminal)"),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print JSON: metadata only unless --reveal"),
    ] = False,
    env: EnvOption = None,
) -> None:
    """Show a secret's metadata, and its value with --reveal (never logged).

    Reads the local state directory only; with shared state (``state =
    "cluster"``) it refreshes the working copy from the cluster first.
    """
    try:
        with _runner(spec, env=env) as runner:
            metadata = runner.secret_metadata(name, release=release)
            if as_json and not reveal:
                _emit(metadata)
                return
            values = _reveal(runner, name, key, release, reveal=reveal, as_json=as_json)
    except _refusals() as error:
        _refuse(error)
        return
    _print_values(metadata, values, as_json=as_json)


def _reveal(
    runner: Any,
    name: str,
    key: str | None,
    release: str | None,
    *,
    reveal: bool,
    as_json: bool,
) -> dict[str, bytes]:
    from piceli.k8s.release_secret_spec import SecretError

    if not reveal and not _confirm_reveal(name):
        raise SecretError(
            "secret-reveal-required",
            f"printing secret {name!r} needs --reveal (or confirmation on a "
            "terminal); use --json for metadata only",
        )
    values: dict[str, bytes] = runner.reveal_secret(name, key=key, release=release)
    if not as_json and len(values) != 1:
        raise SecretError(
            "secret-key-required",
            f"secret {name!r} has several values; choose one with --key "
            f"({', '.join(sorted(values))})",
        )
    return values


def _print_values(
    metadata: dict[str, Any], values: dict[str, bytes], *, as_json: bool
) -> None:
    if as_json:
        revealed: dict[str, Any] = {}
        for item, value in values.items():
            try:
                revealed[item] = value.decode("utf-8")
            except UnicodeDecodeError:
                import base64

                revealed[item] = {"base64": base64.b64encode(value).decode()}
        _emit({**metadata, "values": revealed})
        return
    value = next(iter(values.values()))
    stream = sys.stdout.buffer
    stream.write(value)
    if sys.stdout.isatty() and not value.endswith(b"\n"):
        stream.write(b"\n")
    stream.flush()
