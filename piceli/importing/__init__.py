"""Import existing Kubernetes objects as a typed :class:`~piceli.app.App` module.

Maturity: **preview**. See ``docs/migrate_from_kubectl.md``.

:func:`import_live` reads one namespace through an explicit kubeconfig and
context (read-only); :func:`import_yaml` reads a directory of manifests. Both
generate the same kind of module: a ``build(ctx)`` composition function that
declares ConfigMaps, Secrets, Deployments, Services and NetworkPolicies with
the typed API, mounts PersistentVolumeClaims as ``ExistingClaim`` (never
managed), keeps fields the typed API does not cover with ``app.override(...)``
(one comment per field) and keeps objects of other kinds as raw manifests.

Secret values are never copied: each Secret key becomes a ``ctx.secret(...)``
input, and the module's docstring lists the ``[secrets.*]`` ``import``
generators that read the live values on the first release.

The generated module renders exactly the imported fields (the import checks
this and refuses with ``import-roundtrip-mismatch`` otherwise), minus fields
the API server populates or defaults.

Importing this package has no side effects.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from piceli.importing.generate import (
    ImportedObject,
    ImportFailure,
    ImportResult,
    RunningImage,
    SecretInput,
    Skipped,
)
from piceli.importing.generate import generate as _generate
from piceli.importing.sources import Selector, read_live, read_yaml, select

__all__ = [
    "ImportFailure",
    "ImportResult",
    "ImportedObject",
    "RunningImage",
    "SecretInput",
    "Skipped",
    "import_live",
    "import_yaml",
]

_APP_NAME = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")


def _app_name(name: str | None, namespace: str) -> str:
    value = name or namespace
    if not _APP_NAME.fullmatch(value):
        raise ImportFailure(
            "import-name-invalid", f"the app name must be a DNS label, got {value!r}"
        )
    return value


def import_live(
    kubeconfig: Path,
    context: str,
    namespace: str,
    *,
    selectors: Sequence[str] = (),
    app_name: str | None = None,
    transport: str = "https",
) -> ImportResult:
    """Generate a typed module from the objects of a live namespace.

    Read-only: never writes to the cluster. Uses exactly ``kubeconfig`` and
    ``context`` (never ``KUBECONFIG``, ``~/.kube/config`` or the current
    context). ``transport="loopback-http"`` is only for a local test server
    (see :mod:`piceli.testing`).

    :param selectors: ``Kind/name`` or ``label=value``; keep objects matching
        any of them (default: every importable object).
    :param app_name: The ``App`` name; defaults to the namespace.
    :raises ImportFailure: with a registered ``code``.
    """
    name = _app_name(app_name, namespace)
    for text in selectors:  # refuse a bad selector before contacting the cluster
        Selector.parse(text)
    source = select(
        read_live(Path(kubeconfig), context, namespace, transport=transport),
        [*selectors],
    )
    return _generate(
        source.manifests,
        app_name=name,
        namespace=namespace,
        source="live",
        origin=f"namespace {namespace} (context {context})",
        running_images=source.running_images,
        skipped=source.skipped,
    )


def import_yaml(
    directory: Path,
    *,
    namespace: str | None = None,
    selectors: Sequence[str] = (),
    app_name: str | None = None,
) -> ImportResult:
    """Generate a typed module from a directory of manifests (no cluster).

    :param namespace: The namespace to render into; defaults to the one the
        files name, else ``default``.
    :raises ImportFailure: with a registered ``code``.
    """
    source = select(read_yaml(Path(directory), namespace), [*selectors])
    return _generate(
        source.manifests,
        app_name=_app_name(app_name, source.namespace),
        namespace=source.namespace,
        source="yaml",
        origin=f"the manifests in {Path(directory).name or '.'}/",
        skipped=source.skipped,
    )
