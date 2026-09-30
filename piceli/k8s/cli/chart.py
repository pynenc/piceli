"""``piceli chart``: an app as a Helm chart, a chart archive or plain manifests.

Contract (every subcommand):

- Renders the target exactly like ``piceli render`` (same TARGET, ``--spec``,
  ``--namespace``, ``--env``) and turns it into a chart
  (:func:`piceli.chart.build_chart`). Never contacts a cluster; Secrets are
  never rendered (the chart references them by name); an object holding a
  redacted or apply-time secret value is refused (``chart-secret-value``).
  The chart is deterministic: the same render, name and version give the
  same files and archive bytes.
- ``chart render --out DIR`` writes the chart directory (``DIR`` must be
  absent, empty or written by a previous ``chart render``) and prints
  ``{"state": "written", …}``.
- ``chart package --out DIR`` writes ``<name>-<version>.tgz`` into ``DIR``
  and prints ``{"state": "packaged", "digest": …}``; an existing archive with
  other content is refused (bump ``--version``).
- ``chart manifests [--values FILE] [--out DIR]`` applies a values file
  (merged over the defaults like Helm, validated against the schema) and
  prints the plain manifests as YAML, or writes one file per object like
  ``render --out``.
- ``chart publish --to oci://host[:port]/path`` pushes the chart as
  ``path/<name>:<version>`` in the layout of ``helm push``. Without
  ``--approve`` nothing is sent: it prints the manifest digest and exits
  ``3``; ``--approve DIGEST`` pushes by digest, then the tag, and reads both
  back. Credentials only from ``--credentials FILE``, never printed.
- Exit codes: ``0`` done, ``1`` the push failed, ``2`` rejected, ``3``
  approval required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from piceli.cli_contract import EXIT_APPROVAL, emit_json, fail, reject, say
from piceli.errors import ERRORS

app = typer.Typer(
    rich_markup_mode=None,
    help="Render an app as a Helm chart, a chart archive or plain manifests "
    "with values; publish the chart to an OCI registry.",
)

Target = Annotated[
    str | None,
    typer.Argument(
        help="module:attr or path/to/file.py:attr, as for `piceli render`",
        show_default=False,
    ),
]
Spec = Annotated[
    Path | None, typer.Option("--spec", help="release.toml, as for `piceli render`")
]
Namespace = Annotated[
    str | None,
    typer.Option(
        "--namespace",
        help="Namespace to render into; the chart installs into the release "
        "namespace (--namespace of helm) instead",
    ),
]
Env = Annotated[
    str | None, typer.Option("--env", help="Render this environment of the App")
]
Name = Annotated[
    str | None,
    typer.Option(
        "--name",
        help="Chart name (default: the App name, its app.kubernetes.io/part-of label)",
        show_default=False,
    ),
]
Version = Annotated[str, typer.Option("--version", help="Chart version (SemVer 2)")]
AppVersion = Annotated[
    str | None,
    typer.Option(
        "--app-version",
        help="appVersion of Chart.yaml (default: the chart version)",
        show_default=False,
    ),
]


def _reject(reason: str, message: str) -> NoReturn:
    reject(reason, message)


def _chart(
    target: str | None,
    spec: Path | None,
    namespace: str | None,
    env: str | None,
    name: str | None,
    version: str,
    app_version: str | None,
) -> Any:
    from piceli.chart import ChartError, build_chart, chart_name
    from piceli.k8s.cli.render import _render

    if not target and spec is None:
        reject("render-target-invalid", "pass a TARGET, --spec, or both")
    rendered_namespace, components, _ = _render(target, spec, namespace, env, _reject)
    chosen = name or chart_name(components)
    if chosen is None:
        reject(
            "chart-invalid",
            "the render has no single app.kubernetes.io/part-of label; pass --name",
        )
    try:
        return build_chart(
            components,
            name=chosen,
            version=version,
            namespace=rendered_namespace,
            app_version=app_version,
        )
    except ChartError as error:
        reject(error.code, str(error))


def _summary(chart: Any, env: str | None) -> dict[str, Any]:
    body = chart.public()
    if env is not None:
        body["environment"] = env
    return body


def _explain(chart: Any) -> None:
    for path in chart.required:
        say(f"  required value (no default): {path}")
    for secret in chart.secrets:
        keys = ", ".join(secret.keys) or "-"
        say(f"  existing Secret the chart reads: {secret.name} (keys: {keys})")
    for claim in chart.existing_claims:
        say(f"  existing volume claim the pods mount: {claim}")


@app.command("render")
def render(
    target: Target = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Chart directory to write; absent, empty or a previous chart render",
            show_default=False,
        ),
    ] = None,
    spec: Spec = None,
    namespace: Namespace = None,
    env: Env = None,
    name: Name = None,
    version: Version = "0.1.0",
    app_version: AppVersion = None,
) -> None:
    """Write the app as a Helm chart directory (Chart.yaml, values, schema, templates)."""
    from piceli.chart import ChartError, archive_digest, write_chart

    if out is None:
        reject("chart-out-refused", "pass --out DIR for the chart directory")
    chart = _chart(target, spec, namespace, env, name, version, app_version)
    try:
        files = write_chart(out, chart)
    except ChartError as error:
        reject(error.code, str(error))
    except OSError as error:
        reject("chart-out-refused", f"cannot write {out}: {type(error).__name__}")
    emit_json(
        {
            "state": "written",
            "directory": str(out),
            "files": files,
            "archive_digest": archive_digest(chart.archive()),
            **_summary(chart, env),
        }
    )
    say(f"wrote chart {chart.name} {chart.version} ({len(files)} file(s)) to {out}")
    _explain(chart)


@app.command("package")
def package(
    target: Target = None,
    out: Annotated[
        Path,
        typer.Option(
            "--out", help="Directory for <name>-<version>.tgz (created if absent)"
        ),
    ] = Path("."),
    spec: Spec = None,
    namespace: Namespace = None,
    env: Env = None,
    name: Name = None,
    version: Version = "0.1.0",
    app_version: AppVersion = None,
) -> None:
    """Write the chart archive <name>-<version>.tgz (byte-identical per chart)."""
    from piceli.chart import archive_digest

    chart = _chart(target, spec, namespace, env, name, version, app_version)
    data = chart.archive()
    path = out / chart.archive_name
    try:
        if path.is_symlink() or (path.exists() and path.read_bytes() != data):
            reject(
                "chart-out-refused",
                f"{chart.archive_name} exists with other content; bump --version",
            )
        out.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as error:
        reject("chart-out-refused", f"cannot write {path}: {type(error).__name__}")
    emit_json(
        {
            "state": "packaged",
            "file": str(path),
            "digest": archive_digest(data),
            "size": len(data),
            **_summary(chart, env),
        }
    )
    say(f"packaged {path}")
    _explain(chart)


@app.command("manifests")
def manifests(
    target: Target = None,
    values: Annotated[
        list[Path] | None,
        typer.Option(
            "--values",
            "-f",
            help="YAML values file merged over the chart defaults (repeatable; "
            "later files win), as for helm install --values",
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Write one YAML file per object (as render --out) instead of printing",
            show_default=False,
        ),
    ] = None,
    install_namespace: Annotated[
        str | None,
        typer.Option(
            "--install-namespace",
            help="Namespace the manifests install into (default: the render's)",
            show_default=False,
        ),
    ] = None,
    spec: Spec = None,
    namespace: Namespace = None,
    env: Env = None,
    name: Name = None,
    version: Version = "0.1.0",
    app_version: AppVersion = None,
) -> None:
    """Print (or write) plain manifests with a values file applied; no Helm needed."""
    import yaml

    from piceli.app import render as rendering
    from piceli.chart import ChartError, merge_values

    chart = _chart(target, spec, namespace, env, name, version, app_version)
    merged: dict[str, Any] = {}
    for item in values or ():
        loaded: Any = None
        try:
            loaded = yaml.safe_load(item.read_text()) or {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            pass
        if not isinstance(loaded, dict):
            reject(
                "chart-values-invalid",
                f"{item} is not a readable YAML mapping of values",
            )
        merged = merge_values(merged, loaded)
    try:
        components = chart.manifests(merged, install_namespace)
    except ChartError as error:
        reject(error.code, str(error))
    if out is None:
        typer.echo(rendering.to_yaml(components), nl=False)
        return
    from piceli.gitops import GitOpsError, handoff, write_directory

    try:
        written = write_directory(out, handoff(components, "external"))
    except GitOpsError as error:
        reject(
            error.code
            if error.code == "render-out-refused"
            else "chart-values-invalid",
            str(error),
        )
    except OSError as error:
        reject("render-out-refused", f"cannot write {out}: {type(error).__name__}")
    emit_json(
        {
            "state": "written",
            "directory": str(out),
            "files": written["files"],
            "removed": written["removed"],
            **_summary(chart, env),
        }
    )
    say(f"wrote {len(written['files'])} file(s) to {out}")


@app.command("publish")
def publish(
    target: Target = None,
    to: Annotated[
        str,
        typer.Option(
            "--to",
            help="oci://host[:port]/path; the chart goes to path/<name>:<version>",
            show_default=False,
        ),
    ] = "",
    spec: Spec = None,
    namespace: Namespace = None,
    env: Env = None,
    name: Name = None,
    version: Version = "0.1.0",
    app_version: AppVersion = None,
    approve: Annotated[
        str | None,
        typer.Option(
            "--approve",
            help="The digest printed without --approve; pushes it",
            show_default=False,
        ),
    ] = None,
    credentials: Annotated[
        Path | None,
        typer.Option(
            "--credentials",
            help='Private JSON file: {"username", "password"} or {"token"}',
        ),
    ] = None,
    ca_file: Annotated[
        Path | None,
        typer.Option("--ca-file", help="CA bundle for a TLS registry"),
    ] = None,
) -> None:
    """Push the chart to an OCI registry as helm push does (needs --approve DIGEST)."""
    from piceli.artifacts.registry import RegistryTarget

    if not to:
        reject("chart-target-invalid", "pass --to oci://host[:port]/path")
    try:
        parsed = RegistryTarget.parse(to)
    except ValueError as error:
        code = str(getattr(error, "code", ""))
        reject(code if code in ERRORS else "chart-target-invalid", str(error))
    if parsed.tag is not None:
        reject(
            "chart-target-invalid",
            "--to names a path, not a tag: the chart is pushed as "
            "path/<name>:<version>",
        )
    chart = _chart(target, spec, namespace, env, name, version, app_version)
    try:
        registry = RegistryTarget(
            parsed.host,
            parsed.port,
            f"{parsed.repository}/{chart.name}",
            chart.tag,
            parsed.tls,
        )
    except ValueError as error:
        reject("chart-target-invalid", str(error))
    artifact = chart.oci()
    body: dict[str, Any] = {
        **_summary(chart, env),
        "digest": artifact.digest,
        "media_type": artifact.public()["media_type"],
        "config": artifact.config.descriptor(),
        "layer": artifact.layer.descriptor(),
        "target": registry.identity,
    }
    if approve is None:
        emit_json({"state": "approval-required", **body})
        say(
            f"chart {chart.name} {chart.version} for {registry.identity}; nothing was pushed"
        )
        _explain(chart)
        say(f"approve with: --approve {artifact.digest}")
        raise typer.Exit(EXIT_APPROVAL)
    if approve != artifact.digest:
        reject(
            "chart-artifact-changed",
            "the approved digest is not this chart's digest",
            digest=artifact.digest,
        )
    body.update(_push(registry, artifact, credentials, ca_file))
    emit_json({"state": "published", **body})
    say(f"published {body['url']} --version {chart.version}")


def _push(
    registry: Any, artifact: Any, credentials: Path | None, ca_file: Path | None
) -> dict[str, Any]:
    from piceli.artifacts.delivery_inputs import DeliveryInputError
    from piceli.artifacts.registry import (
        RegistryCredentials,
        RegistryError,
        StreamedOciRegistryClient,
    )
    from piceli.gitops import push

    try:
        loaded = (
            RegistryCredentials.load(credentials.absolute())
            if credentials is not None
            else None
        )
    except DeliveryInputError as error:
        code = getattr(error, "code", None) or "invalid-credentials-file"
        reject(code if code in ERRORS else "invalid-credentials-file")
    except (OSError, ValueError):
        reject("invalid-credentials-file")
    try:
        endpoint = registry.endpoint(
            credentials=loaded,
            ca_file=ca_file.absolute() if ca_file is not None else None,
        )
    except ValueError as error:
        reject("chart-target-invalid", str(error))
    client = StreamedOciRegistryClient(endpoint)
    try:
        counts = push(client, registry.repository, artifact, registry.tag)
    except RegistryError as error:
        code = error.reason if error.reason in ERRORS else "chart-push-failed"
        fail(code, f"push to {registry.registry} failed: {error.reason}")
    return {
        **counts,
        "reference": f"{registry.registry}/{registry.repository}@{artifact.digest}",
        "url": f"oci://{registry.registry}/{registry.repository}",
        "tag": registry.tag,
    }
