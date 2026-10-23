"""``piceli trains [NAME]`` and ``piceli train-wait RUN`` (0.26.0).

``piceli trains`` shows what the trains are doing and did, from their state
directories alone (:mod:`piceli.train.status`): the run in progress with its
step, host, elapsed time and log tail (``--follow`` streams the log until
the run ends), recent runs, per-step duration trends, flaky tests and, with
``--composition``/``--infra``, which run produced what each target runs.
``--json`` prints one ``piceli.trains.v1`` object.

``piceli train-wait RUN`` blocks until a run ends and exits with its
verdict, printing its ``piceli.train.summary.v1``.

Both are read-only. Importing this module is side-effect free.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import EXIT_FAILED, emit_json, fail, reject, say
from piceli.train.queue import is_queue_id

StateRootOption = Annotated[
    Path | None,
    typer.Option(
        "--state-dir",
        help="Train state root (default $XDG_STATE_HOME/piceli/train, else "
        "~/.local/state/piceli/train)",
    ),
]


def _follow(root: Path, name: str, interval: float) -> str | None:
    """Stream the running step's log until the run ends; its run id."""
    from piceli.train.state import TrainState
    from piceli.train.status import current

    state = TrainState(root / name)
    offsets: dict[str, int] = {}
    shown: tuple[Any, Any] | None = None
    run_id: str | None = None
    first = True
    while True:
        live = current(state, lines=10)
        if live is None:
            return run_id
        run_id = live["run"]
        step = live.get("step")
        key = (step.get("trial"), step.get("name")) if step else None
        if step and key != shown:
            shown = key
            trial = f"trial {step['trial']}" if step.get("trial") else "publish"
            say(
                f"{trial}: {step['name']} on {step.get('host')} ({step.get('started')})"
            )
            if first:
                for row in (step.get("tail") or "").splitlines():
                    say(f"  | {row}")
        log = step.get("log") if step else None
        if log:
            path = Path(str(log))
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            offset = offsets.setdefault(str(log), size if first else 0)
            if size > offset:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    data = handle.read(size - offset)
                offsets[str(log)] = size
                for row in data.decode("utf-8", errors="replace").splitlines():
                    say(f"  | {row}")
        first = False
        time.sleep(interval)


def trains(
    name: Annotated[
        str | None,
        typer.Argument(help="One train (its name); default every train"),
    ] = None,
    runs: Annotated[
        int, typer.Option("--runs", help="Recent runs to show", min=1, max=200)
    ] = 10,
    trend: Annotated[
        int,
        typer.Option(
            "--trend", help="Executions per step a trend's median uses", min=1, max=200
        ),
    ] = 10,
    follow: Annotated[
        bool,
        typer.Option(
            "--follow",
            help="Stream the running step's log until the run ends (needs NAME "
            "or a single train)",
        ),
    ] = False,
    composition: Annotated[
        str | None,
        typer.Option(
            "--composition",
            help="Also show what each environment and NixHost runs and the run "
            "that produced it (as piceli targets reads them)",
        ),
    ] = None,
    infra: Annotated[
        str | None,
        typer.Option("--infra", help="MODULE declaring NixHosts (as piceli targets)"),
    ] = None,
    timeout: Annotated[
        float,
        typer.Option("--timeout", help="Seconds to wait for each host", min=1),
    ] = 20.0,
    interval: Annotated[
        float,
        typer.Option("--interval", help="Seconds between --follow polls", min=0.1),
    ] = 1.0,
    state_dir: StateRootOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print one piceli.trains.v1 object on stdout")
    ] = False,
) -> None:
    """The trains: the run in progress, recent runs, step trends, flaky tests, deployed by which run."""
    from piceli.train.state import default_root, train_names
    from piceli.train.status import deployed, histories, overview, table

    root = state_dir if state_dir is not None else default_root()
    names = [name] if name else None
    if name is not None and name not in train_names(root):
        reject(
            "train-unknown",
            f"no train {name!r} has run here (known: "
            f"{', '.join(train_names(root)) or 'none'})",
        )
    if follow:
        chosen = name or (train_names(root)[0] if len(train_names(root)) == 1 else None)
        if chosen is None:
            reject("train-unknown", "--follow needs the train's name")
        run_id = _follow(root, chosen, interval)
        if run_id is not None:
            say(f"run {run_id} ended")
        names = [chosen]
    document = overview(root, names, runs=runs, trend=trend)
    if composition is not None or infra is not None:
        from piceli.k8s.cli.targets import collect

        targets, _body, reason, _detail = collect(composition, infra, timeout=timeout)
        document["deployed"] = deployed(targets, histories(root))
        if not (targets.get("controller") or {}).get("read") and reason:
            document["deployed_reason"] = reason
    for line in table(document):
        say(line)
    if as_json:
        emit_json(document)


def _locate(root: Path, ref: str) -> tuple[str, str] | None:
    """``(train, run id)`` for a run id, or for a train name its running
    run, else its newest."""
    from piceli.train.state import TrainState, train_names

    known = train_names(root)
    if ref in known:
        state = TrainState(root / ref)
        owner = state.owner()
        if owner and isinstance(owner.get("run"), str):
            return ref, owner["run"]
        newest = state.history(limit=1)
        return (ref, str(newest[0]["run"])) if newest else None
    for train in known:
        if (root / train / "runs" / ref).is_dir():
            return train, ref
    return None


def train_wait(
    run: Annotated[
        str,
        typer.Argument(
            help="A run id (from piceli train --json or piceli trains), a queue "
            "id (q-…: waits for the run that serves it), or a train's name: its "
            "running run, else its newest"
        ),
    ],
    timeout: Annotated[
        float,
        typer.Option("--timeout", help="Seconds to wait (0: no limit)", min=0),
    ] = 0.0,
    interval: Annotated[
        float, typer.Option("--interval", help="Seconds between checks", min=0.1)
    ] = 2.0,
    state_dir: StateRootOption = None,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="Print the run's piceli.train.summary.v1 on stdout"
        ),
    ] = False,
) -> None:
    """Wait until a train run ends; exit with its verdict."""
    from piceli.train.state import TrainState, default_root
    from piceli.train.summary import headline, summarize

    root = state_dir if state_dir is not None else default_root()
    deadline = time.monotonic() + timeout if timeout else None
    if is_queue_id(run):
        found = _await_request(root, run, deadline, interval)
    else:
        found = _locate(root, run)
    if found is None:
        reject("train-run-not-found", f"no train run {run!r} under the state root")
    train, run_id = found
    state = TrainState(root / train)
    announced = False
    while True:
        record = state.record(run_id)
        owner = state.owner()
        running = bool(owner and owner.get("run") == run_id)
        if record is not None and record.get("state") != "running":
            break
        if not running:  # its directory exists, so it held the lock: killed
            fail(
                "train-interrupted",
                f"run {run_id} of train {train} stopped before it finished",
                train=train,
                run=run_id,
            )
        if not announced:
            say(f"waiting for run {run_id} of train {train}")
            announced = True
        if deadline is not None and time.monotonic() >= deadline:
            fail(
                "train-wait-timeout",
                f"run {run_id} still running after {timeout:.0f} s",
                train=train,
                run=run_id,
            )
        time.sleep(interval)
    summary = summarize(record)
    say(headline(summary))
    if as_json:
        emit_json(summary)
    if not summary.get("ok"):
        raise typer.Exit(EXIT_FAILED)


def _await_request(
    root: Path, request_id: str, deadline: float | None, interval: float
) -> tuple[str, str]:
    """Wait until a queued request has a run; ``(train, run id)``."""
    from piceli.train.queue import find_request

    announced = False
    while True:
        found = find_request(root, request_id)
        if found is None:
            reject("train-queue-not-found", f"no queued request {request_id}")
        queue, request = found
        train = queue.state.root.name
        if request.get("state") == "cancelled":
            fail("train-queue-cancelled", f"request {request_id} was cancelled",
                 train=train, queue=request_id)  # fmt: skip
        if request.get("outcome") == "rejected":
            fail(str(request.get("reason")), str(request.get("message") or ""),
                 train=train, queue=request_id)  # fmt: skip
        if request.get("run"):
            return train, str(request["run"])
        if request.get("state") == "running" and not _alive(request.get("pid")):
            fail(
                "train-interrupted",
                f"the process serving {request_id} died; resume it with "
                f"piceli train-queue {train} --drain",
                train=train,
                queue=request_id,
            )
        if not announced:
            say(f"waiting for queued request {request_id} of train {train}")
            announced = True
        if deadline is not None and time.monotonic() >= deadline:
            fail("train-wait-timeout", f"request {request_id} has not started",
                 train=train, queue=request_id)  # fmt: skip
        time.sleep(interval)


def _alive(pid: Any) -> bool:
    from piceli.train.queue import _alive as alive

    return alive(pid)


def train_queue(
    name: Annotated[str, typer.Argument(help="The train's name")],
    cancel: Annotated[
        str | None,
        typer.Option("--cancel", help="Cancel this waiting request (its q-… id)"),
    ] = None,
    drain: Annotated[
        bool,
        typer.Option(
            "--drain",
            help="Serve the queue here until it is empty (resumes a request "
            "whose process died); runs each request's steps with its flags",
        ),
    ] = False,
    state_dir: StateRootOption = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print one piceli.train.queue.v1 object on stdout"),
    ] = False,
) -> None:
    """A train's queue: waiting requests with their flags; cancel one, or serve them."""
    from piceli.k8s.cli.train import serve_queue
    from piceli.train.model import TrainError
    from piceli.train.queue import QUEUE_SCHEMA, TrainQueue
    from piceli.train.state import TrainState, default_root, train_names

    root = state_dir if state_dir is not None else default_root()
    if name not in train_names(root):
        reject("train-unknown", f"no train {name!r} has run or queued here")
    queue = TrainQueue(TrainState(root / name))
    if cancel is not None:
        try:
            done = queue.cancel(cancel)
        except TrainError as error:
            reject(error.code, str(error))
        say(f"cancelled {cancel}: {' '.join(done.get('branches') or ())}")
        if as_json:
            emit_json({"schema": QUEUE_SCHEMA, "train": name, **done})
        return
    served: list[dict[str, Any]] = []
    if drain:
        if queue.state.owner() is not None:
            say("the train is running; it serves the queue when its run ends")
        served = [done for done, _ in serve_queue(queue, state_dir)]
    requests = queue.pending()
    for row in requests:
        flags = " ".join(f for f in ("--promote", "--push") if row.get(f.strip("-")))
        where = f"#{row['position']}" if row.get("position") else row["state"]
        say(f"{where} {row['id']} {' '.join(row.get('branches') or ())} {flags}".rstrip()
            + f" (queued {row.get('queued_at')})")  # fmt: skip
    if not requests:
        say(f"train {name}: the queue is empty")
    if as_json:
        body: dict[str, Any] = {
            "schema": QUEUE_SCHEMA,
            "train": name,
            "requests": requests,
        }
        if drain:
            body["served"] = served
        emit_json(body)


def register(root: typer.Typer) -> None:
    root.command("trains")(trains)
    root.command("train-wait")(train_wait)
    root.command("train-queue")(train_queue)
