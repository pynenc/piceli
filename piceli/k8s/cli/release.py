"""``piceli release``: plan, apply, rollback, resume, stop and status from a spec.

JSON goes to stdout; a short human summary goes to stderr. Exit codes:
``0`` success, ``1`` the execution did not become ready, ``2`` refused
(invalid spec, identity mismatch, unknown plan), ``3`` approval required.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

app = typer.Typer(
    help="Plan, apply and roll back releases described by a release.toml spec.",
    no_args_is_help=True,
)

SpecOption = Annotated[
    Path,
    typer.Option(
        "--spec",
        help="release.toml describing the release",
        exists=True,
        dir_okay=False,
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


def _runner(spec: Path) -> Any:
    from piceli.k8s.release_runner import ReleaseRunner
    from piceli.k8s.release_spec import ReleaseSpec

    return ReleaseRunner(ReleaseSpec.from_toml(spec))


def _refusals() -> tuple[type[BaseException], ...]:
    from piceli.k8s.ops.kubernetes_provider import ProviderError

    return (ValueError, OSError, ProviderError)


def _refuse(error: BaseException) -> None:
    _emit({"state": "refused", "reason": str(error) or type(error).__name__})
    _say(f"refused: {error}")
    raise typer.Exit(EXIT_REFUSED)


def _describe_plan(result: Any, spec: Path, command: str) -> None:
    counts = ", ".join(f"{n} {op}" for op, n in result.counts.items()) or "no actions"
    _say(f"release {result.release} ({result.mode}, {result.intent}): {counts}")
    for action in result.to_dict()["actions"]:
        if action["operation"] != "no-op":
            _say(f"  {action['operation']:>7} {action['kind']}/{action['name']}")
    for name, origin in result.secrets.items():
        _say(f"  secret {name}: {origin}")
    _say(f"plan hash: {result.plan_hash} (valid until {result.expires_at})")
    _say(
        f"approve with: piceli release {command} --spec {spec} --approve {result.plan_hash}"
    )


def _confirm(result: Any) -> bool:
    if not sys.stdin.isatty():
        return False
    answer = typer.prompt(
        "Type the first 12 characters of the plan hash to execute", default=""
    )
    return bool(answer) and len(answer) >= 12 and result.plan_hash.startswith(answer)


def _finish(outcome: dict[str, Any]) -> None:
    _emit(outcome)
    execution = outcome["execution"]
    _say(
        f"{outcome['intent']} {outcome['release']}: {execution['state']}"
        + (
            f" ({execution['failure_category']})"
            if "failure_category" in execution
            else ""
        )
    )
    if execution["state"] != "ready" and outcome["intent"] != "stop":
        raise typer.Exit(EXIT_NOT_READY)


def _plan_then_execute(
    spec: Path,
    *,
    command: str,
    approve: str | None,
    auto_approve: bool,
    rotate: list[str] | None = None,
    rollback_to: str | None = None,
) -> None:
    try:
        runner = _runner(spec)
        if approve is not None:
            if auto_approve or rotate:
                raise ValueError("--approve cannot be combined with planning flags")
            expected = None
            if rollback_to is not None:
                expected = runner.resolve_rollback_target(rollback_to)
            outcome = runner.apply(
                approve,
                expected_intent="rollback" if rollback_to is not None else None,
                expected_release=expected,
            )
        else:
            result = runner.plan(rotate=rotate or (), rollback_to=rollback_to)
            _describe_plan(result, spec, command)
            if not auto_approve and not _confirm(result):
                _emit({"state": "approval-required", **result.to_dict()})
                raise typer.Exit(EXIT_APPROVAL)
            outcome = runner.apply(result.plan_hash)
    except _refusals() as error:
        _refuse(error)
        return
    _finish(outcome)


@app.command("plan")
def plan(
    spec: SpecOption,
    rotate: RotateOption = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Also write the full redacted plan JSON here"),
    ] = None,
) -> None:
    """Capture live discovery and persist an approvable plan (prints its hash)."""
    try:
        result = _runner(spec).plan(rotate=rotate or ())
        if out is not None:
            out.write_text(
                json.dumps(result.to_dict(full=True), sort_keys=True, indent=2)
            )
    except _refusals() as error:
        _refuse(error)
        return
    _describe_plan(result, spec, "apply")
    _emit({"state": "planned", **result.to_dict()})


app.command("preview", help="Alias of `plan`.")(plan)


@app.command("apply")
def apply(
    spec: SpecOption,
    approve: ApproveOption = None,
    auto_approve: AutoApproveOption = False,
    rotate: RotateOption = None,
) -> None:
    """Execute an approved plan (``--approve HASH``), or plan and confirm."""
    _plan_then_execute(
        spec, command="apply", approve=approve, auto_approve=auto_approve, rotate=rotate
    )


@app.command("rollback")
def rollback(
    target: Annotated[
        str, typer.Argument(help="Release name to re-apply, or `previous`")
    ],
    spec: SpecOption,
    approve: ApproveOption = None,
    auto_approve: AutoApproveOption = False,
) -> None:
    """Re-plan and re-apply an earlier release against current cluster state."""
    _plan_then_execute(
        spec,
        command=f"rollback {target}",
        approve=approve,
        auto_approve=auto_approve,
        rollback_to=target,
    )


@app.command("resume")
def resume(spec: SpecOption, release: ReleaseOption = None) -> None:
    """Resume an interrupted apply of a created release (same grant and ids)."""
    try:
        outcome = _runner(spec).resume(release)
    except _refusals() as error:
        _refuse(error)
        return
    _finish(outcome)


@app.command("stop")
def stop(spec: SpecOption, release: ReleaseOption = None) -> None:
    """Cancel the latest execution of a release (exact owner only)."""
    try:
        outcome = _runner(spec).stop(release)
    except _refusals() as error:
        _refuse(error)
        return
    _finish(outcome)


@app.command("status")
def status(spec: SpecOption) -> None:
    """Show catalogued releases, their executions and history (no cluster access)."""
    try:
        value = _runner(spec).status()
    except _refusals() as error:
        _refuse(error)
        return
    _say(
        f"deployed: {value.get('deployed')}  previous: {value.get('previous')}  "
        f"releases: {len(value['releases'])}"
    )
    _emit(value)
