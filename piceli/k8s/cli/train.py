"""``piceli train APP --branches a b c``: a pre-merge train (0.26.0).

Each candidate set is merged onto the train's current head in a temporary
worktree and judged by the steps of the merged tree; a failing set is
bisected and its failing branches ejected (:mod:`piceli.train.engine`).
Human progress goes to stderr; ``--json`` prints the result object
(``piceli.train.v1``) on stdout. Exit codes: ``0`` every branch joined (and
promote and push, when asked, succeeded); ``1`` a branch was ejected, or
the promotion was refused, or a publish step or the push failed; ``2``
rejected before anything ran.

Importing this module is side-effect free.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import EXIT_FAILED, emit_json, fail, reject, say


def _human(event: Mapping[str, Any]) -> None:
    kind = event["event"]
    if kind == "trial-started":
        say(
            f"trial {event['trial']}: {' '.join(event['branches'])} on {event['base'][:12]}"
        )
    elif kind == "step-finished":
        where = (
            f" (log {event['log']})"
            if event.get("log") and event["verdict"] != "passed"
            else ""
        )
        say(
            f"  {event['step']}: {event['verdict']} in {event['duration']:.1f} s{where}"
        )
    elif kind == "trial-finished" and event["verdict"] != "passed":
        say(f"  trial {event['trial']} failed at {event.get('step')}")
    elif kind == "branch-joined":
        say(f"joined:  {event['branch']}")
    elif kind == "branch-included":
        say(f"already in the base: {event['branch']}")
    elif kind == "branch-ejected":
        say(f"ejected: {event['branch']} ({event.get('step')}) [{event.get('reason')}]")
    elif kind == "promoted":
        say(f"promoted: {event['tag']} at {event['head'][:12]}")
    elif kind == "promote-rejected":
        say(f"not promoted: {event['message']} [{event['reason']}]")
    elif kind in {"published", "publish-failed"}:
        say(
            f"  publish {event['step']}: {event['verdict']} in {event['duration']:.1f} s"
        )
    elif kind == "pushed":
        say(f"pushed:  {' '.join(event['refs'])} to {event['remote']}")
    elif kind == "push-failed":
        say(f"push to {event['remote']} failed [train-push-failed]")


def _summary(result: Mapping[str, Any]) -> None:
    for branch in result["branches"]:
        line = f"  {branch['verdict']:<8} {branch['branch']}"
        if branch["verdict"] == "ejected":
            line += f": {branch.get('step')} [{branch.get('reason')}]"
            for row in (branch.get("tail") or "").splitlines()[-5:]:
                line += f"\n             {row}"
        say(line)
    for item in result["flaky"]:
        say(f"  flaky    {item['key']} (seen {item.get('count', 1)}x)")
    say(
        f"train {result['train']}: {len(result['trials'])} trial(s) in "
        f"{result['duration']:.1f} s; head {result['head'][:12]}; record {result['record']}"
    )
    if result["promote"]["state"] == "not-requested" and any(
        b["verdict"] == "joined" for b in result["branches"]
    ):
        say("  nothing moved: run with --promote to tag and fast-forward")


def train(
    app: Annotated[
        str,
        typer.Argument(
            help="The train: MODULE[:ATTR] or path/to/file.py[:ATTR] declaring a "
            "piceli.train.Train (ATTR is optional when the module declares one)"
        ),
    ],
    branches: Annotated[
        list[str] | None,
        typer.Argument(help="The candidate branches, in the order they join"),
    ] = None,
    branches_flag: Annotated[
        bool,
        typer.Option("--branches", help="Starts the list of candidate branches"),
    ] = False,
    promote: Annotated[
        bool,
        typer.Option(
            "--promote",
            help="Tag the joined head and move tested and the base by "
            "fast-forward only, then run the publish steps",
        ),
    ] = False,
    push: Annotated[
        bool,
        typer.Option(
            "--push", help="Push the tag, tested and the base (needs --promote)"
        ),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the result object on stdout")
    ] = False,
    state_dir: Annotated[
        Path | None,
        typer.Option(
            "--state-dir",
            help="Train state root (default $XDG_STATE_HOME/piceli/train, else "
            "~/.local/state/piceli/train)",
        ),
    ] = None,
) -> None:
    """Merge branches onto the base one tested merge at a time; eject failures."""
    from piceli.train.engine import TrainRun, locate
    from piceli.train.model import TrainError
    from piceli.train.runner import fan_out
    from piceli.train.state import TrainState

    del branches_flag  # only marks where the branch list starts
    if not branches:
        reject(
            "train-branches-required", "name the candidate branches: --branches a b c"
        )
    if push and not promote:
        reject("train-push-needs-promote", "--push pushes a promotion: add --promote")
    try:
        loaded, repo, spec_path = locate(app, Path.cwd())
    except TrainError as error:
        reject(error.code, str(error))
    run = TrainRun(
        loaded,
        repo=repo,
        spec_path=spec_path,
        branches=branches,
        state=TrainState.for_train(loaded.train.name, state_dir),
        promote=promote,
        push=push,
        events=fan_out(_human),
    )
    try:
        run.prepare()
    except TrainError as error:
        reject(error.code, str(error))
    if spec_path is None:
        say(f"note: {app} is outside {repo}; every commit is judged by its steps")
    try:
        result = run.run()
    except TrainError as error:
        if error.code == "train-busy":
            reject(error.code, str(error))
        fail(error.code, str(error), run=run.run_id)
    _summary(result)
    if as_json:
        emit_json(result)
    if not result["ok"]:
        raise typer.Exit(EXIT_FAILED)


def register(root: typer.Typer) -> None:
    root.command("train")(train)
