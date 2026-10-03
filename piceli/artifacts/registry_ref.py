"""Which in-cluster registry a command means, and how to reach its cluster.

``piceli registry status|install|uninstall|forward`` and ``piceli artifacts
retention --cluster`` take one reference to the registry of
``Registry.in_cluster(...)``. It may name:

- the ``Registry.in_cluster(...)`` value (``MODULE:ATTR``);
- a ``Pipeline`` delivering to it (its target names the cluster);
- a :class:`piceli.infra.Cluster` declared with ``registry=Registry.in_cluster(...)``
  (its credential profile reaches the cluster; the profile's API server must
  be the declared ``api``);
- a composition module (``infra.py``, no ``:ATTR``) whose one ``Cluster`` has
  such a registry.

The cluster is reached with explicit ``--kubeconfig`` and ``--context`` when
given, else the pipeline's target, else the Cluster's profile: never the
ambient context. Refusals carry a registered error code.

Importing this module is side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from piceli.infra import Cluster
    from piceli.pipeline.model import ClusterRegistry


class RegistryRefError(ValueError):
    """A registry reference refused, with a registered ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RegistryRef:
    """The registry a reference names, and where its cluster came from."""

    registry: ClusterRegistry
    #: A pipeline's target (``kubeconfig``, ``context``, ``transport``, ``exec_policy()``).
    target: Any = None
    #: The declared cluster whose profile reaches it.
    cluster: Cluster | None = None


@dataclass(frozen=True)
class ClusterAccess:
    """An explicit kubeconfig file and context (never the ambient one)."""

    kubeconfig: Path
    context: str
    transport: str = "https"
    exec_policy: Any = None


def _not_in_cluster(ref: str) -> RegistryRefError:
    return RegistryRefError(
        "cluster-registry-invalid",
        f"{ref} is not Registry.in_cluster(...), a Pipeline delivering to it, "
        "nor a Cluster (or composition) whose registry= is Registry.in_cluster(...)",
    )


def _of_cluster(cluster: Cluster, ref: str) -> RegistryRef:
    from piceli.pipeline.model import ClusterRegistry

    if not isinstance(cluster.registry, ClusterRegistry):
        raise RegistryRefError(
            "cluster-registry-invalid",
            f"the Cluster of {ref} declares no registry=Registry.in_cluster(...)",
        )
    return RegistryRef(cluster.registry, cluster=cluster)


def _composition_cluster(path: Path, ref: str) -> RegistryRef:
    """The one ``Cluster`` a composition module declares (its environments' too)."""
    from piceli.infra import Cluster, CompositionError
    from piceli.infra.composition import _import

    try:
        module = _import(path)
    except CompositionError as error:
        raise RegistryRefError(error.code, str(error)) from None
    values = vars(module)
    clusters = {id(v): v for v in values.values() if isinstance(v, Cluster)}
    for item in values.get("environments") or ():
        found = getattr(item, "cluster", None)
        if isinstance(found, Cluster):
            clusters.setdefault(id(found), found)
    if len(clusters) != 1:
        raise RegistryRefError(
            "cluster-registry-invalid",
            f"{ref} declares {len(clusters)} Cluster(s); name one as {ref}:ATTR",
        )
    return _of_cluster(next(iter(clusters.values())), ref)


def load_registry_ref(
    ref: str, env: str | None = None, base: Path | None = None
) -> RegistryRef:
    """The in-cluster registry ``ref`` names (see the module docstring).

    :raises RegistryRefError: ``pipeline-not-found``, ``pipeline-load-failed``,
        ``environment-required``, ``composition-invalid`` or
        ``cluster-registry-invalid``.
    """
    from piceli.app.render import RenderError, load_target
    from piceli.infra import Cluster
    from piceli.pipeline.errors import PipelineError
    from piceli.pipeline.model import ClusterRegistry, Pipeline

    root = base or Path.cwd()
    if ":" not in ref and ref.endswith(".py"):
        path = (root / ref).resolve()
        if not path.is_file():
            raise RegistryRefError("pipeline-not-found", f"file not found: {ref}")
        return _composition_cluster(path, ref)
    try:
        value = load_target(ref, root)
    except RenderError as error:
        raise RegistryRefError("pipeline-not-found", str(error)) from None
    except Exception as error:  # the module raised while importing
        from piceli.cli_contract import describe_user_error

        code = getattr(error, "code", None)
        if isinstance(error, ValueError) and code in {"cluster-invalid"}:
            raise RegistryRefError(code, str(error)) from None
        raise RegistryRefError(
            "pipeline-load-failed",
            f"importing {ref} failed: {describe_user_error(error)}",
        ) from None
    if isinstance(value, Cluster):
        return _of_cluster(value, ref)
    target = None
    if isinstance(value, Pipeline):
        try:
            if env is not None:
                value = value.for_environment(env)
            elif value.needs_environment:
                raise RegistryRefError(
                    "environment-required",
                    f"{ref} deploys one target per environment; pass --env NAME",
                )
        except PipelineError as error:
            code = (
                "cluster-registry-invalid"
                if error.code == "pipeline-invalid"
                else error.code
            )
            raise RegistryRefError(code, str(error)) from None
        target = value.target
        value = value.deliver
    if not isinstance(value, ClusterRegistry):
        raise _not_in_cluster(ref)
    return RegistryRef(value, target=target)


def cluster_access(
    found: RegistryRef | None,
    *,
    kubeconfig: Path | None,
    context: str | None,
    transport: str = "https",
    exec_policy: Any = None,
) -> ClusterAccess:
    """How to reach the registry's cluster.

    Explicit ``kubeconfig`` and ``context`` win (both are required together);
    then a pipeline's target; then a declared Cluster's credential profile,
    whose API server must be ``Cluster(api=)``.

    :raises RegistryRefError: ``cluster-registry-target-required``,
        ``cluster-api-mismatch``, ``cluster-api-failed`` or a profile code.
    """
    if (
        kubeconfig is not None
        or context is not None
        or found is None
        or (found.target is None and found.cluster is None)
    ):
        if kubeconfig is None or not context:
            raise RegistryRefError(
                "cluster-registry-target-required",
                "name the cluster with --kubeconfig FILE --context NAME "
                "(or pass a pipeline whose target names it, or a Cluster)",
            )
        return ClusterAccess(kubeconfig, context, transport, exec_policy)
    if found.target is not None:
        target = found.target
        return ClusterAccess(
            Path(target.kubeconfig),
            target.context,
            target.transport,
            target.exec_policy(),
        )
    assert found.cluster is not None
    return profile_access(found.cluster, transport, exec_policy)


def profile_access(cluster: Cluster, transport: str, exec_policy: Any) -> ClusterAccess:
    """The kubeconfig and context of ``cluster``'s credential profile, checked
    against its declared ``api`` (in a pod: the service account)."""
    from piceli.infra.cluster import api_matches, kubeconfig_server
    from piceli.k8s.ops.provider_factory import _read_kubeconfig
    from piceli.profiles import ProfileError, override, resolve

    name = override() or cluster.credentials
    try:
        found = resolve(name)
    except ProfileError as error:
        raise RegistryRefError(error.code or "profile-invalid", str(error)) from None
    if not found.in_cluster:
        try:
            document = _read_kubeconfig(found.kubeconfig)
        except Exception:
            raise RegistryRefError(
                "cluster-api-failed",
                f"the kubeconfig of profile {name!r} is unreadable",
            ) from None
        server = kubeconfig_server(document, found.context)
        if server is None or not api_matches(cluster.api, server):
            raise RegistryRefError(
                "cluster-api-mismatch",
                f"profile {name!r} does not point at Cluster(api={cluster.api!r})",
            )
    return ClusterAccess(found.kubeconfig, found.context, transport, exec_policy)
