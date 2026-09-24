"""``piceli render``: print the manifests of an app or composition, without a cluster.

Contract:

- Reads the target module and, with ``--spec``, the spec and the local receipt
  files it names. Never contacts a cluster, never reads or generates secret
  values (secret inputs are placeholders), never writes files. Safe to retry.
- stdout: the manifests (YAML documents, or one JSON object with
  ``--format json``). stderr: errors.
- Exit codes: ``0`` rendered, ``2`` rejected (the target or spec cannot be
  loaded, or the model is invalid). With ``--format json`` a rejection prints
  ``{"state": "rejected", "reason": "render-target-invalid" |
  "render-model-invalid", "message": …}``.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

EXIT_REJECTED = 2


class OutputFormat(StrEnum):
    yaml = "yaml"
    json = "json"


def render(
    target: Annotated[
        str | None,
        typer.Argument(
            help=(
                "module:attr or path/to/file.py:attr: an App, a composition "
                "function of the release context, or a DeploymentComposition. "
                "Defaults to the spec's [release] composition."
            ),
            show_default=False,
        ),
    ] = None,
    spec: Annotated[
        Path | None,
        typer.Option(
            "--spec",
            help=(
                "release.toml providing the namespace, images, secret inputs "
                "(as placeholders), [values] and declared nodes"
            ),
            exists=True,
            dir_okay=False,
        ),
    ] = None,
    namespace: Annotated[
        str | None,
        typer.Option(
            "--namespace",
            help="Namespace to render into (default: the spec's, else 'default')",
        ),
    ] = None,
    output: Annotated[
        OutputFormat, typer.Option("--format", help="Output format")
    ] = OutputFormat.yaml,
) -> None:
    """Print the manifests of a typed app or composition. Never contacts a cluster."""
    from pydantic import ValidationError

    from piceli.app import render as rendering
    from piceli.k8s.release_spec import ReleaseSpecError

    def reject(reason: str, message: str) -> None:
        if output is OutputFormat.json:
            typer.echo(
                json.dumps(
                    {"state": "rejected", "reason": reason, "message": message},
                    sort_keys=True,
                )
            )
        typer.echo(f"render rejected ({reason}): {message}", err=True)
        raise typer.Exit(EXIT_REJECTED)

    inputs = {}
    try:
        if spec is not None:
            loaded, context = rendering.spec_context(spec, namespace)
            inputs = dict(context.secrets)
            value = (
                rendering.load_target(target, Path.cwd())
                if target
                else loaded.load_composition()
            )
        elif target:
            context = rendering.empty_context(namespace or "default")
            value = rendering.load_target(target, Path.cwd())
        else:
            reject("render-target-invalid", "pass a TARGET, --spec, or both")
            return
    except (rendering.RenderError, ReleaseSpecError) as error:
        reject("render-target-invalid", str(error))
        return
    try:
        composition = rendering.render_target(value, context)
    except rendering.RenderError as error:
        reject("render-target-invalid", str(error))
        return
    except (ValidationError, ValueError) as error:
        reject("render-model-invalid", str(error))
        return
    components = rendering.rendered(composition, inputs)
    if output is OutputFormat.json:
        typer.echo(rendering.to_json(context.namespace, components))
    else:
        typer.echo(rendering.to_yaml(components), nl=False)
