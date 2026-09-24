"""Record and verify the git identity of declared build sources."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.artifacts.source_identity import (
    DEFAULT_GIT_TIMEOUT,
    InputsLock,
    InputsSpec,
    SourceIdentityError,
    UnknownSourceError,
    record_inputs,
    verify_inputs,
)

app = typer.Typer(
    help="Record and verify source identities (commit, dirty flag, diff digest)."
)

SpecOption = Annotated[
    Path,
    typer.Option(
        "--spec", help="inputs.toml declaring the sources", exists=True, dir_okay=False
    ),
]
OnlyOption = Annotated[
    list[str] | None,
    typer.Option(
        "--only",
        help="Limit to this declared source (repeatable); same spec as the build",
    ),
]
TimeoutOption = Annotated[
    float, typer.Option("--timeout", help="Seconds allowed for each git call")
]


def _reject(error: Exception) -> None:
    body: dict[str, Any] = {"state": "rejected", "reason": str(error)}
    if isinstance(error, UnknownSourceError):
        body = {"state": "rejected", "reason": error.code, "source": error.name}
    typer.echo(json.dumps(body, sort_keys=True), err=True)
    raise typer.Exit(2)


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


@app.command("record")
def record(
    spec: SpecOption,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write the lock here (default: stdout only)"),
    ] = None,
    timeout: TimeoutOption = DEFAULT_GIT_TIMEOUT,
    only: OnlyOption = None,
) -> None:
    """Capture each declared source (or the ``--only`` ones) and write a lock."""
    try:
        lock = record_inputs(
            InputsSpec.from_toml(spec), timeout=timeout, only=only or None
        )
        if out is not None:
            _write_atomic(out, lock.to_json())
    except (SourceIdentityError, ValueError, OSError) as error:
        _reject(error)
        return
    result: dict[str, Any] = {"state": "recorded", **lock.to_dict()}
    if out is not None:
        result["lock"] = str(out)
    typer.echo(json.dumps(result, sort_keys=True))


@app.command("verify")
def verify(
    spec: SpecOption,
    lock: Annotated[
        Path,
        typer.Option(
            "--lock",
            help="Lock written by `inputs record`",
            exists=True,
            dir_okay=False,
        ),
    ],
    timeout: TimeoutOption = DEFAULT_GIT_TIMEOUT,
    only: OnlyOption = None,
) -> None:
    """Recapture the sources and compare them with the lock (exit 1 on drift).

    ``--only NAME`` checks just that source against the lock's entry for it.
    """
    try:
        verification = verify_inputs(
            InputsSpec.from_toml(spec),
            InputsLock.from_json(lock.read_text()),
            timeout=timeout,
            only=only or None,
        )
    except (SourceIdentityError, ValueError, OSError) as error:
        _reject(error)
        return
    typer.echo(json.dumps(verification.to_dict(), sort_keys=True))
    if not verification.ok:
        typer.echo(f"source drift: {verification.summary()}", err=True)
        raise typer.Exit(1)
