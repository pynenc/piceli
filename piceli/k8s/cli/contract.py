"""``piceli explain`` and ``piceli help-json``: the CLI contract, as commands."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from piceli.cli_contract import emit_json, help_tree, reject
from piceli.errors import AREAS, lookup


def explain(
    code: Annotated[
        str | None,
        typer.Argument(help="Error code, e.g. tool-pin-mismatch", show_default=False),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the entry as one JSON object")
    ] = False,
    run: Annotated[
        str | None,
        typer.Option(
            "--run",
            help="Explain a past execution instead (execution id, unique prefix or "
            "`piceli deploy` run id; needs --spec): same as `piceli release "
            "status --run`",
        ),
    ] = None,
    spec: Annotated[
        str | None,
        typer.Option(
            "--spec",
            help="With --run: path/to/release.toml or MODULE:ATTR of a Pipeline",
        ),
    ] = None,
    env: Annotated[
        str | None,
        typer.Option("--env", help="With --run: the pipeline's environment"),
    ] = None,
) -> None:
    """Explain an error code: cause, fix and whether a retry can succeed.

    With ``--run ID --spec SPEC``: why a past execution failed, from the local
    journal (the causes found in the pods, with redacted log tails and events).
    """
    if run is not None:
        if spec is None or code is not None:
            reject(
                "explain-run-needs-spec",
                "--run explains a past execution of --spec; pass --spec and no code",
            )
        from piceli.k8s.cli.release import status

        status(spec, env=env, run=run)
        return
    entry = lookup((code or "").strip())
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
