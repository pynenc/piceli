"""``piceli deploy MODULE:ATTR``: take a typed app from source to a verified release.

One journaled, resumable run of the stages inputs → build → deliver → plan →
apply → checks (see :mod:`piceli.pipeline.runner`). Output follows the CLI
contract: machine output on stdout (with ``--json``, one JSON event per stage
change, then the result; without it, only the result object), human text on
stderr. Exit codes: ``0`` ready (or planned/stopped as asked), ``1`` a stage
ran but did not succeed, ``2`` rejected, ``3`` approval required.
"""

from __future__ import annotations

import sys
from pathlib import Path
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


def _load(entry: str) -> Any:
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
    return value


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
    for name, image in deliver.get("images", {}).items():
        config = image["config_digest"]
        say(
            f"  deliver  {name}: {image['action']}"
            + (f" ({config[7:19]})" if config else "")
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


def _confirm(combined_hash: str) -> bool:
    if not sys.stdin.isatty():
        return False
    answer = typer.prompt(
        "Type the first 12 characters of the combined hash to execute",
        default="",
        err=True,
    )
    return bool(answer) and len(answer) >= 12 and combined_hash.startswith(answer)


def _human(event: dict[str, Any]) -> None:
    if event.get("event") != "stage" or event["state"] in {"planned", "running"}:
        return
    detail = event.get("detail", {})
    note = detail.get("reason") or detail.get("why")
    say(f"[{event['stage']}] {event['state']}" + (f" ({note})" if note else ""))


def deploy(
    target: Annotated[
        str,
        typer.Argument(
            help="The pipeline: MODULE:ATTR or path/to/file.py:ATTR naming a Pipeline"
        ),
    ],
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
) -> None:
    """Deploy a pipeline: inputs → build → deliver → plan → apply → checks.

    Every stage is journaled and skipped when its content is unchanged. Plan
    first (--plan), then approve the combined hash (--approve HASH).
    """
    from piceli.pipeline import PipelineError, PipelineRunner

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
    pipeline = _load(target)
    runner = PipelineRunner(
        pipeline, on_event=emit_json if as_json else _human, say=say
    )
    try:
        with runner.locked():
            if resume:
                result = runner.resume()
            else:
                combined = runner.plan(until, reapply=reapply)
                _describe(combined, target)
                body = {
                    "schema": "piceli.deploy-event.v1",
                    "event": "result",
                    **combined.to_dict(),
                }
                if plan:
                    # The command stays on one line so it can be copied.
                    say("approve with:")
                    say(f"  piceli deploy {target} --approve {combined.combined_hash}")
                    emit_json({**body, "state": "planned"})
                    raise typer.Exit(EXIT_OK)
                if approve is not None:
                    if approve != combined.combined_hash:
                        say(
                            "the plan changed since it was approved (or the hash is "
                            "wrong); review the new plan above"
                        )
                        reject("pipeline-plan-changed")
                elif not auto_approve and not _confirm(combined.combined_hash):
                    say("approve with:")
                    say(f"  piceli deploy {target} --approve {combined.combined_hash}")
                    emit_json({**body, "state": "approval-required"})
                    raise typer.Exit(EXIT_APPROVAL)
                result = runner.execute(
                    combined, combined.combined_hash, reapply=reapply
                )
    except PipelineError as error:
        _fail(runner, error)
    except (ValueError, OSError) as error:
        from piceli.pipeline.runner import classify

        _fail(runner, classify(error))
    except KeyboardInterrupt:
        say("interrupted; continue with: piceli deploy " + target + " --resume")
        if runner.run is not None:
            emit_json(runner.result("interrupted"))
        raise typer.Exit(EXIT_FAILED) from None
    state = result["state"]
    say(f"deploy {state}: release {result.get('release')}")
    emit_json(result)
    raise typer.Exit(EXIT_OK)


def _fail(runner: Any, error: Any) -> None:
    say(f"{'failed' if error.failed else 'rejected'}: {error} ({error.code})")
    for item in error.details.get("blocking", ()):
        suggest = " or ".join(item.get("suggest", ()))
        say(
            f"  blocking {item['kind']}/{item['name']}: {item['message']}"
            + (f" -> {suggest}" if suggest else "")
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
            say(f"continue after fixing it with: --resume (stage {stage})")
    else:
        body = {"state": "rejected", "reason": error.code}
    if error.details.get("blocking"):
        body["blocking"] = error.details["blocking"]
    emit_json(body)
    raise typer.Exit(EXIT_FAILED if error.failed else EXIT_REJECTED)
