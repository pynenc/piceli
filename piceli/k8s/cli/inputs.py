"""Record and verify the git identity of declared build sources.

Machine JSON goes to stdout, human text to stderr. Refusals print
``{"state": "rejected", "reason": "<code>", "message": …}`` and exit 2
(see ``piceli explain <code>``); drift exits 1.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Annotated, Any, NoReturn

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
from piceli.cli_contract import EXIT_FAILED, emit_json, reject_error, say

app = typer.Typer(
    rich_markup_mode=None,
    help="Record and verify source identities (commit, dirty flag, diff digest).",
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


_ERRORS = (SourceIdentityError, ValueError, OSError)


def _reject(error: Exception, default: str) -> NoReturn:
    """Print the rejection for ``error`` (its own code, else ``default``); exit 2.

    ``default`` names the phase that failed: reading the spec, reading the
    lock, capturing the sources or writing the lock.
    """
    if default == "source-capture-failed" and not isinstance(
        error, SourceIdentityError
    ):
        # Outside git, capture only raises for the --timeout bounds or I/O.
        default = "inputs-io-error" if isinstance(error, OSError) else "invalid-timeout"
    extra: dict[str, Any] = {}
    if isinstance(error, UnknownSourceError):
        extra["source"] = error.name
    reject_error(error, default, **extra)


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
        declared = InputsSpec.from_toml(spec)
    except _ERRORS as error:
        _reject(error, "invalid-inputs-spec")
    try:
        lock = record_inputs(declared, timeout=timeout, only=only or None)
    except _ERRORS as error:
        _reject(error, "source-capture-failed")
    if out is not None:
        try:
            _write_atomic(out, lock.to_json())
        except OSError as error:
            _reject(error, "inputs-io-error")
    result: dict[str, Any] = {"state": "recorded", **lock.to_dict()}
    if out is not None:
        result["lock"] = str(out)
    emit_json(result)
    say(f"recorded {len(lock.sources)} source(s)" + (f" to {out}" if out else ""))


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
        declared = InputsSpec.from_toml(spec)
    except _ERRORS as error:
        _reject(error, "invalid-inputs-spec")
    try:
        recorded = InputsLock.from_json(lock.read_text())
    except _ERRORS as error:
        _reject(error, "inputs-lock-invalid")
    try:
        verification = verify_inputs(
            declared, recorded, timeout=timeout, only=only or None
        )
    except _ERRORS as error:
        _reject(error, "source-capture-failed")
    result = verification.to_dict()
    if not verification.ok:
        emit_json({**result, "reason": "source-drift"})
        say(f"source drift: {verification.summary()} [source-drift]")
        raise typer.Exit(EXIT_FAILED)
    emit_json(result)
    say(f"verified {len(result['sources'])} source(s): no drift")
