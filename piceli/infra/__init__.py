"""Compositions: clusters, sources and components (skeleton, 0.14 design).

A composition module declares a :class:`Cluster`, the :class:`Source` repos
it builds from, the :class:`Component` contracts those repos carry
(``piceli.toml``), and the environments that deploy them. ``piceli cluster
init`` sets the cluster up; ``piceli gitops enable`` deploys the
environments. The signatures here are the contract between the packages
that implement them; behaviour lives in the submodules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from piceli.pipeline.model import Registry

__all__ = ["Cluster", "Component", "Controller", "Node", "Source", "Ui"]


@dataclass(frozen=True)
class Node:
    """A cluster node; each role becomes the label ``piceli.io/role-<role>=true``."""

    name: str
    arch: Literal["amd64", "arm64"]
    roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        from piceli.infra.cluster import as_tuple, check_node

        object.__setattr__(self, "roles", as_tuple(self.roles, "Node(roles=)"))
        check_node(self)

    @property
    def labels(self) -> dict[str, str]:
        """``piceli.io/role-<role>=true`` per role (and ``piceli.io/builder=true``)."""
        from piceli.infra.cluster import node_labels

        return node_labels(self)


@dataclass(frozen=True)
class Controller:
    """Where the GitOps controller runs and how often it polls its sources."""

    on: str
    poll: str = "1m"
    sync: Literal["on change"] = "on change"
    image: str | None = None

    def __post_init__(self) -> None:
        from piceli.infra.cluster import check_controller

        check_controller(self)


@dataclass(frozen=True)
class Ui:
    """The UI installed in the cluster; ``forward`` = reachable only through ``piceli access ui``."""

    access: Literal["forward"] = "forward"
    on: str | None = None
    image: str | None = None

    def __post_init__(self) -> None:
        from piceli.infra.cluster import check_ui

        check_ui(self)


@dataclass(frozen=True)
class Cluster:
    """A cluster: its API, the credential profile that reaches it, its nodes and services."""

    name: str
    api: str
    credentials: str
    nodes: tuple[Node, ...] = ()
    storage_class: str | None = None
    registry: Registry | None = None
    controller: Controller | None = None
    ui: Ui | None = None

    def __post_init__(self) -> None:
        from piceli.infra.cluster import as_tuple, check_cluster

        object.__setattr__(self, "nodes", as_tuple(self.nodes, "Cluster(nodes=)"))
        check_cluster(self)

    def node(self, name: str) -> Node | None:
        """The declared node ``name``, if any."""
        return next((node for node in self.nodes if node.name == name), None)

    def describe(self) -> dict[str, object]:
        """The declaration as data (``piceli.cluster.v1``); no credentials."""
        from piceli.infra.cluster import describe

        return describe(self)


@dataclass(frozen=True)
class Source:
    """A Git repository a composition builds components from (one Git Secret for all)."""

    url: str
    name: str | None = None


@dataclass(frozen=True)
class Component:
    """A component whose contract is the ``piceli.toml`` in its source at the environment's ref."""

    name: str
    source: Source | None = None
    image_ref: str | None = None
    pin: str | None = None
    options: dict[str, object] = field(default_factory=dict)

    @classmethod
    def image(cls, ref: str, *, pin: str) -> Component:
        """A third-party image (no source): mirrored into the in-cluster registry."""
        return cls(name=ref.rsplit("/", 1)[-1].split(":", 1)[0], image_ref=ref, pin=pin)
