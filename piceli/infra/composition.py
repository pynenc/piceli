"""A composition: the module (``infra.py``) that says what runs where, and when.

A composition module declares, as module attributes:

- ``environments = [...]``: :class:`piceli.envs.Environment` s whose
  ``follow`` maps each :class:`~piceli.infra.Source` to its rule
  (``{product: "main", sdk: Tag("v*")}``), and at most one branch rule
  (:meth:`piceli.envs.Environment.per_branch`);
- the :class:`~piceli.infra.Cluster` (optional; ``Registry.in_cluster`` is
  where built and mirrored images go);
- the :class:`~piceli.infra.Component` s, directly or through the
  :class:`piceli.envs.Stack` s of the environments, or, for an
  ``Environment(pipeline=...)``, a :class:`piceli.Pipeline` whose app the
  environment deploys (its host build's contexts read the sources);
- optionally ``name = "shop"`` (default: the file's stem): the app name of
  every object the composition deploys.

:func:`load_composition` imports the module and validates it;
:meth:`Composition.to_dict` is the plain-data form ``piceli gitops enable``
puts into the controller's config (so the install plan hash covers every
rule) and :meth:`Composition.from_dict` reads it back. A composition of
contract components only is plain data: the controller never imports user
code. A composition with pipeline environments is imported by the controller
from its own repository at the followed commit (:mod:`piceli.infra.pipelines`).

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from piceli.envs.model import (
    BRANCH,
    Branch,
    BranchEnvironments,
    Branches,
    EnvError,
    Environment,
    Promote,
    Stack,
    Tag,
)
from piceli.infra import Cluster, Component, CompositionError, Node, Source

COMPOSITION_SCHEMA = "piceli.composition.v1"
_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")

EnvItem = Environment | BranchEnvironments


def _invalid(message: str) -> CompositionError:
    return CompositionError("composition-invalid", message)


def _in_cluster(cluster: Cluster | None) -> bool:
    from piceli.pipeline.model import ClusterRegistry

    return cluster is not None and isinstance(cluster.registry, ClusterRegistry)


@dataclass(frozen=True)
class Composition:
    """Sources, components and environments, validated together.

    :param name: The app name of everything it deploys (a DNS label).
    :param environments: Named environments and at most one branch rule.
    :param components: Every component the environments use.
    :param cluster: The cluster (its registry receives the images); with
        several clusters, the one that runs the controller (the *home*
        cluster).
    :param clusters: Every declared cluster, the home one included (0.15;
        default: ``cluster`` alone).
    :param nix_releases: The Nix host releases the controller publishes
        (0.25.0, :class:`~piceli.infra.nix_releases.ReleaseTarget` of each
        :class:`~piceli.infra.NixRelease`); a composition of releases only
        needs no environment.
    """

    name: str
    environments: tuple[EnvItem, ...]
    components: tuple[Component, ...]
    cluster: Cluster | None = None
    clusters: tuple[Cluster, ...] = ()
    nix_releases: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        self._check_clusters()
        if not _LABEL.fullmatch(self.name or ""):
            raise _invalid(f"composition name {self.name!r} is not a DNS label")
        if not self.environments and not self.nix_releases:
            raise _invalid("the composition declares no environment")
        names = [item.name for item in self.environments]
        if len(set(names)) != len(names):
            raise _invalid("two environments share a name")
        released = [item.name for item in self.nix_releases]
        if len(set(released)) != len(released):
            raise _invalid("two NixReleases release the same host")
        for name in set(released) & set(names):
            raise _invalid(
                f"NixRelease {name!r} has an environment's name: approve and sync "
                "name either"
            )
        named = [item for item in self.environments if isinstance(item, Environment)]
        for release in self.nix_releases:  # 0.25.0: a promotion gate
            gate = getattr(release, "gate", None)
            if gate is None:
                continue
            env = next((i for i in named if i.name == gate.environment), None)
            if env is None:
                raise _invalid(
                    f"NixRelease {release.name!r}: gate environment "
                    f"{gate.environment!r} is no Environment of the composition"
                )
            if gate.source_key not in {source.key for source, _ in env.sources}:
                raise _invalid(
                    f"NixRelease {release.name!r}: environment {env.name!r} does "
                    f"not follow the gate's source {gate.source_key!r}"
                )
        spaces = [item.namespace for item in named]
        if len(set(spaces)) != len(spaces):
            raise _invalid("two environments share a namespace")
        rules = [i for i in self.environments if isinstance(i, BranchEnvironments)]
        if len(rules) > 1:
            raise _invalid("declare at most one Environment.per_branch(...) rule")
        components: dict[str, Component] = {}
        for component in self.components:
            if components.get(component.name, component) != component:
                raise _invalid(f"two different components are named {component.name!r}")
            components[component.name] = component
        object.__setattr__(self, "components", tuple(components.values()))
        sources: dict[str, Source] = {}
        declared: list[Source] = [
            *self.sources_of_components(),
            *self.sources_of_envs(),
        ]
        declared += [item.source for item in self.nix_releases]
        for source in declared:
            if sources.get(source.key, source).url != source.url:
                raise _invalid(f"two sources are named {source.key!r}")
            sources[source.key] = source
        for item in self.environments:
            if not item.sources:
                raise _invalid(
                    f"environment {item.name!r}: follow maps each Source to its rule "
                    "(follow={source: 'main'})"
                )
            if item.pipeline is not None and not _in_cluster(self.cluster):
                raise _invalid(
                    f"environment {item.name!r} deploys a pipeline: the composition's "
                    "Cluster needs registry=Registry.in_cluster(...) (its images go there)"
                )
            followed = {source.key for source, _ in item.sources}
            for component in self.stack_of(item):
                if (
                    component.source is not None
                    and component.source.key not in followed
                ):
                    raise _invalid(
                        f"environment {item.name!r} runs {component.name!r} but does "
                        f"not follow its source {component.source.key!r}"
                    )
            if item.cluster is not None and item.cluster not in self.clusters:
                raise _invalid(f"environment {item.name!r} names another cluster")
            for placement in self.placements_of(item):
                if placement.cluster not in self.clusters:
                    raise _invalid(
                        f"environment {item.name!r} places a cluster the composition "
                        f"does not declare ({placement.name!r})"
                    )
                if (
                    item.pipeline is not None
                    and placement.cluster != self.cluster
                    and not _in_cluster(placement.cluster)
                ):
                    raise _invalid(
                        f"environment {item.name!r} deploys a pipeline to "
                        f"{placement.name!r}: that Cluster needs "
                        "registry=Registry.in_cluster(...) (its nodes pull from it)"
                    )
            for name in item.settings:
                if name not in components:
                    raise _invalid(
                        f"environment {item.name!r}: settings name no component {name!r}"
                    )

    def _check_clusters(self) -> None:
        clusters = tuple(self.clusters) or (
            (self.cluster,) if self.cluster is not None else ()
        )
        if self.cluster is not None and self.cluster not in clusters:
            clusters = (self.cluster, *clusters)
        names = [item.name for item in clusters]
        if len(set(names)) != len(names):
            raise _invalid("two clusters share a name")
        if len(clusters) > 1 and self.cluster is None:
            raise _invalid(
                "several clusters: one runs the controller (Controller(...) on it)"
            )
        object.__setattr__(self, "clusters", clusters)

    # ------------------------------------------------------------ queries
    @property
    def remote_clusters(self) -> tuple[Cluster, ...]:
        """The declared clusters other than the home one (0.15)."""
        return tuple(item for item in self.clusters if item != self.cluster)

    def cluster_named(self, name: str) -> Cluster | None:
        return next((item for item in self.clusters if item.name == name), None)

    def placements_of(self, env: EnvItem) -> tuple[Any, ...]:
        """Where ``env`` runs when that is not just the home cluster.

        Its ``clusters=`` placements; an environment whose ``cluster=`` is
        another than the home cluster is placed there alone. ``()``: the
        home cluster, as in 0.14.
        """
        from piceli.envs.placement import Placement

        if env.clusters:
            return tuple(env.clusters)
        if env.cluster is not None and env.cluster != self.cluster:
            return (Placement(env.cluster),)
        return ()

    @property
    def sources(self) -> tuple[Source, ...]:
        found: dict[str, Source] = {}
        for source in (
            self.sources_of_envs()
            + self.sources_of_components()
            + [item.source for item in self.nix_releases]
        ):
            found.setdefault(source.key, source)
        return tuple(found[key] for key in sorted(found))

    def sources_of_envs(self) -> list[Source]:
        return [source for item in self.environments for source, _ in item.sources]

    def sources_of_components(self) -> list[Source]:
        return [item.source for item in self.components if item.source is not None]

    def component(self, name: str) -> Component:
        for item in self.components:
            if item.name == name:
                return item
        raise _invalid(f"no component {name!r}")

    def stack_of(self, env: EnvItem) -> tuple[Component, ...]:
        """The components ``env`` runs: its stack's, else every component.

        An environment that deploys a pipeline runs no component.
        """
        if env.pipeline is not None:
            return ()
        if env.stack is None:
            return self.components
        return tuple(self.component(name) for name in env.stack.workloads)

    def environment(self, name: str) -> EnvItem | None:
        return next((item for item in self.environments if item.name == name), None)

    @property
    def branch_rule(self) -> BranchEnvironments | None:
        return next(
            (i for i in self.environments if isinstance(i, BranchEnvironments)), None
        )

    @property
    def registry(self) -> Any:
        return None if self.cluster is None else self.cluster.registry

    @property
    def pipelines(self) -> bool:
        """Whether an environment deploys a pipeline (the controller imports the module)."""
        return any(item.pipeline is not None for item in self.environments)

    # ------------------------------------------------------------ plain data
    def to_dict(self) -> dict[str, Any]:
        """The composition as plain data (``piceli.composition.v1``); no secret."""
        return {
            "schema": COMPOSITION_SCHEMA,
            "name": self.name,
            "cluster": None if self.cluster is None else _cluster_dict(self.cluster),
            "sources": [
                {"name": source.key, "url": source.url} for source in self.sources
            ],
            "components": [_component_dict(item) for item in self.components],
            "environments": [
                _env_dict(item, self.cluster) for item in self.environments
            ],
            # Only with several clusters: other configs (and hashes) unchanged.
            **(
                {
                    "clusters": [
                        _cluster_dict(item, remote=True)
                        for item in self.remote_clusters
                    ]
                }
                if self.remote_clusters
                else {}
            ),
            # 0.25.0, only when declared: other configs (and hashes) unchanged.
            **(
                {"nix_releases": [item.to_dict() for item in self.nix_releases]}
                if self.nix_releases
                else {}
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Composition:
        """Read :meth:`to_dict` back.

        :raises CompositionError: ``composition-invalid``.
        """
        if not isinstance(value, Mapping) or value.get("schema") != COMPOSITION_SCHEMA:
            raise _invalid("unknown composition schema")
        if any(item.get("pipeline") for item in value.get("environments") or ()):
            raise _invalid(
                "a composition with pipeline environments is imported from its "
                "repository, not read from plain data"
            )
        try:
            sources = {
                item["name"]: Source(item["url"], name=item["name"])
                for item in value.get("sources") or ()
            }
            components = {
                item["name"]: _component_from(item, sources)
                for item in value.get("components") or ()
            }
            cluster = _cluster_from(value.get("cluster"))
            others = tuple(
                found
                for found in (
                    _cluster_from(item) for item in value.get("clusters") or ()
                )
                if found is not None
            )
            named = {item.name: item for item in (cluster, *others) if item is not None}
            environments = tuple(
                _env_from(item, sources, components, cluster, named)
                for item in value.get("environments") or ()
            )
            from piceli.infra.nix_releases import ReleaseTarget

            releases = tuple(
                ReleaseTarget.from_dict(item, sources)
                for item in value.get("nix_releases") or ()
            )
            return cls(
                name=str(value["name"]),
                environments=environments,
                components=tuple(components.values()),
                cluster=cluster,
                clusters=tuple(named.values()),
                nix_releases=releases,
            )
        except (KeyError, TypeError, ValueError, EnvError) as error:
            if isinstance(error, CompositionError):
                raise
            raise _invalid(f"the composition config is invalid: {error}") from None


# ---------------------------------------------------------------- plain data


def _follow_dict(item: Any) -> Any:
    if item == BRANCH:
        return BRANCH
    if isinstance(item, Branch):
        return {"branch": item.name}
    if isinstance(item, Tag):
        return {"tag": item.pattern}
    return {"promote": True}


def _follow_from(item: Any) -> Any:
    if item == BRANCH:
        return BRANCH
    if "branch" in item:
        return Branch(item["branch"])
    if "tag" in item:
        return Tag(item["tag"])
    return Promote()


def _stack_dict(stack: Stack | None) -> Any:
    return (
        None
        if stack is None
        else {"name": stack.name, "components": list(stack.workloads)}
    )


def _env_dict(item: EnvItem, home: Cluster | None = None) -> dict[str, Any]:
    follow = {
        source.key: [_follow_dict(rule) for rule in rules]
        for source, rules in item.sources
    }
    common = {
        "name": item.name,
        "namespace": item.namespace,
        "follow": follow,
        "stack": _stack_dict(item.stack),
        "on_nodes": _nodes(item.on_nodes),
        "quota": None if item.quota is None else dict(item.quota),
        "auto_approve": item.auto_approve,
        "secrets": list(item.secrets),
        "settings": {k: dict(v) for k, v in item.settings.items()},
    }
    if item.pipeline is not None:
        # A summary (the plan hash covers it); the controller imports the module.
        common["pipeline"] = pipeline_summary(item.pipeline)
        if item.restore_points is not None:
            from piceli.envs.model import describe_restore_points

            common["restore_points"] = describe_restore_points(item.restore_points)
    if item.replicas:
        # Only when declared (0.15): other configs keep their hash.
        common["replicas"] = dict(item.replicas)
    if item.clusters:
        # Only when declared (0.15): single-cluster configs keep their hash.
        common["clusters"] = [_placement_dict(p) for p in item.clusters]
        if item.rollout is not None:
            common["rollout"] = item.rollout.describe()
    elif item.cluster is not None and home is not None and item.cluster != home:
        # Another cluster than the home one, named alone (cluster=edge).
        common["clusters"] = [{"cluster": item.cluster.name}]
    if isinstance(item, Environment):
        # Only when declared: compositions without it keep their summary hash.
        return {
            "kind": "environment",
            **common,
            **({"stopped": True} if item.stopped else {}),
        }
    return {
        "kind": "branches",
        **common,
        "branches": list(item.branches.patterns),
        "fallback": item.branches.fallback,
        "limit": item.limit,
        "idle_stop": item.idle_stop,
        "claim_sizes": dict(item.claim_sizes),
        "allow_egress": list(item.allow_egress),
        **({"allow_api": True} if item.allow_api else {}),
    }


def _placement_dict(placement: Any) -> dict[str, Any]:
    body = placement.describe()
    if placement.pipeline is not None:
        body["pipeline"] = pipeline_summary(placement.pipeline)
    return body


def _placement_from(item: Mapping[str, Any], clusters: Mapping[str, Cluster]) -> Any:
    from piceli.envs.placement import Placement

    return Placement(
        clusters[item["cluster"]],
        namespace=item.get("namespace"),
        on_nodes=item.get("on_nodes"),
        values=item.get("values") or {},
        replicas=item.get("replicas") or None,
    )


def pipeline_summary(pipeline: Any) -> dict[str, Any]:
    """What a pipeline environment deploys, as plain data (no paths, no secrets)."""
    return {
        "app": pipeline.name,
        "builds": [
            {"builder": build.builder, "spec": build.path.name if build.path else None}
            for build in pipeline.builds
        ],
        "checks": len(pipeline.checks),
        "rollback_on_failed_checks": pipeline.rollback_on_failed_checks,
    }


def _nodes(value: Any) -> Any:
    if value is None:
        return None
    return dict(value) if isinstance(value, Mapping) else list(value)


def _env_from(
    item: Mapping[str, Any],
    sources: Mapping[str, Source],
    components: Mapping[str, Component],
    cluster: Cluster | None,
    clusters: Mapping[str, Cluster] | None = None,
) -> EnvItem:
    follow = {
        sources[key]: [_follow_from(rule) for rule in rules]
        for key, rules in item["follow"].items()
    }
    stack = None
    if item.get("stack") is not None:
        stack = Stack(
            item["stack"]["name"],
            [components[name] for name in item["stack"]["components"]],
        )
    common: dict[str, Any] = {
        "namespace": item["namespace"],
        "follow": follow,
        "stack": stack,
        "cluster": cluster,
        "on_nodes": item.get("on_nodes"),
        "quota": item.get("quota"),
        "auto_approve": bool(item.get("auto_approve")),
        "secrets": tuple(item.get("secrets") or ()),
        "settings": dict(item.get("settings") or {}),
        "replicas": item.get("replicas") or None,
    }
    if item.get("clusters"):
        from piceli.envs.placement import Rollout

        common["cluster"] = None
        common["clusters"] = tuple(
            _placement_from(entry, clusters or {}) for entry in item["clusters"]
        )
        if item.get("rollout"):
            common["rollout"] = Rollout(tuple(item["rollout"]["order"]))
    if item.get("kind") == "branches":
        return Environment.per_branch(
            Branches(tuple(item["branches"]), fallback=item.get("fallback") or "main"),
            name=item["name"],
            limit=int(item.get("limit") or 3),
            idle_stop=item.get("idle_stop"),
            claim_sizes=item.get("claim_sizes") or {},
            allow_egress=tuple(item.get("allow_egress") or ()),
            allow_api=item.get("allow_api") is True,
            **common,
        )
    return Environment(item["name"], stopped=item.get("stopped") is True, **common)


def _component_dict(item: Component) -> dict[str, Any]:
    return {
        "name": item.name,
        "source": None if item.source is None else item.source.key,
        "image": item.image_ref,
        "pin": item.pin,
        "contract": dict(item.options.get("contract") or {})  # type: ignore[call-overload]
        if item.image_ref is not None
        else None,
        "settings": dict(item.settings),
    }


def _component_from(
    item: Mapping[str, Any], sources: Mapping[str, Source]
) -> Component:
    if item.get("image") is not None:
        return Component.image(
            item["image"],
            pin=item["pin"],
            name=item["name"],
            contract=item.get("contract") or {},
            settings=item.get("settings") or {},
        )
    return Component(
        item["name"],
        source=sources[item["source"]],
        settings=dict(item.get("settings") or {}),
    )


def _cluster_dict(cluster: Cluster, *, remote: bool = False) -> dict[str, Any]:
    registry = cluster.registry
    if remote and registry is None:
        return {"name": cluster.name, "api": cluster.api, "registry": None}
    described = None
    if registry is not None:
        from piceli.pipeline.model import ClusterRegistry

        if not isinstance(registry, ClusterRegistry):
            raise _invalid(
                "the cluster's registry must be Registry.in_cluster(...): nodes pull "
                "built and mirrored images from it"
            )
        described = {
            "on": registry.on,
            "port": registry.port,
            "namespace": registry.namespace,
            "name": registry.name,
            "repository": registry.repository,
            "storage": registry.storage,
            # Only when declared: other configs (and their hashes) are unchanged.
            **({"mirror": list(registry.mirror)} if registry.mirror else {}),
        }
    body: dict[str, Any] = {
        "name": cluster.name,
        "api": cluster.api,
        "registry": described,
    }
    controller = cluster.controller
    if controller is not None and (
        tuple(controller.builds) != ("cluster",) or controller.prebuilt
    ):
        # 0.21.0, only when declared (other configs and hashes unchanged).
        body["controller"] = {
            "on": controller.on,
            "builds": list(controller.builds),
            "host_wait": controller.host_wait,
        }
        if controller.prebuilt:  # 0.26.0, only when on
            body["controller"]["prebuilt"] = True
        if controller.delivery == "nodes":
            # 0.22.0: the nodes build hosts import into, with their
            # architecture; the targets without their SSH user.
            from piceli.infra.cluster import node_target_identity

            body["controller"]["delivery"] = "nodes"
            body["nodes"] = [
                {
                    "name": node.name,
                    "arch": node.arch,
                    "target": node_target_identity(str(node.target)),
                }
                for node in cluster.nodes
            ]
    return body


def _cluster_from(value: Any) -> Cluster | None:
    if value is None:
        return None
    from piceli.pipeline.model import Registry

    registry = None
    if value.get("registry") is not None:
        found = value["registry"]
        registry = Registry.in_cluster(
            on=found["on"],
            port=int(found["port"]),
            namespace=found["namespace"],
            name=found["name"],
            repository=found.get("repository"),
            storage=found.get("storage") or "20Gi",
            mirror=tuple(found.get("mirror") or ()),
        )
    # Credentials are a local profile and never part of the config; in the
    # controller's pod every profile name resolves to its service account.
    from piceli.profiles import IN_CLUSTER_CONTEXT

    controller = None
    if isinstance(value.get("controller"), dict):
        from piceli.infra import Controller

        found_controller = value["controller"]
        controller = Controller(
            on=found_controller["on"],
            builds=tuple(found_controller.get("builds") or ("cluster",)),
            host_wait=str(found_controller.get("host_wait") or "30m"),
            delivery=found_controller.get("delivery") or "registry",
            prebuilt=bool(found_controller.get("prebuilt", False)),
        )
    nodes = tuple(
        Node(item["name"], arch=item["arch"], target=item.get("target"))
        for item in value.get("nodes") or ()
    )
    return Cluster(
        name=value["name"],
        api=value["api"],
        credentials=IN_CLUSTER_CONTEXT,
        nodes=nodes,
        registry=registry,
        controller=controller,
    )


# ---------------------------------------------------------------- loading


def _import(path: Path) -> Any:
    name = "_piceli_composition_" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
    # Imported afresh each time: the module may read its environment.
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise _invalid("cannot import the composition module")
    module = importlib.util.module_from_spec(spec)
    _prefer_siblings(path.parent)
    if str(path.parent) not in sys.path:
        sys.path.append(str(path.parent))
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException as error:
        del sys.modules[name]
        if isinstance(error, CompositionError | EnvError):
            raise _invalid(str(error)) from None
        if isinstance(error, Exception):
            raise _invalid(
                f"importing the composition failed: {_import_error(error)}"
            ) from None
        raise
    return module


def _import_error(error: Exception) -> str:
    """``Type: message`` of an import failure (0.27.1: the type alone said
    too little), the message's first line scrubbed of secrets and cut short."""
    from piceli.pipeline.prerollout import scrub

    lines = str(error).strip().splitlines()
    message = scrub(lines[0])[:300] if lines else ""
    return type(error).__name__ + (f": {message}" if message else "")


def _installed() -> list[Path]:
    import sysconfig

    paths = sysconfig.get_paths()
    return [
        Path(paths[key])
        for key in ("stdlib", "platstdlib", "purelib", "platlib")
        if key in paths
    ]


def _prefer_siblings(directory: Path) -> None:
    """The module's sibling files win over same-named ones imported from elsewhere.

    The composition imports the files next to it (``from app import
    pipeline``). Another directory on ``sys.path`` holding a file of the same
    name (another checkout of the composition) is dropped from it, and a
    module of that name imported from there is forgotten; the standard
    library and installed packages are never touched.
    """
    names = {
        item.stem if item.suffix == ".py" else item.name
        for item in directory.iterdir()
        if item.suffix == ".py" or (item / "__init__.py").is_file()
    }
    installed = _installed()

    def foreign(location: Path) -> bool:
        return not location.is_relative_to(directory) and not any(
            location.is_relative_to(root) for root in installed
        )

    import piceli

    # Piceli's own entry (an editable install puts its checkout on sys.path).
    own = Path(piceli.__file__).parent.parent

    def shadows(entry: str) -> bool:
        found = Path(entry or ".").absolute()
        return (
            found != own
            and foreign(found)
            and any(
                (found / f"{name}.py").is_file()
                or (found / name / "__init__.py").is_file()
                for name in names
            )
        )

    sys.path[:] = [item for item in sys.path if not shadows(item)]
    for name, loaded in list(sys.modules.items()):
        location = getattr(loaded, "__file__", None)
        if name.split(".", 1)[0] in names and isinstance(location, str):
            if foreign(Path(location)):
                del sys.modules[name]


def is_composition(path: Path) -> bool:
    """Whether ``path`` is a ``.py`` file (a composition entry has no ``:ATTR``)."""
    return path.suffix == ".py" and path.is_file()


def load_composition(entry: str | Path, root: Path | None = None) -> Composition:
    """Import a composition module (``infra.py``) and validate it.

    :param entry: The module file (relative to ``root``).
    :raises CompositionError: ``composition-invalid`` (no ``environments``,
        an environment without a source mapping, a component whose source the
        environment does not follow, two sources or components with one name).
    """
    path = (Path(root or ".") / Path(entry)).resolve()
    if not is_composition(path):
        raise _invalid("the composition is a .py file")
    return composition_from(_import(path), default_name=path.stem)


def composition_from(module: Any, *, default_name: str = "composition") -> Composition:
    """The :class:`Composition` of an imported module (or any namespace object)."""
    from piceli.infra.nix_releases import NixRelease

    values = vars(module) if not isinstance(module, Mapping) else dict(module)
    # 0.25.0: every NixRelease of the module, as Components are found.
    releases = tuple(
        {
            id(value): value
            for value in values.values()
            if isinstance(value, NixRelease)
        }.values()
    )
    environments = values.get("environments", () if releases else None)
    if isinstance(environments, str) or not isinstance(environments, Sequence):
        raise _invalid("the module declares no environments = [...] list")
    items = tuple(environments)
    if not all(isinstance(item, Environment | BranchEnvironments) for item in items):
        raise _invalid(
            "environments holds Environment(...) and Environment.per_branch(...) items"
        )
    clusters = {id(v): v for v in values.values() if isinstance(v, Cluster)}
    for item in items:
        if item.cluster is not None:
            clusters.setdefault(id(item.cluster), item.cluster)
        for placement in item.clusters:
            clusters.setdefault(id(placement.cluster), placement.cluster)
    home = _home(list(clusters.values()), items)
    components: list[Component] = [
        value for value in values.values() if isinstance(value, Component)
    ]
    for item in items:
        if item.stack is not None and item.pipeline is None:
            for component in item.stack.components:
                if not isinstance(component, Component):
                    raise _invalid(
                        f"stack {item.stack.name!r} holds {component!r}, not a Component"
                    )
                components.append(component)
            missing = set(item.stack.workloads) - {c.name for c in components}
            if missing:
                raise _invalid(
                    f"stack {item.stack.name!r} names {sorted(missing)[0]!r}, which is "
                    "no Component of the composition"
                )
    if (
        not components
        and not any(item.pipeline is not None for item in items)
        and not (releases and not items)
    ):
        raise _invalid("the composition declares no Component and no pipeline")
    name = values.get("name")
    if not isinstance(name, str):
        name = (
            re.sub(r"[^a-z0-9]+", "-", default_name.lower()).strip("-") or "composition"
        )
    return Composition(
        name=name,
        environments=items,
        components=tuple(components),
        cluster=home,
        clusters=tuple(clusters.values()),
        nix_releases=tuple(item.target() for item in releases),
    )


def _home(clusters: list[Cluster], items: Sequence[EnvItem]) -> Cluster | None:
    """The cluster that runs the controller: the one with ``Controller(...)``;
    one cluster alone is home; else the one environments name with ``cluster=``."""
    if len(clusters) <= 1:
        return next(iter(clusters), None)
    with_controller = [item for item in clusters if item.controller is not None]
    if len(with_controller) > 1:
        raise _invalid("two clusters declare a Controller: one GitOps controller")
    if with_controller:
        return with_controller[0]
    named = {id(item.cluster): item.cluster for item in items if item.cluster}
    if len(named) == 1:
        return next(iter(named.values()))
    raise _invalid(
        "several clusters: declare Controller(...) on the one that runs the controller"
    )
