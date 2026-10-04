"""Several clusters for one environment: :class:`Placement` and :class:`Rollout` (0.15).

An :class:`~piceli.envs.Environment` (or a branch rule) of a composition
deploys to one cluster (``cluster=``) or to several (``clusters=``)::

    Environment(
        "edge", namespace="shop-edge", pipeline=pipeline, follow={...},
        clusters=[Placement(edge_canary, on_nodes=["edge-1"]),
                  Placement(edge_b, namespace="shop")],
        rollout=Rollout(order=["edge-canary", "edge-*"]),
    )

Each :class:`Placement` names a :class:`piceli.infra.Cluster` and what
differs there: the namespace, the nodes, ``values`` (setting overrides of
contract components, ``{component: {key: value}}``) or the ``pipeline`` (a
pipeline environment's per-cluster pipeline, such as
``pipeline.for_environment("edge-b")``). :func:`placed` derives the
environment one cluster runs; the GitOps controller keeps one record per
environment and cluster.

:class:`Rollout` orders the clusters in waves of name globs: a wave deploys a
revision only when every cluster of the earlier waves runs it with its checks
passed; a failure there holds the later waves until the next revision.

Importing this module is side-effect free.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from piceli.envs.model import BRANCH, EnvError, _is_label, _nodes, _settings

__all__ = ["Placement", "Rollout", "placed", "placements", "waves"]

_GLOB = re.compile(r"[a-z0-9*?\[\]-]{1,63}")


def _invalid(message: str) -> EnvError:
    return EnvError("env-config-invalid", message)


@dataclass(frozen=True)
class Placement:
    """Where one cluster runs an environment, and what differs there.

    :param cluster: The :class:`piceli.infra.Cluster`.
    :param namespace: The namespace in that cluster (default: the
        environment's; a branch rule's is ``"<prefix>{branch}"``).
    :param on_nodes: Node names or a ``{label: value}`` selector in that
        cluster (default: the environment's).
    :param values: ``{component: {key: value}}`` merged over the
        environment's ``settings`` (contract components only).
    :param pipeline: A pipeline environment's pipeline in that cluster
        (default: the environment's).
    :param replicas: ``{workload: count}`` merged over the environment's
        ``replicas``.
    """

    cluster: Any
    namespace: str | None = None
    on_nodes: Any = None
    values: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    pipeline: Any = field(default=None, compare=False)
    replicas: Mapping[str, int] | None = None

    def __post_init__(self) -> None:
        from piceli.envs.model import _replicas
        from piceli.infra import Cluster

        if not isinstance(self.cluster, Cluster):
            raise _invalid("Placement(cluster) is a piceli.infra.Cluster")
        what = f"Placement({self.cluster.name})"
        if self.namespace is not None and not (
            _is_label(self.namespace)
            or (
                isinstance(self.namespace, str)
                and self.namespace.endswith(BRANCH)
                and _is_label(self.namespace[: -len(BRANCH)] + "x")
            )
        ):
            raise _invalid(
                f"{what}: namespace is a DNS label (or '<prefix>{{branch}}')"
            )
        object.__setattr__(self, "on_nodes", _nodes(self.on_nodes, f"{what} on_nodes"))
        object.__setattr__(
            self, "values", _settings(self.values or {}, f"{what} values")
        )
        object.__setattr__(
            self, "replicas", _replicas(self.replicas, f"{what} replicas")
        )
        if self.pipeline is not None:
            from piceli.pipeline.model import Pipeline

            if not isinstance(self.pipeline, Pipeline):
                raise _invalid(f"{what}: pipeline is a piceli Pipeline")

    @property
    def name(self) -> str:
        """The cluster's name."""
        return str(self.cluster.name)

    def describe(self) -> dict[str, Any]:
        """Plain data (the composition config): only what is declared."""
        body: dict[str, Any] = {"cluster": self.name}
        if self.namespace is not None:
            body["namespace"] = self.namespace
        if self.on_nodes is not None:
            body["on_nodes"] = (
                dict(self.on_nodes)
                if isinstance(self.on_nodes, Mapping)
                else list(self.on_nodes)
            )
        if self.values:
            body["values"] = {k: dict(v) for k, v in self.values.items()}
        if self.pipeline is not None:
            body["pipeline"] = self.pipeline.name
        if self.replicas:
            body["replicas"] = dict(self.replicas)
        return body


@dataclass(frozen=True)
class Rollout:
    """The order clusters deploy a revision in: waves of cluster-name globs.

    ``Rollout(order=["edge-canary", "edge-*"])``: ``edge-canary`` first; the
    clusters matching ``edge-*`` (and not an earlier wave) only after it runs
    the revision with its checks passed. A cluster no pattern matches is in a
    last wave of its own.
    """

    order: Sequence[str] = ()

    def __post_init__(self) -> None:
        if isinstance(self.order, str) or not isinstance(self.order, Sequence):
            raise _invalid("Rollout(order=) is a list of cluster names or globs")
        order = tuple(self.order)
        if not order or not all(
            isinstance(item, str) and _GLOB.fullmatch(item) for item in order
        ):
            raise _invalid(
                "Rollout(order=) names at least one cluster (a name or a glob "
                "like 'edge-*')"
            )
        if len(set(order)) != len(order):
            raise _invalid("Rollout(order=) lists a pattern twice")
        object.__setattr__(self, "order", order)

    def describe(self) -> dict[str, Any]:
        return {"order": list(self.order)}


def placements(value: Any, what: str) -> tuple[Placement, ...]:
    """``clusters=`` normalised: a :class:`Placement` per cluster (a bare
    ``Cluster`` is ``Placement(cluster)``), each cluster once."""
    from piceli.infra import Cluster

    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise _invalid(f"{what}: clusters is a list of Placement(...) or Cluster")
    found: list[Placement] = []
    for item in value:
        if isinstance(item, Cluster):
            item = Placement(item)
        if not isinstance(item, Placement):
            raise _invalid(f"{what}: clusters holds {item!r}, not a Placement")
        found.append(item)
    names = [item.name for item in found]
    if len(set(names)) != len(names):
        raise _invalid(f"{what}: a cluster is placed twice")
    return tuple(found)


def check_rollout(rollout: Any, clusters: Sequence[Placement], what: str) -> None:
    """A rollout needs placements, and each of its patterns matches one."""
    if rollout is None:
        return
    if not isinstance(rollout, Rollout):
        raise _invalid(f"{what}: rollout is a Rollout(order=[...])")
    if not clusters:
        raise _invalid(f"{what}: rollout= orders the clusters of clusters=[...]")
    names = [item.name for item in clusters]
    for pattern in rollout.order:
        if not any(fnmatch.fnmatchcase(name, pattern) for name in names):
            raise _invalid(f"{what}: rollout pattern {pattern!r} matches no cluster")


def waves(names: Sequence[str], rollout: Rollout | None) -> list[list[str]]:
    """The clusters by wave (each cluster once; unmatched ones last)."""
    if rollout is None:
        return [list(names)] if names else []
    left = list(names)
    found: list[list[str]] = []
    for pattern in rollout.order:
        wave = [name for name in left if fnmatch.fnmatchcase(name, pattern)]
        if wave:
            found.append(wave)
            left = [name for name in left if name not in wave]
    if left:
        found.append(left)
    return found


def placed(env: Any, placement: Placement) -> Any:
    """The environment ``placement``'s cluster runs (no ``clusters`` left)."""
    from piceli.envs.model import BranchEnvironments

    settings = {k: dict(v) for k, v in env.settings.items()}
    for component, table in placement.values.items():
        settings[component] = {**settings.get(component, {}), **table}
    changes: dict[str, Any] = {
        "cluster": placement.cluster,
        "clusters": (),
        "rollout": None,
        "on_nodes": env.on_nodes if placement.on_nodes is None else placement.on_nodes,
        "settings": settings,
        "pipeline": env.pipeline if placement.pipeline is None else placement.pipeline,
    }
    if placement.replicas:
        changes["replicas"] = {**(env.replicas or {}), **placement.replicas}
    if placement.namespace is not None:
        if isinstance(env, BranchEnvironments) != placement.namespace.endswith(BRANCH):
            raise _invalid(
                f"Placement({placement.name}): a branch rule's namespace is "
                "'<prefix>{branch}', a named environment's a DNS label"
            )
        changes["namespace"] = placement.namespace
    if isinstance(env, BranchEnvironments):
        return dataclasses.replace(env, **changes)
    derived = dataclasses.replace(env, **changes)
    object.__setattr__(derived, "sources", env.sources)
    return derived
