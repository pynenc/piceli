"""``piceli render``: print the manifests of an app, composition or pipeline, without a cluster.

Contract:

- Reads the target module and, with ``--spec``, the spec and the local receipt
  files it names. Never contacts a cluster, never reads or generates secret
  values (secret inputs are placeholders), never writes files. Safe to retry.
- A ``Pipeline`` target renders with its target's namespace and declared
  nodes (``node="alias"`` pins resolve), build images as placeholders and
  the delivery node's pin; it reads no kubeconfig, build spec or state and
  takes no ``--spec``.
- ``--env NAME`` renders one environment of the App (its typed overrides; a
  pipeline also uses its target for that environment). ``--diff-env OTHER``
  renders both and prints their typed difference instead of manifests.
- stdout: the manifests (YAML documents, or one JSON object
  ``{"state": "rendered", …}`` with ``--format json``); with ``--diff-env``,
  the difference (text, or one JSON object ``{"state": "diffed", …}``).
  stderr: human text.
- Exit codes: ``0`` rendered, ``2`` rejected (the target or spec cannot be
  loaded, the model is invalid, or an environment is unknown or invalid). A
  rejection always prints one JSON object on stdout, whatever ``--format``
  says: ``{"state": "rejected", "reason": "render-target-invalid" |
  "render-model-invalid" | "environment-…", "message": …}``
  (:func:`piceli.cli_contract.reject`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from piceli.cli_contract import reject as contract_reject


class OutputFormat(StrEnum):
    yaml = "yaml"
    json = "json"


Rejector = Callable[[str, str], NoReturn]


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
                "Namespace to render into (default: the spec's, the pipeline "
                "target's or the environment's, else 'default')"
            ),
        ),
    ] = None,
    output: Annotated[
        OutputFormat, typer.Option("--format", help="Output format")
    ] = OutputFormat.yaml,
    env: Annotated[
        str | None,
        typer.Option(
            "--env",
            help="Render this environment of the App (app.environment(...)); a "
            "pipeline also uses its target for it",
            show_default=False,
        ),
    ] = None,
    diff_env: Annotated[
        str | None,
        typer.Option(
            "--diff-env",
            help="Print the typed difference between --env and this "
            "environment instead of manifests",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Print the manifests of a typed app, composition or pipeline. Never contacts a cluster."""
    from piceli.app import render as rendering

    def reject(reason: str, message: str) -> NoReturn:
        # One JSON rejection object on stdout whatever --format says.
        contract_reject(reason, message)

    if diff_env is not None and env is None:
        reject("environment-required", "--diff-env compares with --env; pass both")
    if not target and spec is None:
        reject("render-target-invalid", "pass a TARGET, --spec, or both")
    first = _render(target, spec, namespace, env, reject)
    if diff_env is None:
        name, components, _ = first
        if output is OutputFormat.json:
            typer.echo(rendering.to_json(name, components, env))
        else:
            typer.echo(rendering.to_yaml(components), nl=False)
        return
    from piceli.app.environment import diff_text, environment_diff

    second = _render(target, spec, namespace, diff_env, reject)
    diff = environment_diff(
        (env or "", first[0], first[2], first[1]),
        (diff_env, second[0], second[2], second[1]),
    )
    if output is OutputFormat.json:
        typer.echo(json.dumps({"state": "diffed", **diff}, indent=2, sort_keys=True))
    else:
        typer.echo(diff_text(diff), nl=False)


def _render(
    target: str | None,
    spec: Path | None,
    namespace: str | None,
    env: str | None,
    reject: Rejector,
) -> tuple[str, list[dict[str, Any]], Any]:
    """``(namespace, rendered components, environment or None)`` of one render."""
    from pydantic import ValidationError

    from piceli.app import render as rendering
    from piceli.app.environment import EnvironmentInvalid
    from piceli.k8s.release_spec import ReleaseSpecError
    from piceli.pipeline import Pipeline, PipelineError

    inputs = {}
    try:
        if spec is None and target:
            value = rendering.load_target(target, Path.cwd())
            if isinstance(value, Pipeline):
                return _render_pipeline(value, namespace, env, reject)
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
        else:
            context = rendering.empty_context(
                namespace or rendering.environment_namespace(value, env) or "default"
            )
    except (rendering.RenderError, ReleaseSpecError, OSError) as error:
        reject("render-target-invalid", str(error))
    if isinstance(value, Pipeline):
        reject(
            "render-target-invalid",
            "a Pipeline renders with its target; drop --spec",
        )
    try:
        composition, app = rendering.render_app_target(value, context, env)
    except rendering.RenderError as error:
        reject("render-target-invalid", str(error))
    except EnvironmentInvalid as error:
        reject(error.code, str(error))
    except (ValidationError, ValueError) as error:
        reject("render-model-invalid", str(error))
    environment = app.selected_environment if app is not None else None
    return context.namespace, rendering.rendered(composition, inputs), environment


def _render_pipeline(
    pipeline: Any,
    namespace: str | None,
    env: str | None,
    reject: Rejector,
) -> tuple[str, list[dict[str, Any]], Any]:
    """Render a Pipeline offline: its target's namespace and declared nodes."""
    from pydantic import ValidationError

    from piceli.app import render as rendering
    from piceli.pipeline import PipelineError
    from piceli.pipeline.compose import offline_composition

    try:
        if env is not None:
            pipeline = pipeline.for_environment(env)
        target = pipeline.target
    except PipelineError as error:
        reject(error.code, str(error))
    if namespace is not None and namespace != target.namespace:
        reject(
            "render-target-invalid",
            f"the pipeline's target namespace is {target.namespace!r}; "
            "a Pipeline renders into its target's namespace",
        )
    try:
        context, composition = offline_composition(pipeline)
    except PipelineError as error:
        model = error.code == "render-model-invalid"
        reject("render-model-invalid" if model else "render-target-invalid", str(error))
    except (ValidationError, ValueError) as error:
        reject("render-model-invalid", str(error))
    return (
        context.namespace,
        rendering.rendered(composition, dict(context.secrets)),
        pipeline.environment,
    )
