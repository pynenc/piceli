"""``piceli bundle`` and ``piceli support-bundle``: delivery to a client's cluster.

Contract:

- ``bundle TARGET --env ENV --out DIR`` renders the target like ``piceli
  render`` (same TARGET and ``--env``), checks it against the safety gate for
  foreign clusters and writes the bundle into ``DIR`` (absent or empty): a
  kustomize base and ``overlays/dev``, ``prepare.sh``, the image archives of
  ``--receipt`` (``--platform``, default ``linux/amd64``; ``--all-platforms``),
  ``INSTALL.md``, ``UNINSTALL.md``, ``bundle.json`` and ``SHA256SUMS``. It
  never contacts a cluster or registry and writes no secret value. Prints
  ``{"state": "written", …}``; a safety violation is refused with
  ``bundle-unsafe-<rule>`` and every violation in ``violations``.
- ``support-bundle --kubeconfig FILE --context CTX --namespace NS --out FILE``
  reads the namespace with GET requests only (never Secrets) and writes a
  redacted ``.tar.gz`` with a manifest (mode 0600, never overwritten).
- Exit codes: ``0`` done, ``2`` rejected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from piceli.cli_contract import emit_json, reject, say
from piceli.k8s.cli.cluster_access import (
    AllowExecOption,
    ContextOption,
    ExecSha256Option,
    exec_policy,
    guarded,
)


def _reject(reason: str, message: str) -> NoReturn:
    reject(reason, message)


def _load(
    target: str, env: str | None
) -> tuple[str, list[dict[str, Any]], Any, Any, Any]:
    """``(namespace, components, app, generators, HelmChart or None)``."""
    from piceli.app import App
    from piceli.app import render as rendering
    from piceli.bundle.helm import HelmChart
    from piceli.k8s.cli.render import _render, _render_pipeline
    from piceli.pipeline import Pipeline, PipelineError

    try:
        value = rendering.load_target(target, Path.cwd())
    except (rendering.RenderError, PipelineError) as error:
        reject("render-target-invalid", str(error))
    except Exception as error:  # the module raised while importing
        reject(
            "render-target-invalid",
            f"importing {target} failed: {type(error).__name__}",
        )
    declaration = None
    if isinstance(value, HelmChart):
        declaration, value = value, value.target
        if not isinstance(value, Pipeline | App):
            reject(
                "render-target-invalid",
                "a HelmChart wraps a Pipeline or an App",
            )
        if isinstance(value, App):
            namespace, components, app = _render_app(value, env)
            return namespace, components, app, {}, declaration
    if isinstance(value, Pipeline):
        namespace, components, _ = _render_pipeline(value, None, env, _reject)
        try:
            chosen = value.for_environment(env) if env is not None else value
        except PipelineError as error:
            reject(error.code, str(error))
        return namespace, components, chosen.app, dict(chosen.secrets), declaration
    namespace, components, _ = _render(target, None, None, env, _reject)
    app = value if isinstance(value, App) else None
    if app is not None and env is not None:
        app = app.for_environment(env)
    return namespace, components, app, {}, declaration


def _render_app(value: Any, env: str | None) -> tuple[str, list[dict[str, Any]], Any]:
    """Render an App object offline, as ``piceli render`` renders its target."""
    from pydantic import ValidationError

    from piceli.app import render as rendering
    from piceli.app.environment import EnvironmentInvalid

    context = rendering.empty_context(
        rendering.environment_namespace(value, env) or "default"
    )
    try:
        composition, app = rendering.render_app_target(value, context, env)
    except EnvironmentInvalid as error:
        reject(error.code, str(error))
    except (rendering.RenderError, ValidationError, ValueError) as error:
        reject("render-model-invalid", str(error))
    return context.namespace, rendering.rendered(composition, {}), app


def _forward(app: Any, components: list[dict[str, Any]]) -> Any:
    from piceli.bundle.build import Forward

    if app is not None:
        for shortcut in app.shortcuts():
            if shortcut.target.startswith("service/"):
                return Forward(
                    shortcut.target.removeprefix("service/"),
                    shortcut.local_port,
                    shortcut.remote_port,
                )
    for component in components:
        for resource in component["resources"]:
            manifest = resource["manifest"]
            if manifest.get("kind") != "Service":
                continue
            ports = (manifest.get("spec") or {}).get("ports") or ()
            if ports and isinstance(ports[0].get("port"), int):
                port = ports[0]["port"]
                return Forward(
                    str(manifest["metadata"]["name"]),
                    port if port >= 1024 else 8080,
                    port,
                )
    return None


def bundle(
    target: Annotated[
        str,
        typer.Argument(
            help="module:attr or path/to/file.py:attr (a Pipeline or an App)"
        ),
    ],
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Bundle (or, with --helm, chart) directory to write (absent or empty)",
            show_default=False,
        ),
    ] = None,
    env: Annotated[
        str | None, typer.Option("--env", help="Render this environment (e.g. client)")
    ] = None,
    receipt: Annotated[
        Path | None,
        typer.Option(
            "--receipt",
            help="Build receipt (piceli artifacts build-spec run) holding the "
            "app's build images; their archives go into images/",
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            help="Directory the receipt's paths are relative to (default: its directory)",
        ),
    ] = None,
    platform: Annotated[
        list[str] | None,
        typer.Option(
            "--platform",
            help="Platform to put in the archives (repeatable; default linux/amd64)",
        ),
    ] = None,
    all_platforms: Annotated[
        bool,
        typer.Option(
            "--all-platforms", help="Every platform the receipt holds (multi-arch)"
        ),
    ] = False,
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            help="Bundle name and part-of label (default: the render's part-of label)",
            show_default=False,
        ),
    ] = None,
    version: Annotated[
        str, typer.Option("--version", help="Bundle version (X.Y.Z)")
    ] = "0.1.0",
    storage_class: Annotated[
        str | None,
        typer.Option(
            "--storage-class", help="storageClassName of every claim, in the overlay"
        ),
    ] = None,
    helm: Annotated[
        bool,
        typer.Option(
            "--helm",
            help="Write a Helm chart (Chart.yaml, values.yaml, templates/) instead "
            "of the kustomize bundle and prepare.sh",
        ),
    ] = False,
    app_version: Annotated[
        str | None,
        typer.Option(
            "--app-version",
            help="With --helm: appVersion of Chart.yaml (default: --version)",
            show_default=False,
        ),
    ] = None,
    push: Annotated[
        str | None,
        typer.Option(
            "--push",
            help="With --helm: oci://host[:port]/path; the chart goes to "
            "path/<name>:<version> (needs --approve DIGEST)",
            show_default=False,
        ),
    ] = None,
    approve: Annotated[
        str | None,
        typer.Option(
            "--approve",
            help="With --push: the digest printed without --approve; pushes it",
            show_default=False,
        ),
    ] = None,
    credentials: Annotated[
        Path | None,
        typer.Option(
            "--credentials",
            help='With --push: private JSON file, {"username", "password"} or {"token"}',
        ),
    ] = None,
    ca_file: Annotated[
        Path | None,
        typer.Option("--ca-file", help="With --push: CA bundle for a TLS registry"),
    ] = None,
) -> None:
    """Package the app for a cluster Piceli never accesses (kustomize, prepare.sh, images; or a Helm chart)."""
    from piceli.bundle.build import BundleError, build_bundle, part_of, write_bundle
    from piceli.bundle.images import (
        DEFAULT_PLATFORMS,
        ImageExportError,
        choose_platforms,
        receipt_images,
    )

    if platform and all_platforms:
        reject("bundle-invalid", "pass --platform or --all-platforms, not both")
    if not helm and (push or approve or app_version or credentials or ca_file):
        reject(
            "bundle-invalid",
            "--push, --approve, --app-version, --credentials and --ca-file need --helm",
        )
    if approve is not None and push is None:
        reject("bundle-invalid", "--approve needs --push")
    if out is None and push is None:
        reject("bundle-out-refused", "pass --out DIR (or, with --helm, --push)")
    if helm and receipt is not None:
        reject(
            "bundle-invalid",
            "a Helm chart names its images by digest in a registry; push the "
            "build images and declare them repository@sha256:... (--receipt is "
            "for the kustomize bundle)",
        )
    namespace, components, app, generators, declaration = _load(target, env)
    chosen_name = name or part_of(components) or (app.name if app is not None else None)
    if chosen_name is None:
        reject("bundle-invalid", "the render has no single part-of label; pass --name")
    exports: list[tuple[str, Any]] = []
    if receipt is not None:
        from piceli.artifacts.build_spec import BuildReceipt, BuildSpecError

        try:
            loaded = BuildReceipt.from_json(receipt.read_text())
        except (OSError, UnicodeDecodeError, BuildSpecError):
            reject(
                "bundle-receipt-invalid", "--receipt is not a readable build receipt"
            )
        try:
            grouped = receipt_images(loaded, output_dir or receipt.absolute().parent)
            wanted = None if all_platforms else list(platform or DEFAULT_PLATFORMS)
            exports = [
                (image, choose_platforms(image, platforms, wanted))
                for image, (_, platforms) in sorted(grouped.items())
            ]
        except ImageExportError as error:
            reject(error.code, str(error))
    if helm:
        _helm(
            components,
            declaration,
            name=chosen_name,
            version=version,
            namespace=namespace,
            generators=generators,
            env=env,
            storage_class=storage_class,
            app_version=app_version,
            out=out,
            push=push,
            approve=approve,
            credentials=credentials,
            ca_file=ca_file,
        )
        return
    assert out is not None
    try:
        built = build_bundle(
            components,
            name=chosen_name,
            version=version,
            namespace=namespace,
            generators=generators,
            receipt_names=[image for image, _ in exports],
            environment=env,
            storage_class=storage_class,
            forward=_forward(app, components),
        )
        used = {item.name for item in built.images if item.source == "receipt"}
        summary = write_bundle(
            out, built, [item for item in exports if item[0] in used]
        )
    except BundleError as error:
        if error.violations:
            reject(
                error.code,
                str(error),
                violations=[item.public() for item in error.violations],
            )
        reject(error.code, str(error))
    emit_json(
        {
            "state": "written",
            "name": built.name,
            "version": built.version,
            "environment": env,
            "namespace": built.namespace,
            "objects": len(built.objects),
            "secrets": [item.name for item in built.secrets],
            "safety_exceptions": [item.public() for item in built.exceptions],
            **summary,
        }
    )
    say(f"wrote bundle {built.name} {built.version} to {out}")
    for item in built.exceptions:
        say(f"  exception: {item.kind}/{item.name} waives {item.rule}: {item.reason}")
    say(f"  next: read {out / 'INSTALL.md'} and send the directory to the client")


def _helm(
    components: list[dict[str, Any]],
    declaration: Any,
    *,
    name: str,
    version: str,
    namespace: str,
    generators: Any,
    env: str | None,
    storage_class: str | None,
    app_version: str | None,
    out: Path | None,
    push: str | None,
    approve: str | None,
    credentials: Path | None,
    ca_file: Path | None,
) -> None:
    """``bundle --helm``: the chart directory, and the OCI push plan or push."""
    from piceli.bundle.build import BundleError, build_bundle
    from piceli.bundle.helm import apply_toggle, build_helm, write_helm
    from piceli.chart import archive_digest

    registry = _registry(push, version) if push is not None else None

    def bundled(rendered: list[dict[str, Any]]) -> Any:
        return build_bundle(
            rendered,
            name=name,
            version=version,
            namespace=namespace,
            generators=generators,
            environment=env,
            storage_class=storage_class,
        )

    try:
        base = bundled(components)
        variants = {
            toggle: bundled(apply_toggle(components, toggle, spec))
            for toggle, spec in (
                declaration.toggles.items() if declaration is not None else ()
            )
        }
        chart = build_helm(
            base, variants=variants, declaration=declaration, app_version=app_version
        )
        files = write_helm(out, chart) if out is not None else []
    except BundleError as error:
        if error.violations:
            reject(
                error.code,
                str(error),
                violations=[item.public() for item in error.violations],
            )
        reject(error.code, str(error))
    archive = chart.archive()
    body: dict[str, Any] = {
        **chart.public(),
        "environment": env,
        "archive_digest": archive_digest(archive),
        "safety_exceptions": [item.public() for item in base.exceptions],
    }
    if out is not None:
        body.update({"directory": str(out), "files": files})
    if registry is None:
        emit_json({"state": "written", **body})
        say(f"wrote chart {chart.name} {chart.version} to {out}")
        say(
            f"  next: helm lint {out}; helm install {chart.name} {out} -n {chart.name} --create-namespace ..."
        )
        return
    from piceli.cli_contract import EXIT_APPROVAL
    from piceli.k8s.cli.chart import _push

    target = registry(chart.name)
    artifact = chart.oci()
    body.update(
        {
            "digest": artifact.digest,
            "media_type": artifact.public()["media_type"],
            "config": artifact.config.descriptor(),
            "layer": artifact.layer.descriptor(),
            "target": target.identity,
        }
    )
    if approve is None:
        emit_json({"state": "approval-required", **body})
        say(
            f"chart {chart.name} {chart.version} for {target.identity}; nothing was pushed"
        )
        say(f"approve with: --approve {artifact.digest}")
        raise typer.Exit(EXIT_APPROVAL)
    if approve != artifact.digest:
        reject(
            "chart-artifact-changed",
            "the approved digest is not this chart's digest",
            digest=artifact.digest,
        )
    body.update(_push(target, artifact, credentials, ca_file))
    emit_json({"state": "published", **body})
    say(f"published {body['url']} --version {chart.version}")


def _registry(push: str, version: str) -> Any:
    """A function of the chart name giving the push target of ``--push``."""
    from piceli.artifacts.registry import RegistryTarget
    from piceli.errors import ERRORS

    try:
        parsed = RegistryTarget.parse(push)
    except ValueError as error:
        code = str(getattr(error, "code", ""))
        reject(code if code in ERRORS else "chart-target-invalid", str(error))
    if parsed.tag is not None:
        reject(
            "chart-target-invalid",
            "--push names a path, not a tag: the chart is pushed as "
            "path/<name>:<version>",
        )

    def target(chart: str) -> Any:
        try:
            return RegistryTarget(
                parsed.host,
                parsed.port,
                f"{parsed.repository}/{chart}",
                version.replace("+", "_"),
                parsed.tls,
            )
        except ValueError as error:
            reject("chart-target-invalid", str(error))

    return target


def support_bundle(
    namespace: Annotated[str, typer.Option("--namespace", help="Namespace to collect")],
    out: Annotated[
        Path, typer.Option("--out", help="The .tar.gz to write (must not exist)")
    ],
    kubeconfig: Annotated[
        Path,
        typer.Option("--kubeconfig", help="Kubeconfig file (never the default one)"),
    ],
    context: ContextOption,
    log_lines: Annotated[
        int,
        typer.Option("--log-lines", min=1, max=5000, help="Lines per container log"),
    ] = 200,
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: Annotated[
        str,
        typer.Option(
            "--transport",
            help="https, or loopback-http for a local test API server only",
        ),
    ] = "https",
) -> None:
    """Collect a read-only, redacted snapshot of a namespace (no Secrets, no env values)."""
    from piceli.bundle.support import (
        Collector,
        SupportError,
        api_transport,
        archive,
        write,
    )
    from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

    policy = exec_policy(allow_exec, exec_sha256)
    if transport not in {"https", "loopback-http"}:
        reject("target-refused", "--transport is https or loopback-http")
    client = guarded(
        lambda: api_client_from_kubeconfig(
            kubeconfig,
            context,
            transport=transport,  # type: ignore[arg-type]
            exec_policy=policy,
        )
    )
    collector = Collector(api_transport(client), namespace, log_lines)
    try:
        files = collector.collect()
        data = archive(
            files,
            namespace=namespace,
            context=context,
            requests=collector.requests,
            problems=collector.problems,
        )
        write(out, data)
    except SupportError as error:
        reject(error.code, str(error))
    methods = sorted({method for method, _ in collector.requests})
    emit_json(
        {
            "state": "written",
            "file": str(out),
            "namespace": namespace,
            "files": len(files) + 1,
            "bytes": len(data),
            "requests": len(collector.requests),
            "methods": methods,
            "problems": collector.problems,
        }
    )
    say(f"wrote {out} ({len(files) + 1} files; read-only: {', '.join(methods)})")


def register(root: typer.Typer) -> None:
    """Add ``bundle`` and ``support-bundle`` to the root application."""
    root.command("bundle")(bundle)
    root.command("support-bundle")(support_bundle)
