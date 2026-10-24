"""``piceli cycle report``: how long push to running-and-checked takes (0.28.0).

Per target (an environment, a Nix release, a Nix host, a train): the
p50 and p90 of the cycles that ended running and checked, their count, the
slowest phase and the slowest single cycle (:mod:`piceli.cycle.report`).

Sources: the GitOps controller's history (with ``--kubeconfig`` and
``--context``, or a local ``--state-dir``; nothing from a cluster without
them), the local train run records and the host switches ``piceli host
wait`` kept. Read-only. Importing this module is side-effect free.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import emit_json, reject, say
from piceli.k8s.cli.gitops import (
    ContextOption,
    KubeconfigOption,
    NamespaceOption,
    StateDirOption,
    TransportOption,
)

app = typer.Typer(
    rich_markup_mode=None,
    help="Cycle metrics: push to running and checked, per target.",
    no_args_is_help=True,
)

#: Train runs read per train (the newest).
MAX_TRAIN_RUNS = 200


def _trains(root: Path | None) -> list[dict[str, Any]]:
    from piceli.train.state import TrainState, default_root, train_names

    base = root or default_root()
    found: list[dict[str, Any]] = []
    for name in train_names(base):
        found.extend(TrainState(base / name).history(limit=MAX_TRAIN_RUNS))
    return found


def _controller(
    state_dir: Path | None,
    kubeconfig: Path | None,
    context: str | None,
    namespace: str,
    transport: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | bool]:
    """The controller's status and history, and what the sources say."""
    if state_dir is None and kubeconfig is None and not context:
        return (
            None,
            None,
            (
                "not read (pass --kubeconfig and --context, or --state-dir, to "
                "include environments and Nix releases)"
            ),
        )
    from piceli.gitops import GitOpsError
    from piceli.k8s.cli.gitops import open_channel

    try:
        with open_channel(state_dir, kubeconfig, context, namespace, transport) as ch:
            status = ch.read_status()
            read = getattr(ch, "read_history", None)
            history = read() if callable(read) else None
    except GitOpsError as error:
        reject(error.code, str(error))
    return status, history, True


@app.command("report")
def report(
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="Only cycles that ended in this window (24h, 30m, 7d) or "
            "after this ISO time; default: all that are kept",
        ),
    ] = None,
    target: Annotated[
        str | None,
        typer.Option("--target", help="Only this target (environment, host, train)"),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print one JSON object on stdout")
    ] = False,
    as_markdown: Annotated[
        bool,
        typer.Option(
            "--markdown",
            help="Print a Markdown table on stdout (to paste into docs)",
        ),
    ] = False,
    train_state: Annotated[
        Path | None,
        typer.Option(
            "--train-state-dir",
            help="Train state root (default $XDG_STATE_HOME/piceli/train)",
        ),
    ] = None,
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    namespace: NamespaceOption = "piceli-system",
    state_dir: StateDirOption = None,
    transport: TransportOption = "https",
) -> None:
    """Push to running and checked per target: p50, p90, count, slowest phase
    and slowest cycle, from the controller's history and local records."""
    from piceli.cycle import local
    from piceli.cycle import report as cycles

    now = time.time()
    if as_json and as_markdown:
        reject("cycle-options-invalid", "pass one of --json or --markdown")
    cutoff = None
    if since is not None:
        try:
            cutoff = cycles.parse_since(since, now)
        except ValueError as error:
            reject("cycle-options-invalid", str(error))
    status, history, controller = _controller(
        state_dir, kubeconfig, context, namespace, transport
    )
    trains = _trains(train_state)
    hosts = local.read_hosts()
    document = cycles.report(
        cycles.collect(history=history, status=status, trains=trains, hosts=hosts),
        now=now,
        since=cutoff,
        target=target,
        sources={
            "controller": controller,
            "train_runs": len(trains),
            "host_switches": len(hosts),
        },
    )
    if as_markdown:
        import sys

        sys.stdout.write(cycles.markdown(document))
        sys.stdout.flush()
        return
    for line in cycles.text(document):
        say(line)
    if as_json:
        emit_json(document)


def register(root: typer.Typer) -> None:
    root.add_typer(app, name="cycle")
