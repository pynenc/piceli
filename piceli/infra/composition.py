"""A composition: the module (``infra.py``) that says what runs where, and when.

A composition module declares, as module attributes:

- ``environments = [...]``: :class:`piceli.envs.Environment` s whose
  ``follow`` maps each :class:`~piceli.infra.Source` to its rule
  (``{product: "main", muse: Tag("v*")}``), and at most one branch rule
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
from piceli.infra import Cluster, Component, CompositionError, Source

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
    :param cluster: The cluster (its registry receives the images).
    """

    name: str
    environments: tuple[EnvItem, ...]
    components: tuple[Component, ...]
    cluster: Cluster | None = None

    def __post_init__(self) -> None:
        if not _LABEL.fullmatch(self.name or ""):
            raise _invalid(f"composition name {self.name!r} is not a DNS label")
        if not self.environments:
            raise _invalid("the composition declares no environment")
        names = [item.name for item in self.environments]
        if len(set(names)) != len(names):
            raise _invalid("two environments share a name")
        named = [item for item in self.environments if isinstance(item, Environment)]
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
        for source in self.sources_of_components() + self.sources_of_envs():
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
            if self.cluster is not None and item.cluster not in (None, self.cluster):
                raise _invalid(f"environment {item.name!r} names another cluster")
            for name in item.settings:
                if name not in components:
                    raise _invalid(
                        f"environment {item.name!r}: settings name no component {name!r}"
                    )

    # ------------------------------------------------------------ queries
    @property
    def sources(self) -> tuple[Source, ...]:
        found: dict[str, Source] = {}
        for source in self.sources_of_envs() + self.sources_of_components():
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
            "environments": [_env_dict(item) for item in self.environments],
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
            environments = tuple(
                _env_from(item, sources, components, cluster)
                for item in value.get("environments") or ()
            )
            return cls(
                name=str(value["name"]),
                environments=environments,
                components=tuple(components.values()),
                cluster=cluster,
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


def _env_dict(item: EnvItem) -> dict[str, Any]:
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
    if isinstance(item, Environment):
        return {"kind": "environment", **common}
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
    }
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
    return Environment(item["name"], **common)


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


def _cluster_dict(cluster: Cluster) -> dict[str, Any]:
    registry = cluster.registry
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
    return {"name": cluster.name, "api": cluster.api, "registry": described}


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

    return Cluster(
        name=value["name"],
        api=value["api"],
        credentials=IN_CLUSTER_CONTEXT,
        registry=registry,
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
                f"importing the composition failed: {type(error).__name__}"
            ) from None
        raise
    return module


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
    values = vars(module) if not isinstance(module, Mapping) else dict(module)
    environments = values.get("environments")
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
    if len(clusters) > 1:
        raise _invalid("a composition deploys to one Cluster")
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
    if not components and not any(item.pipeline is not None for item in items):
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
        cluster=next(iter(clusters.values()), None),
    )
