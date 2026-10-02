"""Compositions: clusters, sources and components (skeleton, 0.14 design).

A composition module declares a :class:`Cluster`, the :class:`Source` repos
it builds from, the :class:`Component` contracts those repos carry
(``piceli.toml``), and the environments that deploy them. ``piceli cluster
init`` sets the cluster up; ``piceli gitops enable`` deploys the
environments. The signatures here are the contract between the packages
that implement them; behaviour lives in the submodules.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from piceli.pipeline.errors import PipelineError
from piceli.pipeline.model import Registry

_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_PIN = re.compile(r"sha256:[0-9a-f]{64}")
_IMAGE_REF = re.compile(r"[a-z0-9][a-z0-9./_-]*(?::[A-Za-z0-9._-]{1,128})?")


class CompositionError(PipelineError):
    """A composition, source or component contract was refused (``code`` is registered)."""


def _slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:63].strip("-")
    return value or "source"


__all__ = [
    "Cluster",
    "Component",
    "CompositionError",
    "Controller",
    "Node",
    "Source",
    "Ui",
]


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
    """Where the GitOps controller runs and how often it polls its sources.

    ``delete_volumes=True`` lets branch teardown delete the PersistentVolumes
    bound to the environment's claims (a ``Retain`` storage class keeps them
    otherwise); it grants the controller delete on PersistentVolumes
    cluster-wide, so it is off by default and teardown reports them instead.
    """

    on: str
    poll: str = "1m"
    sync: Literal["on change"] = "on change"
    image: str | None = None
    delete_volumes: bool = False

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
    """A Git repository a composition builds components from (one Git Secret for all).

    :param url: The remote (``https://``, ``ssh://``, ``git@host:path`` or a
        local path in tests); never credentials in it.
    :param name: Its name in plans and status (default: the repository name
        from the URL, ``shop`` for ``…/shop.git``).
    """

    url: str
    name: str | None = None

    def __post_init__(self) -> None:
        from piceli.gitops import GitOpsError
        from piceli.gitops.config import check_repo_url

        try:
            object.__setattr__(self, "url", check_repo_url(self.url))
        except GitOpsError as error:
            raise CompositionError("composition-invalid", str(error)) from None
        if self.name is not None and not _LABEL.fullmatch(self.name):
            raise CompositionError(
                "composition-invalid", f"source name {self.name!r} is not a DNS label"
            )

    @property
    def key(self) -> str:
        """The source's name: ``name``, else the repository name of the URL."""
        if self.name is not None:
            return self.name
        tail = self.url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        return _slug(tail.removesuffix(".git"))


@dataclass(frozen=True)
class Component:
    """A component whose contract is the ``piceli.toml`` in its source at the environment's ref.

    :param name: The component (``[component.<name>]`` in ``piceli.toml``);
        its workload and Service are named after it.
    :param source: The :class:`Source` that holds its contract and code.
    :param settings: Overrides of the contract's ``settings`` (env vars).
    :param options: ``{"contract": {...}}`` for :meth:`image`: the contract
        of a third-party image (ports, health, volumes, settings; no build).
    """

    name: str
    source: Source | None = None
    image_ref: str | None = None
    pin: str | None = None
    options: dict[str, object] = field(default_factory=dict)
    settings: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _LABEL.fullmatch(self.name):
            raise CompositionError(
                "composition-invalid",
                f"component name {self.name!r} is not a DNS label",
            )
        if (self.source is None) == (self.image_ref is None):
            raise CompositionError(
                "composition-invalid",
                f"component {self.name!r} has a source or is Component.image(...)",
            )
        if self.source is not None and not isinstance(self.source, Source):
            raise CompositionError(
                "composition-invalid", f"component {self.name!r}: source is a Source"
            )
        if self.image_ref is not None and (
            not _IMAGE_REF.fullmatch(self.image_ref)
            or not isinstance(self.pin, str)
            or not _PIN.fullmatch(self.pin)
        ):
            raise CompositionError(
                "composition-invalid",
                f"component {self.name!r}: Component.image(ref, pin='sha256:<64 hex>')",
            )
        if not isinstance(self.settings, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in self.settings.items()
        ):
            raise CompositionError(
                "composition-invalid", f"component {self.name!r}: settings are strings"
            )

    @classmethod
    def image(
        cls,
        ref: str,
        *,
        pin: str,
        name: str | None = None,
        contract: Mapping[str, Any] | None = None,
        settings: Mapping[str, str] | None = None,
    ) -> Component:
        """A third-party image (no source): mirrored into the in-cluster registry.

        ``ref`` is the image (``redis:7.2``), ``pin`` its manifest digest; the
        controller copies ``ref@pin`` into the in-cluster registry when it
        syncs, and nodes pull it from there, never from a hosted registry.
        ``contract`` takes the ``piceli.toml`` keys of a component except
        ``build`` (``{"ports": {"redis": 6379}}``).
        """
        derived = ref.rsplit("/", 1)[-1].split(":", 1)[0]
        return cls(
            name=name or derived,
            image_ref=ref,
            pin=pin,
            options={"contract": dict(contract or {})},
            settings=dict(settings or {}),
        )

    @property
    def mirrored(self) -> str | None:
        """``ref@pin`` of a third-party image (``None`` for a built component)."""
        if self.image_ref is None:
            return None
        return f"{self.image_ref}@{self.pin}"
