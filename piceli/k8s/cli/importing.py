"""``piceli import live|yaml``: generate a typed app module from existing objects.

Contract (see ``docs/migrate_from_kubectl.md``):

- ``import live`` reads one namespace through an explicit ``--kubeconfig`` and
  ``--context`` and never writes to the cluster. ``import yaml`` reads local
  files only. Secret values are never read into the output.
- With ``--out FILE`` the module is written there (refused if it exists,
  unless ``--force``) and stdout carries one JSON summary object. Without
  ``--out`` stdout carries the module source, or with ``--json`` one JSON
  object that includes it as ``module``. Human text goes to stderr.
- Exit codes: ``0`` imported, ``2`` rejected
  (``{"state": "rejected", "reason": "<code>"}`` on stdout).
- Safe to retry: the result is deterministic for the same objects.
"""

from __future__ import annotations

import os
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import emit_json, reject, say

app = typer.Typer(
    rich_markup_mode=None,
    help="Generate a typed app module from live objects or manifest files.",
    no_args_is_help=True,
)


class Transport(StrEnum):
    https = "https"
    loopback_http = "loopback-http"


Select = Annotated[
    list[str] | None,
    typer.Option(
        "--select",
        help="Kind/name or label=value; import only matching objects (repeatable)",
        show_default=False,
    ),
]
Out = Annotated[
    Path | None,
    typer.Option(
        "--out",
        help="Write the module here (stdout: a JSON summary)",
        dir_okay=False,
        show_default=False,
    ),
]
Force = Annotated[
    bool, typer.Option("--force", help="Overwrite an existing --out file")
]
Name = Annotated[
    str | None,
    typer.Option(
        "--name", help="App name (default: the namespace)", show_default=False
    ),
]
Json = Annotated[
    bool,
    typer.Option("--json", help="Without --out: print one JSON object with the module"),
]


def _write(path: Path, text: str, force: bool) -> None:
    if path.exists() and not force:
        say(f"{path} exists; pass --force to overwrite it")
        reject("import-output-refused")
    if not path.parent.is_dir():
        say(f"directory {path.parent} does not exist")
        reject("import-output-refused")
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _finish(result: Any, out: Path | None, force: bool, as_json: bool) -> None:
    summary = result.summary()
    objects = result.objects
    counts = {
        mode: sum(1 for item in objects if item.mode == mode)
        for mode in ("typed", "raw", "existing-claim")
    }
    untyped = sum(len(item.untyped) for item in objects)
    say(
        f"imported {len(objects)} objects from {result.source} namespace "
        f"{result.namespace}: {counts['typed']} typed, {counts['raw']} raw, "
        f"{counts['existing-claim']} existing claims; {untyped} untyped fields "
        f"kept as overrides; {len(result.secrets)} secret inputs to declare "
        "(listed in the module docstring)"
    )
    for item in result.skipped:
        say(f"skipped {item.kind}/{item.name}: {item.reason}")
    if out is not None:
        _write(out, result.module, force)
        say(f"wrote {out}")
        emit_json({"state": "imported", "out": str(out), **summary})
    elif as_json:
        emit_json(
            {"state": "imported", "out": None, "module": result.module, **summary}
        )
    else:
        typer.echo(result.module, nl=False)


def _run(function: Any, *args: Any, **kwargs: Any) -> Any:
    from piceli.importing import ImportFailure

    try:
        return function(*args, **kwargs)
    except ImportFailure as error:
        say(str(error))
        reject(error.code)


@app.command("live")
def live(
    kubeconfig: Annotated[
        Path,
        typer.Option(
            "--kubeconfig",
            help="Explicit kubeconfig file (never ~/.kube/config or KUBECONFIG)",
            dir_okay=False,
        ),
    ],
    context: Annotated[
        str,
        typer.Option("--context", help="Explicit context (never current-context)"),
    ],
    namespace: Annotated[str, typer.Option("--namespace", help="Namespace to import")],
    selectors: Select = None,
    out: Out = None,
    force: Force = False,
    name: Name = None,
    as_json: Json = False,
    transport: Annotated[
        Transport,
        typer.Option(
            "--transport",
            help="https, or loopback-http for a local test API server only",
        ),
    ] = Transport.https,
) -> None:
    """Generate a typed module from the objects of a live namespace (read-only)."""
    from piceli.importing import import_live

    result = _run(
        import_live,
        kubeconfig,
        context,
        namespace,
        selectors=selectors or [],
        app_name=name,
        transport=transport.value,
    )
    _finish(result, out, force, as_json)


@app.command("yaml")
def yaml_command(
    directory: Annotated[
        Path,
        typer.Argument(
            help="Directory of *.yaml, *.yml and *.json manifests (read recursively)",
            file_okay=False,
        ),
    ],
    namespace: Annotated[
        str | None,
        typer.Option(
            "--namespace",
            help="Namespace to render into (default: the one the files name, else 'default')",
            show_default=False,
        ),
    ] = None,
    selectors: Select = None,
    out: Out = None,
    force: Force = False,
    name: Name = None,
    as_json: Json = False,
) -> None:
    """Generate a typed module from a directory of manifests (no cluster)."""
    from piceli.importing import import_yaml

    result = _run(
        import_yaml,
        directory,
        namespace=namespace,
        selectors=selectors or [],
        app_name=name,
    )
    _finish(result, out, force, as_json)
