"""Forwards of a composition environment: ``piceli access ENV --cluster infra.py:CLUSTER``.

A composition (``infra.py``) names each environment's namespace and the
:class:`~piceli.infra.Cluster` whose ``credentials`` profile reaches it. The
components' ports live in their contracts (in the source repositories), so
the environment's forwards are its live Services: one forward per Service
port, each on a free loopback port (several environments can be reached at
once, as branch environments are). The cluster is read with the profile's
explicit kubeconfig and context, never the current context.

:func:`forward_shortcut` is the one place a forward is described, for this
command and for the forwards the web UI starts.

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from piceli.k8s.access import AccessTarget, AccessTargetError
from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.k8s.ui_config import UiShortcut

__all__ = ["composition_env_target", "forward_shortcut", "service_forwards"]

_ID = re.compile(r"[^a-z0-9-]+")


def forward_shortcut(
    id: str,
    *,
    kind: str,
    name: str,
    namespace: str,
    local_port: int,
    remote_port: int,
    label: str | None = None,
) -> UiShortcut:
    """One supervised loopback forward to ``kind/name:remote_port``."""
    return UiShortcut(
        id=id,
        label=label or f"{name}:{remote_port}",
        target=f"{kind.lower()}/{name}",
        namespace=namespace,
        local_port=local_port,
        remote_port=remote_port,
    )


def service_forwards(
    namespace: str, services: Iterable[Mapping[str, Any]], *, port: Callable[[], int]
) -> tuple[UiShortcut, ...]:
    """One forward per Service port, ordered by Service name and port."""
    found: list[tuple[str, int]] = []
    for raw in services:
        name = (raw.get("metadata") or {}).get("name")
        if not isinstance(name, str) or not name:
            continue
        for item in (raw.get("spec") or {}).get("ports") or []:
            value = item.get("port") if isinstance(item, Mapping) else None
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 < value < 65536
            ):
                found.append((name, value))
    return tuple(
        forward_shortcut(
            _ID.sub("-", f"{name}-{remote}".lower()).strip("-")[:63],
            kind="Service",
            name=name,
            namespace=namespace,
            local_port=port(),
            remote_port=remote,
        )
        for name, remote in sorted(set(found))
    )


def _live_services(target: KubeconfigTarget) -> list[dict[str, Any]]:
    from piceli.services.query import KubernetesReader
    from piceli.services.registration import Registration

    reader = KubernetesReader(Registration("access-env", "access", target))
    try:
        return reader.list("v1", "Service", target.namespace)
    finally:
        reader.close()


def composition_env_target(
    cluster: str,
    env: str,
    root: Path,
    *,
    services: Callable[[KubeconfigTarget], list[dict[str, Any]]] | None = None,
    port: Callable[[], int] | None = None,
) -> AccessTarget:
    """The :class:`AccessTarget` of environment ``env`` of the composition holding ``cluster``.

    :param cluster: ``infra.py:ATTR``, the composition module and its Cluster.
    :raises AccessTargetError: ``access-target-invalid`` (not a Cluster in a
        composition), ``env-not-found`` (no such named environment), or a
        profile's code when its credentials cannot be resolved.
    """
    from piceli.app.render import load_target
    from piceli.envs.model import Environment
    from piceli.envs.ops import free_port
    from piceli.infra import Cluster, CompositionError
    from piceli.infra.composition import load_composition
    from piceli.profiles import ProfileError, resolve

    file, sep, _attr = cluster.rpartition(":")
    if not sep or not file.endswith(".py"):
        raise AccessTargetError(
            "access-target-invalid", "--cluster takes infra.py:CLUSTER"
        )
    try:
        declared = load_target(cluster, root)
        composition = load_composition(file, root)
    except (CompositionError, ImportError, AttributeError, OSError, ValueError):
        raise AccessTargetError(
            "access-target-invalid", f"{cluster} is not a composition's Cluster"
        ) from None
    if not isinstance(declared, Cluster):
        raise AccessTargetError(
            "access-target-invalid", f"{cluster} is not a piceli.infra.Cluster"
        )
    environment = next(
        (
            item
            for item in composition.environments
            if isinstance(item, Environment) and item.name == env
        ),
        None,
    )
    if environment is None:
        raise AccessTargetError(
            "env-not-found",
            f"the composition declares no environment named {env!r}",
        )
    try:
        credentials = resolve(declared.credentials)
    except ProfileError as error:
        raise AccessTargetError(error.code, str(error)) from None
    target = KubeconfigTarget(
        credentials.kubeconfig, credentials.context, environment.namespace
    )
    try:
        live = (services or _live_services)(target)
    except AccessTargetError:
        raise
    except Exception as error:  # provider detail may name paths; keep the code
        code = getattr(error, "code", None)
        raise AccessTargetError(
            code if isinstance(code, str) else "access-target-invalid",
            f"the services of {environment.namespace} could not be read",
        ) from None
    shortcuts = service_forwards(environment.namespace, live, port=port or free_port)
    return AccessTarget(
        name=f"{composition.name} environment {env}",
        namespace=environment.namespace,
        kubeconfig=credentials.kubeconfig,
        context=credentials.context,
        source="composition",
        shortcuts=shortcuts,
        workloads=tuple(
            dict.fromkeys(
                ("Service", item.target.split("/", 1)[1]) for item in shortcuts
            )
        ),
    )
