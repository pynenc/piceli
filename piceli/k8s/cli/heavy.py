"""``piceli heavy run`` and ``piceli heavy status``: serialize heavy work.

The lock, the holder record and the receipts live in a per-user state
directory (see :mod:`piceli.maintenance.heavy`). Neither command contacts a
cluster. ``heavy run`` sends the child's stdout to stderr so that stdout
carries exactly one JSON object (the receipt), as the CLI contract requires.

Importing this module is side-effect free.
"""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from piceli.cli_contract import emit_json, reject, say

app = typer.Typer(
    rich_markup_mode=None,
    help=(
        "A machine-wide lock for heavy work (builds, clusters, long test "
        "gates) with a receipt per run."
    ),
    no_args_is_help=True,
)


def _stderr_fd() -> int | None:
    try:
        return sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):  # captured, e.g. in tests
        return None


@app.command(
    "run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run(
    command: Annotated[
        list[str] | None,
        typer.Argument(help="The command to run, after `--`", show_default=False),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="A label for the holder record and receipt"),
    ] = None,
    wait: Annotated[
        float,
        typer.Option(
            "--wait",
            min=0,
            help="Seconds to wait for the lock (0: do not wait)",
        ),
    ] = 3600.0,
) -> None:
    """Run COMMAND once the lock is free; exit with its exit code."""
    from piceli.maintenance import heavy

    def waiting(holder: dict[str, object]) -> None:
        say(f"waiting for the heavy-work lock held by {heavy.describe_holder(holder)}")

    try:
        receipt = heavy.run(
            command or [],
            name=name,
            wait=wait,
            on_wait=waiting,
            stdout=_stderr_fd(),
        )
    except heavy.HeavyError as error:
        reject(error.code, str(error))
    emit_json(receipt)
    say(
        f"heavy run finished: exit {receipt['exit_code']} in "
        f"{receipt['duration_seconds']}s (waited {receipt['waited_seconds']}s)"
    )
    raise typer.Exit(receipt["exit_code"])


@app.command("status")
def status(
    json_output: Annotated[
        bool, typer.Option("--json", help="Print one JSON object on stdout")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Receipts to list")] = 10,
) -> None:
    """Show who holds the lock and the most recent receipts."""
    from piceli.maintenance.heavy import HeavyLock, describe_holder

    document = HeavyLock().status(limit)
    holder = document["holder"]
    say(f"heavy-work lock: {document['state']}")
    if holder is not None:
        say(f"  held by {describe_holder(holder)}")
    for item in document["receipts"]:
        say(
            f"  {item.get('started_at')} exit {item.get('exit_code')} "
            f"{item.get('duration_seconds')}s {' '.join(item.get('command') or [])}"
        )
    if json_output:
        emit_json(document)


def register(root: typer.Typer) -> None:
    """Add ``heavy`` to the root application."""
    root.add_typer(app, name="heavy")
