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

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import EXIT_FAILED, emit_json, fail, reject, say


def _human(event: Mapping[str, Any]) -> None:
    kind = event["event"]
    if kind == "trial-started":
        say(
            f"trial {event['trial']}: {' '.join(event['branches'])} on {event['base'][:12]}"
            + (" (speculative, in parallel)" if event.get("speculative") else "")
        )
    elif kind == "step-finished":
        where = (
            f" (log {event['log']})"
            if event.get("log") and event["verdict"] not in {"passed", "reused"}
            else ""
        )
        host = (
            f" on {event['host']}" if event.get("host") not in (None, "local") else ""
        )
        say(
            f"  {event['step']}: {event['verdict']} in {event['duration']:.1f} s"
            f"{host}{where}"
        )
    elif kind == "trial-discarded":
        say(f"  trial {event['trial']} discarded: the left half did not all join")
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
    pruned = result.get("prune") or {}
    if pruned.get("sizes"):
        say("  cache: " + _sizes_line(pruned))


def _sizes_line(pruned: Mapping[str, Any]) -> str:
    from piceli.maintenance.cache import format_size

    sizes, limits = pruned["sizes"], pruned.get("limits") or {}
    line = (
        f"store {format_size(sizes['store'])} of {limits.get('store_size')}, "
        f"targets {format_size(sizes['targets'])} of {limits.get('targets_size')}, "
        f"runs {format_size(sizes['runs'])}"
    )
    if pruned.get("freed"):
        line += f"; pruned {format_size(pruned['freed'])}"
    return line


def train_prune(
    train: Annotated[
        str,
        typer.Argument(
            help="The train's name, or its spec (MODULE[:ATTR] or file.py[:ATTR]) "
            "to use its Train(retention=, cache=)"
        ),
    ],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Say what would go; remove nothing")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the result object on stdout")
    ] = False,
    state_dir: Annotated[
        Path | None,
        typer.Option("--state-dir", help="Train state root (as for piceli train)"),
    ] = None,
    cache_dir: Annotated[
        Path | None,
        typer.Option(
            "--cache-dir",
            help="The train cache (default $XDG_CACHE_HOME/piceli/train, else "
            "~/.cache/piceli/train; a spec's Train(cache=) wins)",
        ),
    ] = None,
) -> None:
    """Bound what a train keeps: worktrees, cargo targets, the content store, runs."""
    import re

    from piceli.maintenance.cache import format_size
    from piceli.train.local import cache_root
    from piceli.train.model import Retention, TrainError
    from piceli.train.prune import prune
    from piceli.train.state import TrainState

    retention = Retention()
    cache = cache_root(str(cache_dir) if cache_dir else None)
    name = train
    if ":" in train or train.endswith(".py"):
        from piceli.train.engine import locate

        try:
            loaded, _repo, _path = locate(train, Path.cwd())
        except TrainError as error:
            reject(error.code, str(error))
        name, retention = loaded.train.name, loaded.train.retention
        if loaded.train.cache:
            cache = cache_root(loaded.train.cache)
    elif not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", train):
        reject("train-not-found", f"{train!r} is neither a train name nor a spec")
    result = prune(
        name,
        cache=cache,
        state_root=TrainState.for_train(name, state_dir).root,
        retention=retention,
        dry_run=dry_run,
    )
    verb = "would remove" if dry_run else "removed"
    say(
        f"train {name}: {verb} {len(result['removed'])} "
        f"({format_size(result['freed'])})"
    )
    limits = result["limits"]
    sizes = result["sizes"]
    say(
        f"  store      {format_size(sizes['store']):>10} of {limits['store_size']} "
        f"(unused {limits['store_days']} days go)"
    )
    say(
        f"  targets    {format_size(sizes['targets']):>10} of {limits['targets_size']} "
        f"(unused {limits['target_days']} days go)"
    )
    say(f"  checkouts  {format_size(sizes['checkouts']):>10}")
    say(
        f"  runs       {format_size(sizes['runs']):>10} (the newest "
        f"{limits['keep_runs']} kept; older than {limits['log_days']} days go)"
    )
    for item in result["removed"]:
        say(
            f"  - {item['kind']:<13} {format_size(int(item['bytes'])):>10}  "
            f"{item['path']}  ({item['why']})"
        )
    if as_json:
        emit_json(result)


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
    queue: Annotated[
        bool,
        typer.Option(
            "--queue",
            help="When the train is busy, queue the request and return at once "
            "(its id and position); the running train serves it next",
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
    promote_run: Annotated[
        str | None,
        typer.Option(
            "--promote-run",
            help="Promote the head an earlier run reached (its id, or 'last'), "
            "without running it again; refused when the base moved in a way "
            "that is not a fast-forward of that head",
        ),
    ] = None,
) -> None:
    """Merge branches onto the base one tested merge at a time; eject failures."""
    from piceli.train.engine import TrainRun, locate
    from piceli.train.model import TrainError
    from piceli.train.queue import TrainQueue
    from piceli.train.runner import fan_out
    from piceli.train.state import TrainState

    del branches_flag  # only marks where the branch list starts
    if promote_run is not None and branches:
        reject(
            "train-branches-required",
            "--promote-run promotes an earlier run: name no branches",
        )
    if not branches and promote_run is None:
        reject(
            "train-branches-required", "name the candidate branches: --branches a b c"
        )
    if push and not promote and promote_run is None:
        reject("train-push-needs-promote", "--push pushes a promotion: add --promote")
    try:
        loaded, repo, spec_path = locate(app, Path.cwd())
    except TrainError as error:
        reject(error.code, str(error))
    state = TrainState.for_train(loaded.train.name, state_dir)
    run = TrainRun(
        loaded,
        repo=repo,
        spec_path=spec_path,
        branches=branches or [],
        state=state,
        promote=promote or promote_run is not None,
        push=push,
        events=fan_out(_human),
        warn=say,
    )
    if promote_run is not None:
        try:
            result = run.promote_run(promote_run)
        except TrainError as error:
            reject(error.code, str(error))
        say(
            f"train {result['train']}: run {promote_run} promoted as "
            f"{result['promote'].get('tag') or result['promote']['state']}"
        )
        if as_json:
            emit_json(result)
        if not result["ok"]:
            raise typer.Exit(EXIT_FAILED)
        return
    try:
        run.prepare()
    except TrainError as error:
        reject(error.code, str(error))
    if spec_path is None:
        say(f"note: {app} is outside {repo}; every commit is judged by its steps")
    if queue:
        entry = f"{loaded.file}:{loaded.attr}" if loaded.file is not None else app
        _queued_run(
            TrainQueue(state),
            entry=entry,
            branches=branches or [],
            promote=promote,
            push=push,
            state_dir=state_dir,
            as_json=as_json,
        )
        return
    try:
        result = run.run()
    except TrainError as error:
        if error.code == "train-busy":
            reject(
                error.code,
                str(error),
                hints=("add --queue to queue this request behind the running train",),
            )
        say(f"run {run.run_id} failed midway [{error.code}]")
        serve_queue(TrainQueue(state), state_dir)  # the queue does not wait on it
        fail(error.code, str(error), run=run.run_id)
    _summary(result)
    serve_queue(TrainQueue(state), state_dir)
    if as_json:
        emit_json(result)
    if not result["ok"]:
        raise typer.Exit(EXIT_FAILED)


def build_request(state_dir: Path | None) -> Callable[[Mapping[str, Any]], Any]:
    """Makes the run of a queued request, with the flags it was given."""
    from piceli.train.engine import TrainRun, locate
    from piceli.train.model import TrainError
    from piceli.train.runner import fan_out
    from piceli.train.state import TrainState

    def build(request: Mapping[str, Any]) -> Any:
        try:
            loaded, repo, spec_path = locate(
                str(request["entry"]), Path(request["cwd"])
            )
        except OSError:
            raise TrainError(
                "train-repo-invalid", f"{request['cwd']} is no longer a directory"
            ) from None
        return TrainRun(
            loaded,
            repo=repo,
            spec_path=spec_path,
            branches=list(request["branches"]),
            state=TrainState.for_train(loaded.train.name, state_dir),
            promote=bool(request["promote"]),
            push=bool(request["push"]),
            events=fan_out(_human),
            warn=say,
            queue=str(request["id"]),
        )

    return build


def serve_queue(queue: Any, state_dir: Path | None) -> list[tuple[Any, Any]]:
    """Serve the train's queue until it is empty (human lines on stderr)."""
    from piceli.train.queue import drain

    served = []
    for done, result in drain(queue, build_request(state_dir), say=say):
        if result is not None:
            _summary(result)
        served.append((done, result))
    return served


def queued_body(request: Mapping[str, Any], position: int | None) -> dict[str, Any]:
    from piceli.train.queue import QUEUED_SCHEMA

    return {
        "schema": QUEUED_SCHEMA,
        "state": "queued",
        "train": request["train"],
        "queue_id": request["id"],
        "position": position,
        "branches": request["branches"],
        "promote": request["promote"],
        "push": request["push"],
        "queued_at": request["queued_at"],
    }


def _queued_run(
    queue: Any,
    *,
    entry: str,
    branches: list[str],
    promote: bool,
    push: bool,
    state_dir: Path | None,
    as_json: bool,
) -> None:
    """``--queue``: write the request; return at once when the train is
    busy, else serve the queue (this request in its turn) here."""
    request = queue.enqueue(
        entry=entry, cwd=str(Path.cwd()), branches=branches, promote=promote, push=push
    )
    if queue.state.owner() is None:
        for done, result in serve_queue(queue, state_dir):
            if done["id"] != request["id"]:
                continue
            if result is None:
                reject(str(done.get("reason")), str(done.get("message") or ""))
            if as_json:
                emit_json(result)
            if not result["ok"]:
                raise typer.Exit(EXIT_FAILED)
            return
    position = queue.position(request["id"])
    say(
        f"queued {request['id']} at position {position} behind the running "
        f"train; wait with: piceli train-wait {request['id']}"
    )
    if as_json:
        emit_json(queued_body(request, position))


def register(root: typer.Typer) -> None:
    root.command("train")(train)
    root.command("train-prune")(train_prune)
