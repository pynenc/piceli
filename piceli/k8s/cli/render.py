"""``piceli render``: print the manifests of an app, composition or pipeline, without a cluster.

Contract:

- Reads the target module and, with ``--spec``, the spec and the local receipt
  files it names. Never contacts a cluster, never reads or generates secret
  values (secret inputs are placeholders), never writes files. Safe to retry.
- A ``Pipeline`` target renders with its target's namespace and declared
  nodes (``node="alias"`` pins resolve), build images as placeholders and
  the delivery node's pin; it reads no kubeconfig, build spec or state and
  takes no ``--spec``.
- stdout: the manifests (YAML documents, or one JSON object
  ``{"state": "rendered", …}`` with ``--format json``). stderr: human text.
- Exit codes: ``0`` rendered, ``2`` rejected (the target or spec cannot be
  loaded, or the model is invalid). A rejection always prints one JSON object
  on stdout, whatever ``--format`` says: ``{"state": "rejected", "reason":
  "render-target-invalid" | "render-model-invalid", "message": …}``
  (:func:`piceli.cli_contract.reject`).
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import reject


class OutputFormat(StrEnum):
    yaml = "yaml"
    json = "json"


def render(
    target: Annotated[
        str | None,
        typer.Argument(
            help=(
                "module:attr or path/to/file.py:attr: an App, a composition "
                "function of the release context, a DeploymentComposition, or a "
                "Pipeline (rendered with its target's namespace and nodes). "
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
        ),
    ] = None,
    namespace: Annotated[
        str | None,
        typer.Option(
            "--namespace",
            help=(
                "Namespace to render into (default: the spec's or the pipeline "
                "target's, else 'default')"
            ),
        ),
    ] = None,
    output: Annotated[
        OutputFormat, typer.Option("--format", help="Output format")
    ] = OutputFormat.yaml,
) -> None:
    """Print the manifests of a typed app, composition or pipeline. Never contacts a cluster."""
    from pydantic import ValidationError

    from piceli.app import render as rendering
    from piceli.k8s.release_spec import ReleaseSpecError
    from piceli.pipeline import Pipeline, PipelineError

    inputs = {}
    try:
        if spec is None and target:
            value = rendering.load_target(target, Path.cwd())
            if isinstance(value, Pipeline):
                _render_pipeline(value, namespace, output)
                return
    except rendering.RenderError as error:
        reject("render-target-invalid", str(error))
    except PipelineError as error:
        reject("render-target-invalid", str(error))
    if spec is not None and not spec.is_file():
        reject("render-target-invalid", f"release spec not found: {spec}")
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
        else:
            reject("render-target-invalid", "pass a TARGET, --spec, or both")
    except (rendering.RenderError, ReleaseSpecError, OSError) as error:
        reject("render-target-invalid", str(error))
    if isinstance(value, Pipeline):
        reject(
            "render-target-invalid",
            "a Pipeline renders with its target; drop --spec",
        )
    try:
        composition = rendering.render_target(value, context)
    except rendering.RenderError as error:
        reject("render-target-invalid", str(error))
    except (ValidationError, ValueError) as error:
        reject("render-model-invalid", str(error))
    components = rendering.rendered(composition, inputs)
    if output is OutputFormat.json:
        typer.echo(rendering.to_json(context.namespace, components))
    else:
        typer.echo(rendering.to_yaml(components), nl=False)


def _render_pipeline(
    pipeline: Any,
    namespace: str | None,
    output: OutputFormat,
) -> None:
    """Render a Pipeline offline: its target's namespace and declared nodes."""
    from pydantic import ValidationError

    from piceli.app import render as rendering
    from piceli.pipeline import PipelineError
    from piceli.pipeline.compose import offline_composition

    if namespace is not None and namespace != pipeline.target.namespace:
        reject(
            "render-target-invalid",
            f"the pipeline's target namespace is {pipeline.target.namespace!r}; "
            "a Pipeline renders into its target's namespace",
        )
    try:
        context, composition = offline_composition(pipeline)
    except PipelineError as error:
        model = error.code == "render-model-invalid"
        reject("render-model-invalid" if model else "render-target-invalid", str(error))
    except (ValidationError, ValueError) as error:
        reject("render-model-invalid", str(error))
    components = rendering.rendered(composition, dict(context.secrets))
    if output is OutputFormat.json:
        typer.echo(rendering.to_json(context.namespace, components))
    else:
        typer.echo(rendering.to_yaml(components), nl=False)
