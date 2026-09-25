"""``piceli deploy MODULE:ATTR``: take a typed app from source to a verified release.

One journaled, resumable run of the stages inputs → build → deliver → plan →
apply → checks (see :mod:`piceli.pipeline.runner`). Output follows the CLI
contract: machine output on stdout (with ``--json``, one JSON event per stage
change, then the result; without it, only the result object), human text on
stderr. Exit codes: ``0`` ready (or planned/stopped as asked), ``1`` a stage
ran but did not succeed, ``2`` rejected, ``3`` approval required.

``--ref [SOURCE=]REV`` reads the sources from commits instead of the working
tree (:mod:`piceli.pipeline.refs`); the printed approval command pins the
resolved commit SHAs.

``--plan --out FILE`` also writes a portable plan (:mod:`piceli.pipeline.planfile`);
``--apply FILE --approve HASH`` applies it on any runner: it re-plans against
live state and runs only when the combined hash is still the approved one.
"""

from __future__ import annotations

import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Annotated, Any

import typer

from piceli.cli_contract import (
    EXIT_APPROVAL,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_REJECTED,
    emit_json,
    reject,
    say,
)

STAGE_NAMES = ("inputs", "build", "deliver", "plan", "apply", "checks")


def load_pipeline(entry: str, env: str | None = None) -> Any:
    """Import `MODULE:ATTR` and return its Pipeline; rejects (exit 2) otherwise.

    ``env`` selects one environment (``Pipeline.for_environment``); a pipeline
    with one target per environment is refused without it
    (``environment-required``).
    """
    from piceli.app.render import RenderError, load_target
    from piceli.pipeline import Pipeline, PipelineError

    try:
        value = load_target(entry, Path.cwd())
    except PipelineError as error:
        say(f"the pipeline module refused its declaration: {error}")
        reject(error.code)
    except RenderError as error:
        say(str(error))
        reject("pipeline-not-found")
    except Exception as error:
        say(f"importing {entry} failed: {type(error).__name__}: {error}")
        reject("pipeline-load-failed")
    if not isinstance(value, Pipeline):
        say(f"{entry} is a {type(value).__name__}, not a piceli Pipeline")
        reject("pipeline-not-found")
    if env is not None:
        try:
            return value.for_environment(env)
        except PipelineError as error:
            say(str(error))
            reject(error.code, str(error))
    if value.needs_environment:
        say(
            f"{entry} deploys one target per environment "
            f"({', '.join(sorted(value.targets))}); pass --env NAME"
        )
        reject("environment-required")
    return value


def env_flag(env: str | None) -> str:
    """`` --env NAME`` for printed follow-up commands (empty without one)."""
    return f" --env {env}" if env else ""


#: Human plan lines wrap here; continuation lines align under the stage text.
HUMAN_WIDTH = 100
_INDENT = " " * 11


def _say_wrapped(head: str, text: str) -> None:
    """``head`` + ``text`` wrapped at item boundaries (``, ``), never mid-name."""
    items = text.split(", ")
    line, fresh = head, True
    for index, item in enumerate(items):
        piece = item + ("," if index < len(items) - 1 else "")
        if not fresh and len(line) + 1 + len(piece) > HUMAN_WIDTH:
            say(line)
            line = _INDENT + piece
        else:
            line = line + ("" if fresh else " ") + piece
        fresh = False
    say(line)


def _describe(plan: Any, entry: str) -> None:
    stages = plan.stages
    say(f"deploy plan for {entry} (until {plan.until}):")
    inputs = stages["inputs"]
    if inputs.get("state") != "not-planned":
        for name, build in inputs.get("builds", {}).items():
            files = sum(item["files"] for item in build["contexts"].values())
            say(
                f"  inputs   {name}: {files} staged file(s), plan {build['plan_hash'][7:19]}"
            )
        for name, source in inputs.get("sources", {}).items():
            dirty = " (dirty)" if source["dirty"] else ""
            say(f"  inputs   source {name}: {source['commit'][:12]}{dirty}")
        for name, pinned in inputs.get("refs", {}).items():
            say(f"  inputs   ref {name}: {pinned['ref']} = commit {pinned['commit']}")
        if "refs" in inputs:
            checked = inputs.get("model", {}).get("checked_against")
            say(
                "  inputs   model: working tree"
                + (
                    f" (Python files match source {checked})"
                    if checked
                    else " (not in a pinned repository)"
                )
            )
    for name, build in stages["build"].get("builds", {}).items():
        say(
            f"  build    {name}: {build['action']} ({build['platform']}, "
            f"builder {build['builder'].rpartition('@')[2][7:19]}, "
            f"network {build['network']})"
        )
    deliver = stages["deliver"]
    if "registry" in deliver:
        registry = deliver["registry"]
        changes = ", ".join(
            f"{c['operation']} {c['kind']}/{c['name']}" for c in registry["changes"]
        )
        note = (
            "unchanged (deployed and ready)"
            if registry["action"] == "unchanged"
            else changes or "apply"
        )
        _say_wrapped(f"  deliver  registry {registry['release']}: ", note)
        existing = registry.get("existing")
        if existing and existing.get("action") in {"adopt", "replace"}:
            storage = existing.get("storage") or {}
            where = storage.get("host_path") or storage.get("claim") or "none"
            say(
                f"  deliver  registry {existing['action']}s live Deployment/"
                f"{existing['name']} (data on {where}: {existing['data']})"
            )
    for name, image in deliver.get("images", {}).items():
        config = image["config_digest"]
        say(
            f"  deliver  {name}: {image['action']}"
            + (f" ({config[7:19]})" if config else "")
        )
    for key, mirror in deliver.get("mirrors", {}).items():
        name, _, digest = key.partition("@")
        unused = "" if mirror["used"] else ", not used by the app"
        say(
            f"  deliver  mirror {name}@{digest[:19]}: {mirror['action']} "
            f"({mirror['platform']}{unused})"
        )
    release = stages["plan"]
    if release.get("state") == "planned":
        changes = ", ".join(
            f"{c['operation']} {c['kind']}/{c['name']}" for c in release["changes"]
        )
        _say_wrapped(
            f"  plan     release {release['release']} ({release['mode']}): ",
            changes or "no changes",
        )
        for item in release.get("drift", ()):
            _say_wrapped(
                f"  plan     drift {item['kind']}/{item['name']}: desired fields "
                "also managed by ",
                f"{', '.join(item['managers'])} (re-applied)",
            )
    elif release.get("state") == "pending":
        preview = release.get("preview")
        if isinstance(preview, dict):
            _describe_preview(preview)
        else:
            say("  plan     after delivery (the release plan needs the image digests)")
    apply = stages["apply"]
    if apply.get("action") == "skip":
        say(
            f"  apply    skip: {apply['release']} is deployed, ready and not "
            "drifted (--reapply forces it)"
        )
    elif "action" in apply:
        say(f"  apply    {apply['action']}")
    checks = stages["checks"]
    if "checks" in checks:
        rollback = (
            " (rollback on failure)" if checks["rollback_on_failed_checks"] else ""
        )
        say(f"  checks   {len(checks['checks'])} check(s){rollback}")
    say(f"combined hash: {plan.combined_hash}")


def _placeholder_note(preview: dict[str, Any]) -> str:
    return ", ".join(
        f"{name}={state}" for name, state in preview.get("placeholders", {}).items()
    )


def _describe_preview(preview: dict[str, Any]) -> None:
    """The placeholder preview: structure and ownership only, never approvable."""
    say(
        "  plan     preview with placeholder images ("
        + _placeholder_note(preview)
        + "), not approvable:"
    )
    changes = ", ".join(
        f"{c['operation']} {c['kind']}/{c['name']}" for c in preview["changes"]
    )
    _say_wrapped("  plan     preview: ", changes or "no changes")
    for item in preview.get("drift", ()):
        _say_wrapped(
            f"  plan     drift {item['kind']}/{item['name']}: desired fields "
            "also managed by ",
            f"{', '.join(item['managers'])} (re-applied)",
        )
    say("  plan     the real release plan follows delivery; it may not adopt,")
    say(_INDENT + "replace or delete more than this preview")


def _confirm(combined_hash: str) -> bool:
    if not sys.stdin.isatty():
        return False
    answer = typer.prompt(
        "Type the first 12 characters of the combined hash to execute",
        default="",
        err=True,
    )
    return bool(answer) and len(answer) >= 12 and combined_hash.startswith(answer)


def _approve_command(target: str, combined: Any, env: str | None = None) -> str:
    """The approval command; ``--ref`` values are pinned to the planned SHAs."""
    refs = combined.stages.get("inputs", {}).get("refs", {})
    pinned = "".join(f" --ref {name}={value['commit']}" for name, value in refs.items())
    return (
        f"piceli deploy {target}{env_flag(env)}{pinned} "
        f"--approve {combined.combined_hash}"
    )


@contextmanager
def _terminate_as_interrupt() -> Iterator[None]:
    """Turn SIGTERM (a cancelled CI job) into KeyboardInterrupt so cleanup runs."""

    def interrupt(_signal: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGTERM, interrupt)
    except ValueError:  # not the main thread: leave signals alone
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _human(event: dict[str, Any]) -> None:
    if event.get("event") != "stage" or event["state"] in {"planned", "running"}:
        return
    detail = event.get("detail", {})
    note = detail.get("reason") or detail.get("why")
    say(f"[{event['stage']}] {event['state']}" + (f" ({note})" if note else ""))


def deploy(
    target: Annotated[
        str | None,
        typer.Argument(
            help="The pipeline: MODULE:ATTR or path/to/file.py:ATTR naming a "
            "Pipeline (optional with --apply: the plan file names it)"
        ),
    ] = None,
    plan: Annotated[
        bool,
        typer.Option(
            "--plan",
            help="Plan every stage and print the combined hash; execute nothing",
        ),
    ] = False,
    until: Annotated[
        str,
        typer.Option(
            "--until",
            help="Stop after this stage: inputs, build, deliver, plan, apply or checks",
        ),
    ] = "checks",
    resume: Annotated[
        bool,
        typer.Option(
            "--resume",
            help="Continue the latest interrupted or failed run at its failed stage",
        ),
    ] = False,
    approve: Annotated[
        str | None,
        typer.Option("--approve", help="Combined hash to execute (from --plan)"),
    ] = None,
    auto_approve: Annotated[
        bool,
        typer.Option(
            "--auto-approve", help="Plan and execute without confirmation (CI)"
        ),
    ] = False,
    reapply: Annotated[
        bool,
        typer.Option(
            "--reapply",
            help="Apply even when the release is unchanged and already deployed",
        ),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Stream one JSON event per stage change on stdout"),
    ] = False,
    ref: Annotated[
        list[str] | None,
        typer.Option(
            "--ref",
            metavar="[SOURCE=]REV",
            help="Build SOURCE from commit REV (branch, tag or SHA) in a temporary "
            "worktree instead of the working tree; repeatable. A bare REV pins "
            "every source when they are one repository",
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="With --plan: also write the portable plan file here (apply it "
            "on any runner with --apply FILE --approve HASH)",
        ),
    ] = None,
    apply_file: Annotated[
        Path | None,
        typer.Option(
            "--apply",
            help="Apply the plan file written by --plan --out (needs --approve "
            "with its combined hash); re-plans and refuses any change",
        ),
    ] = None,
    env: Annotated[
        str | None,
        typer.Option(
            "--env",
            help="Environment to deploy: the app's overrides and the pipeline's "
            "target for it (required when the pipeline has one target per "
            "environment); the combined hash covers its name and values",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Deploy a pipeline: inputs → build → deliver → plan → apply → checks.

    Every stage is journaled and skipped when its content is unchanged. Plan
    first (--plan), then approve the combined hash (--approve HASH). With
    --ref the sources are read from commits, and the hash covers the commits.
    """
    from piceli.pipeline import PipelineError, PipelineRunner
    from piceli.pipeline.refs import parse_refs, recorded_requests
    from piceli.pipeline.runner import preview_hash

    document: dict[str, Any] | None = None
    if apply_file is not None:
        document = _plan_file(
            apply_file,
            approve,
            conflicting=bool(plan or auto_approve or resume or reapply or ref or out)
            or until != "checks",
        )
        until, reapply = document["until"], document["reapply"]
        target = target or document["entry"]
        if env is not None and env != document.get("environment"):
            say("--apply deploys the environment the plan file was made for")
            reject("deploy-plan-file-mismatch")
        env = document.get("environment")
    if out is not None and not plan:
        say("--out writes the plan file of --plan; add --plan")
        reject("deploy-flags-conflict")
    if target is None:
        say("name the pipeline: piceli deploy MODULE:ATTR (or --apply FILE)")
        reject("deploy-flags-conflict")
    if until not in STAGE_NAMES:
        say(f"--until must be one of {', '.join(STAGE_NAMES)}")
        reject("deploy-stage-unknown")
    if resume and (plan or approve or auto_approve or reapply or until != "checks"):
        say("--resume continues an approved run; it takes no planning flags")
        reject("deploy-flags-conflict")
    if approve is not None and (plan or auto_approve):
        say("--approve cannot be combined with --plan or --auto-approve")
        reject("deploy-flags-conflict")
    if plan and auto_approve:
        say("--plan executes nothing; drop --auto-approve")
        reject("deploy-flags-conflict")
    if resume and ref:
        say("--resume reuses the commits the run was approved with; drop --ref")
        reject("deploy-flags-conflict")
    try:
        refs = parse_refs(ref or ())
    except PipelineError as error:
        say(str(error))
        reject(error.code)
    if document is not None:
        # The plan's own commits, never a re-resolved branch.
        refs = recorded_requests(document.get("refs") or {})
    pipeline = load_pipeline(target, env)
    runner = PipelineRunner(
        pipeline, on_event=emit_json if as_json else _human, say=say, refs=refs
    )
    finished: tuple[int, dict[str, Any]] | None = None
    try:
        with _terminate_as_interrupt(), runner.locked(), runner.sources():
            if resume:
                result = runner.resume()
            else:
                if document is not None:
                    seeded = runner.adopt_plan_file(document)
                    if seeded:
                        say(f"plan file: {len(seeded)} receipt(s) from the plan runner")
                combined = runner.plan(until, reapply=reapply)
                _describe(combined, target + env_flag(env))
                body = {
                    "schema": "piceli.deploy-event.v1",
                    "event": "result",
                    **combined.to_dict(),
                }
                if pipeline.environment is not None:
                    body["environment"] = pipeline.environment.identity()
                refs_planned = combined.stages["inputs"].get("refs")
                if refs_planned:
                    body["refs"] = {n: v["commit"] for n, v in refs_planned.items()}
                if plan:
                    if out is not None:
                        _write_plan_file(runner, combined, out, target, reapply, env)
                        body["plan_file"] = str(out)
                    # The command stays on one line so it can be copied.
                    say("approve with:")
                    say(f"  {_approve_command(target, combined, env)}")
                    if out is not None:
                        say("or on another runner:")
                        say(
                            f"  piceli deploy --apply {out} --approve "
                            f"{combined.combined_hash}"
                        )
                    finished = (EXIT_OK, {**body, "state": "planned"})
                elif approve is not None:
                    if approve == preview_hash(combined):
                        say(
                            "that is the hash of the placeholder preview, which "
                            "is never approvable; approve the combined hash"
                        )
                        reject("pipeline-preview-not-approvable")
                    if approve != combined.combined_hash:
                        say(
                            "the plan changed since it was approved (or the hash is "
                            "wrong); review the new plan above"
                            + (
                                ""
                                if refs
                                else " (a plan made with --ref needs the same --ref)"
                            )
                        )
                        reject("pipeline-plan-changed")
                elif not auto_approve and not _confirm(combined.combined_hash):
                    say("approve with:")
                    say(f"  {_approve_command(target, combined, env)}")
                    finished = (EXIT_APPROVAL, {**body, "state": "approval-required"})
                if finished is None:
                    result = runner.execute(
                        combined, combined.combined_hash, reapply=reapply
                    )
    except PipelineError as error:
        _fail(runner, error)
    except (ValueError, OSError) as error:
        from piceli.pipeline.runner import classify

        _fail(runner, classify(error))
    except KeyboardInterrupt:
        say(
            "interrupted; continue with: piceli deploy "
            + target
            + env_flag(env)
            + " --resume"
        )
        if runner.run is not None:
            emit_json(runner.result("interrupted"))
        raise typer.Exit(EXIT_FAILED) from None
    if finished is not None:
        # Printed after the state session closed: with shared state the
        # plan's state is in the cluster before its hash is published.
        emit_json(finished[1])
        raise typer.Exit(finished[0])
    state = result["state"]
    say(f"deploy {state}: release {result.get('release')}")
    emit_json(result)
    raise typer.Exit(EXIT_OK)


def _plan_file(path: Path, approve: str | None, *, conflicting: bool) -> dict[str, Any]:
    """Load ``--apply FILE`` and check it against ``--approve`` (rejects)."""
    from piceli.pipeline import PipelineError
    from piceli.pipeline.planfile import load_plan_document

    if conflicting:
        say(
            "--apply takes the plan file's own stages, commits and flags; only "
            "--approve and --json may be added"
        )
        reject("deploy-flags-conflict")
    if approve is None:
        say("--apply needs --approve <combined hash> (the hash the reviewer approved)")
        reject("deploy-flags-conflict")
    try:
        document = load_plan_document(path)
    except PipelineError as error:
        say(str(error))
        reject(error.code)
    if approve != document["combined_hash"]:
        say("the approved hash is not this plan file's combined hash")
        reject("deploy-plan-file-mismatch")
    return document


def _write_plan_file(
    runner: Any,
    combined: Any,
    out: Path,
    target: str,
    reapply: bool,
    env: str | None,
) -> None:
    from piceli.pipeline.planfile import (
        observed_target,
        plan_document,
        write_plan_document,
    )

    observed: dict[str, str] | None
    try:
        observed = observed_target(runner.pipeline)
    except Exception as error:
        # A plan that stops before the cluster (--until inputs/build) may run
        # without cluster access; --apply then skips the identity check.
        say(f"plan file: target identity not recorded ({type(error).__name__})")
        observed = None
    document = plan_document(
        runner,
        combined,
        entry=target,
        reapply=reapply,
        observed=observed,
        environment=env,
    )
    write_plan_document(out, document)
    say(f"plan file: {out}")


def _fail(runner: Any, error: Any) -> None:
    say(f"{'failed' if error.failed else 'rejected'}: {error} ({error.code})")
    for item in error.details.get("blocking", ()):
        suggest = " or ".join(item.get("suggest", ()))
        say(
            f"  blocking {item['kind']}/{item['name']}: {item['message']}"
            + (f" -> {suggest}" if suggest else "")
        )
    preview = error.details.get("preview")
    if isinstance(preview, dict):
        say(
            "  (release preview with placeholder images: "
            + _placeholder_note(preview)
            + "; nothing was built or delivered)"
        )
    say(f"explain with: piceli explain {error.code}")
    if runner.run is not None:
        body = runner.result(runner.run.state)
        if not error.failed:
            body["state"] = "rejected"
        body["reason"] = error.code
        stage = next(
            (
                name
                for name, value in body["stages"].items()
                if value in {"failed", "rejected"}
            ),
            None,
        )
        if stage:
            body["stage"] = stage
            if error.code == "pipeline-preview-changed":
                say("plan again (finished stages are skipped) and approve the new hash")
            else:
                say(f"continue after fixing it with: --resume (stage {stage})")
    else:
        body = {"state": "rejected", "reason": error.code}
        if "stage" in error.details:
            body["stage"] = error.details["stage"]
    if error.details.get("blocking"):
        body["blocking"] = error.details["blocking"]
    if error.details.get("holder"):
        # A held release lock: who holds it and for how long (no command line).
        body["lock"] = {
            key: error.details[key]
            for key in ("holder", "expires_in")
            if key in error.details
        }
    if isinstance(preview, dict):
        body["preview"] = preview
    emit_json(body)
    raise typer.Exit(EXIT_FAILED if error.failed else EXIT_REJECTED)
