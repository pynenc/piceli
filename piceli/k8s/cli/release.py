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
            "Authorize deleting this existing unmanaged object and creating it "
            "from the release, after writing a restorable backup (repeatable; "
            "adds to [release] replace; never retained or managed objects)"
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
    code = getattr(error, "code", None)
    details = getattr(error, "details", None) or {}
    _emit(
        {
            "state": "refused",
            "reason": str(error) or type(error).__name__,
            **({"code": code} if isinstance(code, str) else {}),
            **details,
        }
    )
    _say(f"refused: {error}")
    for item in details.get("blocking", ()):
        _say(
            f"  blocking {item['kind']}/{item['name']}: {item['reason']}"
            + (f" -> {' or '.join(item['suggest'])}" if item["suggest"] else "")
        )
    raise typer.Exit(EXIT_REFUSED)


def _describe_plan(result: Any, spec: Path, command: str) -> None:
    counts = ", ".join(f"{n} {op}" for op, n in result.counts.items()) or "no actions"
    _say(f"release {result.release} ({result.mode}, {result.intent}): {counts}")
    report = result.to_dict()
    for action in report["actions"]:
        if action["operation"] != "no-op":
            _say(
                f"  {action['operation']:>7} {action['kind']}/{action['name']}"
                + _adoption_note(action.get("adoption"))
                + _write_note(action)
            )
    for item in report["drift"]:
        resource = item["resource"]
        _say(
            f"  drift   {resource['kind']}/{resource['name']}: desired fields "
            f"also managed by {', '.join(item['managers'])}"
        )
    for entry in report["adopt_not_needed"]:
        _say(f"  adopt {entry}: not needed (absent or already managed)")
    for entry in report["authorized"].get("replace_not_needed", ()):
        _say(f"  replace {entry}: not needed (absent)")
    for name, origin in result.secrets.items():
        _say(f"  secret {name}: {origin}")
    _say(f"plan hash: {result.plan_hash} (valid until {result.expires_at})")
    _say(
        f"approve with: piceli release {command} --spec {spec} --approve {result.plan_hash}"
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
    return ""


def _confirm(result: Any) -> bool:
    if not sys.stdin.isatty():
        return False
    answer = typer.prompt(
        "Type the first 12 characters of the plan hash to execute", default=""
    )
    return bool(answer) and len(answer) >= 12 and result.plan_hash.startswith(answer)


def _finish(outcome: dict[str, Any]) -> None:
    _emit(outcome)
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
    adopt: list[str] | None = None,
    replace: list[str] | None = None,
    adopt_all_desired: bool = False,
) -> None:
    try:
        runner = _runner(spec)
        if approve is not None:
            if auto_approve or rotate or adopt or replace or adopt_all_desired:
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
            result = runner.plan(
                rotate=rotate or (),
                rollback_to=rollback_to,
                adopt=adopt or (),
                replace=replace or (),
                adopt_all_desired=adopt_all_desired,
            )
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
    adopt: AdoptOption = None,
    replace: ReplaceOption = None,
    adopt_all_desired: AdoptAllOption = False,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Also write the full redacted plan JSON here"),
    ] = None,
) -> None:
    """Capture live discovery and persist an approvable plan (prints its hash)."""
    try:
        result = _runner(spec).plan(
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
    _describe_plan(result, spec, "apply")
    _emit({"state": "planned", **result.to_dict()})


app.command("preview", help="Alias of `plan`.")(plan)


@app.command("apply")
def apply(
    spec: SpecOption,
    approve: ApproveOption = None,
    auto_approve: AutoApproveOption = False,
    rotate: RotateOption = None,
    adopt: AdoptOption = None,
    replace: ReplaceOption = None,
    adopt_all_desired: AdoptAllOption = False,
) -> None:
    """Execute an approved plan (``--approve HASH``), or plan and confirm."""
    _plan_then_execute(
        spec,
        command="apply",
        approve=approve,
        auto_approve=auto_approve,
        rotate=rotate,
        adopt=adopt,
        replace=replace,
        adopt_all_desired=adopt_all_desired,
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
) -> None:
    """Re-plan and re-apply an earlier release against current cluster state."""
    _plan_then_execute(
        spec,
        command=f"rollback {target}",
        approve=approve,
        auto_approve=auto_approve,
        rollback_to=target,
        adopt=adopt,
        replace=replace,
        adopt_all_desired=adopt_all_desired,
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


secret_app = typer.Typer(
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
) -> None:
    """Show a secret's metadata, and its value with --reveal (never logged).

    Reads the local state directory only; never contacts the cluster.
    """
    from piceli.k8s.release_secret_spec import SecretError

    try:
        runner = _runner(spec)
        metadata = runner.secret_metadata(name, release=release)
        if as_json and not reveal:
            _emit(metadata)
            return
        if not reveal and not _confirm_reveal(name):
            raise SecretError(
                "secret-reveal-required",
                f"printing secret {name!r} needs --reveal (or confirmation on a "
                "terminal); use --json for metadata only",
            )
        values = runner.reveal_secret(name, key=key, release=release)
        if not as_json and len(values) != 1:
            raise SecretError(
                "secret-key-required",
                f"secret {name!r} has several values; choose one with --key "
                f"({', '.join(sorted(values))})",
            )
    except _refusals() as error:
        _refuse(error)
        return
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
