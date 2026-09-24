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
TimeoutOption = Annotated[
    float, typer.Option("--timeout", help="Seconds allowed for each git call")
]


def _reject(error: Exception) -> None:
    typer.echo(
        json.dumps({"state": "rejected", "reason": str(error)}, sort_keys=True),
        err=True,
    )
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
) -> None:
    """Capture each declared source and write an inputs lock."""
    try:
        lock = record_inputs(InputsSpec.from_toml(spec), timeout=timeout)
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
) -> None:
    """Recapture the sources and compare them with the lock (exit 1 on drift)."""
    try:
        verification = verify_inputs(
            InputsSpec.from_toml(spec),
            InputsLock.from_json(lock.read_text()),
            timeout=timeout,
        )
    except (SourceIdentityError, ValueError, OSError) as error:
        _reject(error)
        return
    typer.echo(json.dumps(verification.to_dict(), sort_keys=True))
    if not verification.ok:
        typer.echo(f"source drift: {verification.summary()}", err=True)
        raise typer.Exit(1)
