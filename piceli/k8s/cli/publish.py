"""``piceli publish``: push rendered manifests as an OCI artifact for Flux or Argo CD.

Contract:

- Renders the target exactly like ``piceli render`` (same TARGET, ``--spec``,
  ``--namespace``, ``--env``), turns the objects into files
  (:func:`piceli.gitops.handoff`: a Secret needs ``--secrets external``, a
  redacted value or a placeholder image is refused) and packages them in the
  ``flux push artifact`` layout (:func:`piceli.gitops.flux_artifact`). The
  artifact is deterministic: the same render has the same digest.
- Without ``--approve`` nothing is sent: it prints ``{"state":
  "approval-required", "digest": …}`` and exits ``3``. With ``--approve
  DIGEST`` equal to that digest it pushes the blobs, the manifest by digest and
  then the tag of ``--to``, reads them back and prints ``{"state":
  "published", …}``. Pushing is content-addressed, so a retry is safe.
- Credentials come only from ``--credentials FILE`` (private JSON file) and are
  never printed. Plain HTTP only for a loopback registry.
- Never contacts a cluster. Exit codes: ``0`` published, ``1`` the push failed,
  ``2`` rejected, ``3`` approval required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from piceli.cli_contract import EXIT_APPROVAL, emit_json, fail, reject, say
from piceli.errors import ERRORS
from piceli.k8s.cli.render import SecretMode, _render


def publish(
    target: Annotated[
        str | None,
        typer.Argument(
            help="module:attr or path/to/file.py:attr, as for `piceli render`",
            show_default=False,
        ),
    ] = None,
    to: Annotated[
        str,
        typer.Option(
            "--to",
            help="oci://host[:port]/repository[:tag] to push to (the tag is optional)",
            show_default=False,
        ),
    ] = "",
    spec: Annotated[
        Path | None,
        typer.Option("--spec", help="release.toml, as for `piceli render`"),
    ] = None,
    namespace: Annotated[
        str | None,
        typer.Option("--namespace", help="Namespace to render into"),
    ] = None,
    env: Annotated[
        str | None,
        typer.Option("--env", help="Render this environment of the App"),
    ] = None,
    secrets: Annotated[
        SecretMode,
        typer.Option(
            "--secrets",
            help="'refuse' a Secret object, or leave Secrets out because they "
            "are provided outside the artifact ('external')",
        ),
    ] = SecretMode.refuse,
    approve: Annotated[
        str | None,
        typer.Option(
            "--approve",
            help="The artifact digest printed without --approve; pushes it",
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
    source: Annotated[
        str | None,
        typer.Option(
            "--source",
            help="org.opencontainers.image.source annotation (e.g. the Git URL)",
        ),
    ] = None,
    revision: Annotated[
        str | None,
        typer.Option(
            "--revision",
            help="org.opencontainers.image.revision annotation (e.g. main@sha1:<commit>)",
        ),
    ] = None,
) -> None:
    """Push the rendered manifests as a Flux OCI artifact (needs --approve DIGEST)."""
    from piceli.artifacts.registry import RegistryTarget
    from piceli.gitops import GitOpsError, artifact_annotations, flux_artifact, handoff

    def rejected(reason: str, message: str) -> NoReturn:
        reject(reason, message)

    if not to:
        reject("gitops-target-invalid", "pass --to oci://host[:port]/repository[:tag]")
    try:
        registry = RegistryTarget.parse(to)
    except ValueError as error:
        code = str(getattr(error, "code", ""))
        reject(code if code in ERRORS else "gitops-target-invalid", str(error))
    if not target and spec is None:
        reject("render-target-invalid", "pass a TARGET, --spec, or both")
    rendered_namespace, components, _ = _render(target, spec, namespace, env, rejected)
    try:
        files = handoff(components, secrets.value)
        artifact = flux_artifact(
            files.files,
            artifact_annotations(
                namespace=rendered_namespace,
                environment=env,
                source=source,
                revision=revision,
            ),
        )
    except GitOpsError as error:
        reject(error.code, str(error))
    except ValueError as error:
        reject("gitops-target-invalid", str(error))
    body: dict[str, Any] = {
        **artifact.public(),
        "target": registry.identity,
        "namespace": rendered_namespace,
        "omitted_secrets": list(files.omitted_secrets),
    }
    if env is not None:
        body["environment"] = env
    if approve is None:
        emit_json({"state": "approval-required", **body})
        say(f"{len(files.files)} file(s) for {registry.identity}; nothing was pushed")
        for name in files.omitted_secrets:
            say(f"  left out (provide it outside the artifact): Secret {name}")
        say(f"approve with: --approve {artifact.digest}")
        raise typer.Exit(EXIT_APPROVAL)
    if approve != artifact.digest:
        reject(
            "gitops-artifact-changed",
            "the approved digest is not this render's artifact digest",
            digest=artifact.digest,
        )
    body.update(_push(registry, artifact, credentials, ca_file))
    emit_json({"state": "published", **body})
    say(f"published {body['reference']}")


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
        reject("gitops-target-invalid", str(error))
    client = StreamedOciRegistryClient(endpoint)
    try:
        counts = push(client, registry.repository, artifact, registry.tag)
    except RegistryError as error:
        code = error.reason if error.reason in ERRORS else "gitops-push-failed"
        fail(code, f"push to {registry.registry} failed: {error.reason}")
    reference = f"{registry.registry}/{registry.repository}@{artifact.digest}"
    return {
        **counts,
        "reference": reference,
        "url": f"oci://{registry.registry}/{registry.repository}",
        "tag": registry.tag,
    }
