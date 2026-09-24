"""``piceli explain`` and ``piceli help-json``: the CLI contract, as commands."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from piceli.cli_contract import emit_json, help_tree, reject
from piceli.errors import AREAS, lookup


def explain(
    code: Annotated[str, typer.Argument(help="Error code, e.g. tool-pin-mismatch")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the entry as one JSON object")
    ] = False,
) -> None:
    """Explain an error code: cause, fix and whether a retry can succeed."""
    entry = lookup(code.strip())
    if entry is None:
        reject("unknown-error-code")
    if as_json:
        emit_json(entry.to_dict())
        return
    typer.echo(
        f"{entry.code}: {entry.title}\n"
        f"  area:       {entry.area} ({AREAS[entry.area]})\n"
        f"  cause:      {entry.cause}\n"
        f"  fix:        {entry.fix}\n"
        f"  retry-safe: {'yes' if entry.retry_safe else 'no'}"
    )


def help_json() -> None:
    """Print the whole CLI tree (commands, options, contracts) as JSON."""
    typer.echo(json.dumps(help_tree(), sort_keys=True, indent=2))


def register(app: typer.Typer) -> None:
    """Add ``explain`` and ``help-json`` to the root application."""
    app.command("explain")(explain)
    app.command("help-json")(help_json)
