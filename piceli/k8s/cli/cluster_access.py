"""Explicit cluster access options shared by ``piceli observe`` and ``operator``.

Every command that reaches a cluster takes ``--kubeconfig`` **and** a required
``--context``: the file's ``current-context`` is never used, neither by
Piceli's own client nor by the ``kubectl`` processes it starts. Exec
credential plugins (GKE, EKS, AKS, OIDC) run only with ``--allow-exec``,
optionally pinned with ``--exec-sha256`` (see ``docs/managed_clusters.md``).
Refusals print ``{"state": "refused", "reason": ..., "code": ...}`` and exit 2.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, NoReturn, TypeVar

import typer

from piceli.cli_contract import reject_error
from piceli.k8s.ops.exec_credentials import ExecPolicy, ProviderFactoryError

T = TypeVar("T")

ContextOption = Annotated[
    str,
    typer.Option(
        help="Kubeconfig context to use (required; current-context is never used)"
    ),
]
AllowExecOption = Annotated[
    bool,
    typer.Option(
        "--allow-exec",
        help="Allow the context's exec credential plugin (GKE, EKS, AKS, OIDC)",
    ),
]
ExecSha256Option = Annotated[
    str | None,
    typer.Option(help="Expected sha256:<hex> of the resolved exec plugin file"),
]


def refuse(error: ValueError) -> NoReturn:
    """Reject with the error's registered code (else ``target-refused``) and exit 2."""
    reject_error(error, "target-refused")


def exec_policy(allow_exec: bool, exec_sha256: str | None) -> ExecPolicy:
    try:
        return ExecPolicy(allow_exec, exec_sha256)
    except ValueError as error:
        refuse(ProviderFactoryError(str(error)))


def guarded(build: Callable[[], T]) -> T:
    """Run ``build``; a kubeconfig/context refusal becomes a clean exit 2."""
    try:
        return build()
    except ProviderFactoryError as error:
        refuse(error)


def verify_kubectl_target(
    kubeconfig: Path, context: str, allow_exec: bool, exec_sha256: str | None
) -> None:
    """Apply the factory's refusals before ``kubectl`` gets the kubeconfig.

    kubectl runs an exec plugin itself (with the caller's environment), so the
    plugin is resolved and pinned here but not run.
    """
    from piceli.k8s.ops.provider_factory import verify_kubeconfig_context

    policy = exec_policy(allow_exec, exec_sha256)
    guarded(lambda: verify_kubeconfig_context(kubeconfig, context, exec_policy=policy))
