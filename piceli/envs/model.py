"""Declaration of per-branch environments: :class:`EnvConfig` and the namespace mapping.

Importing this module is side-effect free.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from piceli.pipeline.errors import PipelineError

#: Label on every branch namespace: the app it belongs to.
ENV_OF_LABEL = "piceli.io/env-of"
#: Label on every branch namespace: the branch slug.
ENV_BRANCH_LABEL = "piceli.io/env-branch"
#: Annotation on every branch namespace: the branch name as given.
ENV_BRANCH_ANNOTATION = "piceli.io/env-branch"
MANAGED_BY = {"app.kubernetes.io/managed-by": "piceli"}
#: The ConfigMap in each environment's namespace that records its state.
RECORD_NAME = "piceli-env"
#: Name of the isolation NetworkPolicy and ResourceQuota of a branch env.
ISOLATION_NAME = "piceli-env-isolation"
#: Label on the namespace Piceli creates for a named environment.
ENV_NAME_LABEL = "piceli.io/env-name"
#: Kinds a :class:`Stack` names and ``on_nodes`` places.
STACK_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")

_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_PREFIX = re.compile(r"[a-z]([-a-z0-9]{0,38}[-a-z0-9])?")
_SIZE = re.compile(r"[0-9]+(\.[0-9]+)?([KMGTPE]i?)?")
_HASH = 8
_MAX = 63
_IDLE = re.compile(r"([0-9]+)\s*([smhd])")
_LABEL_KEY = re.compile(r"(?:[a-z0-9.-]{1,253}/)?[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
_LABEL_VALUE = re.compile(r"[A-Za-z0-9._-]{0,63}")
_GLOB_CHARS = frozenset("*?[]")

#: Counts a branch env may use when ``quota`` is not declared (they need no
#: pod resource requests, unlike ``requests.cpu`` or ``requests.memory``).
DEFAULT_QUOTA: Mapping[str, str] = {
    "pods": "50",
    "persistentvolumeclaims": "10",
    "services.nodeports": "0",
    "services.loadbalancers": "0",
}


class EnvError(PipelineError):
    """An environment command was refused or failed (``code`` is registered)."""


def slug(branch: str) -> str:
    """``branch`` as a DNS-1123 label fragment (lowercase, ``-`` separated)."""
    value = re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")
    return re.sub(r"-{2,}", "-", value)


# ------------------------------------------------------- named environments


@dataclass(frozen=True)
class Branch:
    """Follow every push to one branch (``Environment(follow=Branch("main"))``).

    :param name: The exact branch name (no glob).
    """

    name: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not self.name.strip()
            or any(ch in _GLOB_CHARS for ch in self.name)
        ):
            raise EnvError(
                "env-config-invalid", "Branch() takes one exact branch name, no glob"
            )

    def describe(self) -> dict[str, str]:
        return {"kind": "branch", "branch": self.name}


@dataclass(frozen=True)
class Tag:
    """Follow new tags matching a glob (``Tag("v*-rc*")``); the latest new one deploys.

    :param pattern: The tag glob.
    """

    pattern: str = "v*"

    def __post_init__(self) -> None:
        if not isinstance(self.pattern, str) or not self.pattern.strip():
            raise EnvError("env-config-invalid", "Tag() takes a non-empty tag glob")

    def describe(self) -> dict[str, str]:
        return {"kind": "tag", "pattern": self.pattern}


@dataclass(frozen=True)
class Promote:
    """Deploy what ``piceli promote ENV BRANCH@SHA`` names."""

    def describe(self) -> dict[str, str]:
        return {"kind": "promote"}


Follow = Branch | Tag | Promote

#: In ``Environment.per_branch(follow={source: BRANCH})``: the source follows
#: the environment's own branch.
BRANCH = "{branch}"


@dataclass(frozen=True)
class Branches:
    """The branches that get an environment each (``Environment.per_branch(Branches("wp-*"))``).

    :param patterns: Branch globs (``"wp-*"``), one or a list.
    :param fallback: A source that follows ``"{branch}"`` but has no such
        branch deploys this branch instead (default ``main``).
    """

    patterns: Any = "*"
    fallback: str = "main"

    def __post_init__(self) -> None:
        items = (self.patterns,) if isinstance(self.patterns, str) else self.patterns
        if (
            not isinstance(items, Sequence)
            or not items
            or not all(isinstance(item, str) and item.strip() for item in items)
        ):
            raise EnvError("env-config-invalid", "Branches() takes branch globs")
        object.__setattr__(self, "patterns", tuple(dict.fromkeys(items)))
        Branch(self.fallback)  # an exact branch name

    def matches(self, branch: str) -> bool:
        return any(fnmatch.fnmatchcase(branch, glob) for glob in self.patterns)


def _follow_items(value: Any, what: str, *, per_branch: bool) -> tuple[Any, ...]:
    """One source's follow rule: ``"main"``, ``Branch``, ``Tag``, ``Promote``, a list."""
    items = (
        (value,)
        if isinstance(value, str | Branch | Tag | Promote)
        or not isinstance(value, Sequence)
        else tuple(value)
    )
    found: list[Any] = []
    for item in items:
        if item == BRANCH and per_branch:
            found.append(BRANCH)
        elif isinstance(item, str) and "{" not in item:
            found.append(Branch(item))
        elif isinstance(item, Branch | Tag | Promote):
            found.append(item)
        else:
            raise EnvError(
                "env-config-invalid",
                f"{what}: follow a source with a branch name, Branch(...), "
                "Tag(...), Promote()" + (' or "{branch}"' if per_branch else ""),
            )
    if not found:
        raise EnvError("env-config-invalid", f"{what}: follow names nothing")
    return tuple(dict.fromkeys(found))


def _describe_follow(item: Any) -> dict[str, str]:
    if item == BRANCH:
        return {"kind": "env-branch"}
    described: dict[str, str] = item.describe()
    return described


def _source_follow(
    follow: Mapping[Any, Any], what: str, *, per_branch: bool
) -> tuple[tuple[Any, tuple[Any, ...]], ...]:
    """``{Source: rule}`` → ``((source, items), ...)`` (sources named uniquely)."""
    from piceli.infra import Source

    pairs: list[tuple[Any, tuple[Any, ...]]] = []
    names: set[str] = set()
    for source, rule in follow.items():
        if not isinstance(source, Source):
            raise EnvError(
                "env-config-invalid",
                f"{what}: follow maps Source(...) objects to their rule",
            )
        if source.key in names:
            raise EnvError(
                "env-config-invalid", f"{what}: two sources named {source.key!r}"
            )
        names.add(source.key)
        pairs.append(
            (source, _follow_items(rule, f"{what} {source.key}", per_branch=per_branch))
        )
    if not pairs:
        raise EnvError("env-config-invalid", f"{what}: follow names no source")
    return tuple(pairs)


def _settings(value: Any, what: str) -> dict[str, dict[str, str]]:
    """``{component: {key: value}}`` overrides of component settings."""
    if not isinstance(value, Mapping) or not all(
        isinstance(name, str)
        and isinstance(table, Mapping)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in table.items())
        for name, table in value.items()
    ):
        raise EnvError(
            "env-config-invalid", f"{what} maps component names to {{key: value}}"
        )
    return {name: dict(sorted(table.items())) for name, table in sorted(value.items())}


def _secrets(value: Any, what: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise EnvError("env-config-invalid", f"{what} must be a list of Secret names")
    if not all(_is_label(item) for item in value):
        raise EnvError(
            "env-config-invalid", f"{what} holds a name that is no DNS label"
        )
    return tuple(dict.fromkeys(value))


def _names(values: Any, what: str) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence | tuple | list):
        raise EnvError("env-config-invalid", f"{what} must be a list")
    names: list[str] = []
    for value in values:
        name = value if isinstance(value, str) else getattr(value, "name", None)
        if not isinstance(name, str) or not _is_label(name):
            raise EnvError(
                "env-config-invalid", f"{what} holds {value!r}, not a workload name"
            )
        names.append(name)
    return tuple(dict.fromkeys(names))


@dataclass(frozen=True)
class Stack:
    """A named subset of the app's workloads (``Stack("minimal", workloads=[api, "db"])``).

    An environment with a stack renders only these workloads (and the objects
    of their components); a workload of the stack that depends on a left-out
    component is refused (``env-stack-incomplete``), and a name that is no
    workload of the app too (``env-stack-unknown``). Configs, Secrets and
    other objects outside any workload's component always render.

    :param name: The stack's name (shown in plans and ``piceli envs``).
    :param workloads: Workload names, or the declared workloads themselves.
    """

    name: str
    workloads: Sequence[Any] = ()
    #: The declared items that are not names (``Component`` s of a composition).
    declared: tuple[Any, ...] = field(init=False, default=(), compare=False, repr=False)

    def __post_init__(self) -> None:
        if not _is_label(self.name):
            raise EnvError("env-config-invalid", "a Stack name is a DNS label")
        if not isinstance(self.workloads, str) and isinstance(
            self.workloads, Sequence | tuple | list
        ):
            object.__setattr__(
                self,
                "declared",
                tuple(item for item in self.workloads if not isinstance(item, str)),
            )
        object.__setattr__(
            self, "workloads", _names(self.workloads, f"Stack {self.name!r} workloads")
        )
        if not self.workloads:
            raise EnvError(
                "env-config-invalid", f"Stack {self.name!r} names no workload"
            )

    @property
    def components(self) -> list[Any]:
        """The ``Component`` s this stack was declared with (a composition's stack)."""
        return list(self.declared)

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "workloads": list(self.workloads)}


def _nodes(value: Any, what: str) -> tuple[str, ...] | dict[str, str] | None:
    """``on_nodes``: node names, or a ``{label: value}`` node selector."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        selector = dict(sorted(value.items()))
        if not selector or not all(
            isinstance(k, str)
            and isinstance(v, str)
            and _LABEL_KEY.fullmatch(k)
            and _LABEL_VALUE.fullmatch(v)
            for k, v in selector.items()
        ):
            raise EnvError(
                "env-config-invalid", f"{what} must be node labels: {{key: value}}"
            )
        return selector
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise EnvError(
            "env-config-invalid", f"{what} must be a list of node names or labels"
        )
    names = tuple(dict.fromkeys(value))
    if not names or not all(
        isinstance(name, str) and name and len(name) <= 253 for name in names
    ):
        raise EnvError("env-config-invalid", f"{what} must name at least one node")
    return names


def _quota(value: Any, what: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise EnvError("env-config-invalid", f"{what} maps resource names to strings")
    return dict(sorted(value.items()))


def parse_idle(value: str | int) -> int:
    """Seconds from ``"24h"``, ``"90m"``, ``"2d"``, ``"3600s"`` (at least a minute)."""
    if isinstance(value, int) and not isinstance(value, bool):
        seconds = value
    else:
        match = _IDLE.fullmatch(str(value).strip())
        if match is None:
            raise EnvError(
                "env-config-invalid",
                f"idle_stop {value!r} is not a duration (90m, 24h, 2d)",
            )
        seconds = (
            int(match.group(1))
            * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
        )
    if seconds < 60:
        raise EnvError("env-config-invalid", "idle_stop must be at least a minute")
    return seconds


@dataclass(frozen=True)
class Environment:
    """A named, long-lived environment with its own trigger (``EnvConfig(environments=[...])``).

    Not to be confused with :class:`piceli.Environment` (an app overlay for
    ``--env``): this one is *where* the pipeline deploys and *what* deploys
    it. The GitOps controller evaluates each environment's ``follow``
    independently, so one branch may feed two environments.

    :param name: The environment's name (a DNS label): ``piceli env up NAME``,
        ``piceli promote NAME BRANCH@SHA``, ``piceli gitops approve NAME``.
        Named like ``EnvConfig.main_branch`` it replaces the implicit main
        environment.
    :param namespace: Its namespace; created by ``env up`` when absent, never
        deleted or stopped by env commands.
    :param follow: :class:`Branch` (every push), :class:`Tag` (a new
        matching tag) and/or :class:`Promote` (``piceli promote``); one or a
        list.
    :param stack: Render only this :class:`Stack` of the app.
    :param on_nodes: Node names (``kubernetes.io/hostname In [...]``) or a
        ``{label: value}`` selector, put on every workload.
    :param quota: A ``ResourceQuota`` ``spec.hard`` for the namespace.
    :param auto_approve: The owner allows the controller to deploy this
        environment without a hash approval when the plan is inside the
        pipeline's ``auto_approve`` policy (like ``--main-auto-approve``).

    Example::

        Environment("main", namespace="shop-main", follow=Branch("main"))
        Environment("rc", namespace="shop", follow=[Tag("v*-rc*"), Promote()])
    """

    name: str
    namespace: str
    follow: Any = field(default_factory=Promote)
    stack: Stack | None = None
    on_nodes: Any = None
    quota: Mapping[str, str] | None = None
    auto_approve: bool = False
    #: The composition's cluster (``piceli.infra.Cluster``) it deploys to.
    cluster: Any = None
    #: Secrets the environment's namespace provides (``needs = ["secret:NAME"]``).
    secrets: Sequence[str] = ()
    #: ``{component: {key: value}}``: overrides of the components' ``settings``.
    settings: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    #: A composition environment that deploys this :class:`piceli.Pipeline`'s
    #: app (its workloads, checks, rollback and approval policy) instead of
    #: contract components; its images are built from the composition's sources.
    pipeline: Any = field(default=None, compare=False)
    #: ``follow={Source: rule}`` normalised: ``((source, rules), ...)``.
    sources: tuple[tuple[Any, tuple[Any, ...]], ...] = field(
        init=False, default=(), compare=False, repr=False
    )

    def __post_init__(self) -> None:
        if not _is_label(self.name):
            raise EnvError(
                "env-config-invalid",
                f"environment name {self.name!r} is not a DNS label",
            )
        if not _is_label(self.namespace):
            raise EnvError(
                "env-config-invalid",
                f"environment {self.name!r}: namespace is not a DNS label",
            )
        self._check_composition()
        if isinstance(self.follow, Mapping):
            pairs = _source_follow(
                self.follow, f"environment {self.name!r}", per_branch=False
            )
            object.__setattr__(self, "sources", pairs)
            object.__setattr__(
                self,
                "follow",
                tuple(dict.fromkeys(item for _, items in pairs for item in items)),
            )
        follow = (
            (self.follow,)
            if isinstance(self.follow, Branch | Tag | Promote)
            else self.follow
        )
        if (
            isinstance(follow, str)
            or not isinstance(follow, Sequence)
            or not follow
            or not all(isinstance(item, Branch | Tag | Promote) for item in follow)
        ):
            raise EnvError(
                "env-config-invalid",
                f"environment {self.name!r}: follow takes Branch(...), Tag(...) "
                "or Promote(), or a list of them",
            )
        object.__setattr__(self, "follow", tuple(dict.fromkeys(follow)))
        if self.stack is not None and not isinstance(self.stack, Stack):
            raise EnvError("env-config-invalid", "stack must be a Stack")
        object.__setattr__(
            self,
            "on_nodes",
            _nodes(self.on_nodes, f"environment {self.name!r} on_nodes"),
        )
        object.__setattr__(
            self, "quota", _quota(self.quota, f"environment {self.name!r} quota")
        )
        if not isinstance(self.auto_approve, bool):
            raise EnvError("env-config-invalid", "auto_approve is True or False")

    def _check_composition(self) -> None:
        what = f"environment {self.name!r}"
        if self.cluster is not None:
            from piceli.infra import Cluster

            if not isinstance(self.cluster, Cluster):
                raise EnvError("env-config-invalid", f"{what}: cluster is a Cluster")
        object.__setattr__(self, "secrets", _secrets(self.secrets, f"{what} secrets"))
        object.__setattr__(
            self, "settings", _settings(self.settings, f"{what} settings")
        )
        _check_pipeline(self, what)

    @classmethod
    def per_branch(
        cls,
        branches: Branches | str | Sequence[str],
        *,
        namespace: str,
        follow: Mapping[Any, Any],
        stack: Stack | None = None,
        cluster: Any = None,
        on_nodes: Any = None,
        quota: Mapping[str, str] | None = None,
        limit: int = 3,
        idle_stop: str | int | None = None,
        claim_sizes: Mapping[str, str] | None = None,
        auto_approve: bool = False,
        secrets: Sequence[str] = (),
        settings: Mapping[str, Mapping[str, str]] | None = None,
        allow_egress: Sequence[str] = (),
        name: str = "branches",
        pipeline: Any = None,
    ) -> BranchEnvironments:
        """One environment per Git branch of the sources (a composition's branch rule).

        :param branches: :class:`Branches` (or globs): the branches that get an
            environment, and the fallback branch of sources that lack one.
        :param namespace: ``"<prefix>{branch}"``: the branch's namespace.
        :param follow: ``{Source: "{branch}" | "main" | Tag(...)}``; at least
            one source follows ``"{branch}"`` (its branches create the
            environments).
        :param limit: At most this many branch environments run
            (``EnvConfig(max_envs=...)``).
        :param pipeline: Deploy this :class:`piceli.Pipeline`'s app instead of
            contract components (``stack`` then names its workloads).

        Every other parameter is the :class:`EnvConfig` one of the same name
        (``on_nodes`` is ``branch_nodes``, ``stack`` is ``branch_stack``).
        A branch environment is removed with its branch.
        """
        return BranchEnvironments(
            name=name,
            branches=branches if isinstance(branches, Branches) else Branches(branches),
            namespace=namespace,
            follow=follow,
            stack=stack,
            cluster=cluster,
            on_nodes=on_nodes,
            quota=quota,
            limit=limit,
            idle_stop=idle_stop,
            claim_sizes=dict(claim_sizes or {}),
            auto_approve=auto_approve,
            secrets=tuple(secrets),
            settings=dict(settings or {}),
            allow_egress=tuple(allow_egress),
            pipeline=pipeline,
        )

    @property
    def branches(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.follow if isinstance(item, Branch))

    @property
    def tags(self) -> tuple[str, ...]:
        return tuple(item.pattern for item in self.follow if isinstance(item, Tag))

    @property
    def promote(self) -> bool:
        return any(isinstance(item, Promote) for item in self.follow)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "namespace": self.namespace,
            "follow": [item.describe() for item in self.follow],
            "stack": None if self.stack is None else self.stack.describe(),
            "on_nodes": _describe_nodes(self.on_nodes),
            "quota": None if self.quota is None else dict(self.quota),
            "auto_approve": self.auto_approve,
            # Composition fields: only when declared, so 0.14 hashes stay equal.
            **(
                {
                    "sources": {
                        source.key: [_describe_follow(item) for item in items]
                        for source, items in self.sources
                    }
                }
                if self.sources
                else {}
            ),
            **({"cluster": self.cluster.name} if self.cluster is not None else {}),
            **({"secrets": list(self.secrets)} if self.secrets else {}),
            **({"settings": dict(self.settings)} if self.settings else {}),
            **({"pipeline": self.pipeline.name} if self.pipeline is not None else {}),
        }


def _check_pipeline(env: Any, what: str) -> None:
    """``pipeline=``: a Pipeline, never mixed with contract components."""
    if env.pipeline is None:
        return
    from piceli.pipeline.model import Pipeline

    if not isinstance(env.pipeline, Pipeline):
        raise EnvError("env-config-invalid", f"{what}: pipeline is a piceli Pipeline")
    if env.stack is not None:
        from piceli.infra import Component

        if any(isinstance(item, Component) for item in env.stack.declared):
            raise EnvError(
                "env-config-invalid",
                f"{what}: deploys a pipeline; its stack names the pipeline's "
                "workloads, not Components (declare the components in another "
                "environment)",
            )
    if env.settings or env.secrets:
        raise EnvError(
            "env-config-invalid",
            f"{what}: settings= and secrets= configure contract components; a "
            "pipeline environment takes them from its Pipeline",
        )


@dataclass(frozen=True)
class BranchEnvironments:
    """A composition's branch rule (built by :meth:`Environment.per_branch`)."""

    name: str
    branches: Branches
    namespace: str
    follow: Mapping[Any, Any]
    stack: Stack | None = None
    cluster: Any = None
    on_nodes: Any = None
    quota: Mapping[str, str] | None = None
    limit: int = 3
    idle_stop: str | int | None = None
    claim_sizes: Mapping[str, str] = field(default_factory=dict)
    auto_approve: bool = False
    secrets: Sequence[str] = ()
    settings: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    allow_egress: Sequence[str] = ()
    pipeline: Any = field(default=None, compare=False)
    sources: tuple[tuple[Any, tuple[Any, ...]], ...] = field(
        init=False, default=(), compare=False, repr=False
    )

    def __post_init__(self) -> None:
        what = f"branch environments {self.name!r}"
        if not _is_label(self.name):
            raise EnvError("env-config-invalid", f"{what}: the name is no DNS label")
        prefix, sep, rest = self.namespace.partition(BRANCH)
        if not sep or rest or not _PREFIX.fullmatch(prefix):
            raise EnvError(
                "env-config-invalid",
                f'{what}: namespace must be "<prefix>{{branch}}" with a lowercase '
                "prefix of at most 40 characters",
            )
        if not isinstance(self.follow, Mapping):
            raise EnvError(
                "env-config-invalid", f"{what}: follow maps each Source to its rule"
            )
        pairs = _source_follow(self.follow, what, per_branch=True)
        if not any(BRANCH in items for _, items in pairs):
            raise EnvError(
                "env-config-invalid",
                f'{what}: at least one source follows "{{branch}}"',
            )
        object.__setattr__(self, "sources", pairs)
        if self.stack is not None and not isinstance(self.stack, Stack):
            raise EnvError("env-config-invalid", f"{what}: stack must be a Stack")
        object.__setattr__(self, "secrets", _secrets(self.secrets, f"{what} secrets"))
        object.__setattr__(
            self, "settings", _settings(self.settings, f"{what} settings")
        )
        if self.cluster is not None:
            from piceli.infra import Cluster

            if not isinstance(self.cluster, Cluster):
                raise EnvError("env-config-invalid", f"{what}: cluster is a Cluster")
        _check_pipeline(self, what)
        # The EnvConfig checks every remaining field (quota, sizes, idle stop).
        self.env_config()

    @property
    def prefix(self) -> str:
        return self.namespace.partition(BRANCH)[0]

    def env_config(self) -> EnvConfig:
        """The :class:`EnvConfig` of these branch environments."""
        return EnvConfig(
            prefix=self.prefix,
            main_branch=self.branches.fallback,
            branches=self.branches.patterns,
            max_envs=self.limit,
            quota=self.quota,
            claim_sizes=dict(self.claim_sizes),
            auto_approve=self.auto_approve,
            allow_egress=tuple(self.allow_egress),
            branch_nodes=self.on_nodes,
            idle_stop=self.idle_stop,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "branches": list(self.branches.patterns),
            "fallback": self.branches.fallback,
            "namespace": self.namespace,
            "sources": {
                source.key: [_describe_follow(item) for item in items]
                for source, items in self.sources
            },
            "stack": None if self.stack is None else self.stack.describe(),
            "cluster": None if self.cluster is None else self.cluster.name,
            "env_config": self.env_config().describe(),
            "secrets": list(self.secrets),
            "settings": dict(self.settings),
            **({"pipeline": self.pipeline.name} if self.pipeline is not None else {}),
        }


def _describe_nodes(value: Any) -> Any:
    if value is None:
        return None
    return dict(value) if isinstance(value, Mapping) else list(value)


@dataclass(frozen=True)
class EnvConfig:
    """One namespace per Git branch of a pipeline (``Pipeline(envs=EnvConfig(...))``).

    :param prefix: Namespace prefix of branch environments (``"shop-"`` gives
        ``shop-feature-x``); lowercase, starts with a letter, at most 40
        characters.
    :param main_namespace: The namespace of the main branch; default the
        pipeline target's namespace. Never deleted, never stopped.
    :param main_branch: The branch deployed to ``main_namespace``.
    :param branches: Glob patterns of the branches that may have an
        environment (``["main", "wp-*"]``); the main branch always may.
    :param max_envs: At most this many branch environments run; on one more,
        the least recently pushed one is stopped (scaled to zero, kept).
    :param quota: ``ResourceQuota`` ``spec.hard`` of each branch environment
        (default: :data:`DEFAULT_QUOTA`, counts only). ``requests.cpu`` or
        ``requests.memory`` need every pod to declare requests.
    :param claim_sizes: Storage request of claims in branch environments:
        ``{"db": "1Gi"}`` (every claim of workload ``db``), ``{"db/data":
        "1Gi"}`` (claim template ``data`` of ``db``) or a claim's name.
    :param seed_from: Seed a *new* branch environment's claims from this
        environment's latest restore point (``"main"``); default empty claims.
    :param auto_approve: The owner allows ``env up``, ``env down`` and
        ``env seed`` of *branch* environments without a human hash (with
        ``--approve-if-policy``). The main branch is never approved by it.
    :param allow_egress: CIDRs branch pods may reach besides their own
        namespace and the cluster DNS (``["0.0.0.0/0"]`` for the internet).
    :param environments: Named, long-lived :class:`Environment` s, each with
        its own trigger (``follow``), namespace, stack and placement. One
        named like ``main_branch`` replaces the implicit main environment
        (``main_namespace``, deployed on tags and promotions).
    :param branch_stack: Branch environments render only this :class:`Stack`.
    :param branch_nodes: Branch environments run on these nodes (names, or a
        ``{label: value}`` selector).
    :param idle_stop: The GitOps controller scales a branch environment to
        zero after no push for this long (``"24h"``); the next push starts it
        again. Default: never.

    Example::

        envs = EnvConfig(prefix="shop-", branches=["main", "wp-*"], max_envs=3)
        assert envs.namespace_for("wp-login") == "shop-wp-login"
    """

    prefix: str
    main_namespace: str | None = None
    main_branch: str = "main"
    branches: Sequence[str] = ("*",)
    max_envs: int = 3
    quota: Mapping[str, str] | None = None
    claim_sizes: Mapping[str, str] = field(default_factory=dict)
    seed_from: str | None = None
    auto_approve: bool = False
    allow_egress: Sequence[str] = ()
    environments: Sequence[Environment] = ()
    branch_stack: Stack | None = None
    branch_nodes: Any = None
    idle_stop: str | int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.prefix, str) or not _PREFIX.fullmatch(self.prefix):
            raise EnvError(
                "env-config-invalid",
                "EnvConfig prefix must be lowercase letters, digits and '-', "
                "start with a letter and have at most 40 characters",
            )
        if self.main_namespace is not None and not _is_label(self.main_namespace):
            raise EnvError("env-config-invalid", "main_namespace is not a DNS label")
        if isinstance(self.branches, str):
            raise EnvError("env-config-invalid", "branches must be a list of globs")
        object.__setattr__(self, "branches", tuple(self.branches))
        if not all(isinstance(item, str) and item for item in self.branches):
            raise EnvError("env-config-invalid", "branches must be non-empty globs")
        if not isinstance(self.main_branch, str) or not self.main_branch:
            raise EnvError("env-config-invalid", "main_branch is required")
        if (
            not isinstance(self.max_envs, int)
            or isinstance(self.max_envs, bool)
            or not 1 <= self.max_envs <= 100
        ):
            raise EnvError("env-config-invalid", "max_envs must be between 1 and 100")
        quota = dict(DEFAULT_QUOTA if self.quota is None else self.quota)
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in quota.items()):
            raise EnvError("env-config-invalid", "quota maps resource names to strings")
        object.__setattr__(self, "quota", dict(sorted(quota.items())))
        sizes = dict(self.claim_sizes)
        for key, value in sizes.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise EnvError("env-config-invalid", "claim_sizes maps names to sizes")
            if not _SIZE.fullmatch(value):
                raise EnvError(
                    "env-config-invalid",
                    f"claim size {value!r} of {key!r} is not a quantity (1Gi, 500Mi)",
                )
        object.__setattr__(self, "claim_sizes", dict(sorted(sizes.items())))
        if isinstance(self.allow_egress, str):
            raise EnvError("env-config-invalid", "allow_egress must be a list of CIDRs")
        object.__setattr__(self, "allow_egress", tuple(self.allow_egress))
        if self.seed_from is not None and not isinstance(self.seed_from, str):
            raise EnvError("env-config-invalid", "seed_from must be a branch name")
        self._check_environments()
        if self.branch_stack is not None and not isinstance(self.branch_stack, Stack):
            raise EnvError("env-config-invalid", "branch_stack must be a Stack")
        object.__setattr__(
            self, "branch_nodes", _nodes(self.branch_nodes, "branch_nodes")
        )
        if self.idle_stop is not None:
            parse_idle(self.idle_stop)

    def _check_environments(self) -> None:
        if isinstance(self.environments, str | Environment) or not isinstance(
            self.environments, Sequence
        ):
            raise EnvError("env-config-invalid", "environments must be a list")
        items = tuple(self.environments)
        if not all(isinstance(item, Environment) for item in items):
            raise EnvError(
                "env-config-invalid", "environments holds Environment(...) items"
            )
        names = [item.name for item in items]
        spaces = [item.namespace for item in items]
        for what, values in (("name", names), ("namespace", spaces)):
            twice = sorted({value for value in values if values.count(value) > 1})
            if twice:
                raise EnvError(
                    "env-config-invalid",
                    f"two environments share the {what} {twice[0]!r}",
                )
        if (
            self.main_namespace is not None
            and self.named(self.main_branch) is None
            and self.main_namespace in spaces
        ):
            raise EnvError(
                "env-config-invalid",
                f"environment namespace {self.main_namespace!r} is the main namespace",
            )
        object.__setattr__(self, "environments", items)

    # ------------------------------------------------------------ named envs
    def named(self, name: str) -> Environment | None:
        """The declared :class:`Environment` called ``name``, if any."""
        return next((item for item in self.environments if item.name == name), None)

    def is_fixed(self, name: str) -> bool:
        """Whether ``name`` is a named environment or the main branch (never a branch env)."""
        return self.is_main(name) or self.named(name) is not None

    @property
    def idle_stop_seconds(self) -> int | None:
        """``idle_stop`` in seconds (``None``: never)."""
        return None if self.idle_stop is None else parse_idle(self.idle_stop)

    # ------------------------------------------------------------ mapping
    def is_main(self, branch: str) -> bool:
        return branch == self.main_branch

    def allows(self, branch: str) -> bool:
        """Whether ``branch`` may have an environment (the main branch always may)."""
        return self.is_fixed(branch) or any(
            fnmatch.fnmatchcase(branch, pattern) for pattern in self.branches
        )

    def namespace_for(self, branch: str, main_namespace: str | None = None) -> str:
        """The namespace of ``branch``: ``<prefix><slug>``, or the main namespace.

        At most 63 characters; a longer name is truncated and ends with ``-``
        and 8 hex of the branch name's SHA-256.

        :raises EnvError: ``env-branch-invalid`` for a branch with no letter
            or digit; ``env-config-invalid`` for the main branch when no main
            namespace is known.
        """
        if not isinstance(branch, str) or not branch or len(branch) > 250:
            raise EnvError("env-branch-invalid", "the branch name is empty or too long")
        fixed = self.named(branch)
        if fixed is not None:
            return fixed.namespace
        if self.is_main(branch):
            namespace = self.main_namespace or main_namespace
            if namespace is None:
                raise EnvError(
                    "env-config-invalid",
                    "the main branch's namespace is unknown: set main_namespace",
                )
            return namespace
        part = slug(branch)
        if not part:
            raise EnvError(
                "env-branch-invalid",
                f"branch {branch!r} has no letter or digit to name a namespace",
            )
        name = self.prefix + part
        if len(name) > _MAX:
            digest = hashlib.sha256(branch.encode()).hexdigest()[:_HASH]
            name = name[: _MAX - _HASH - 1].rstrip("-") + "-" + digest
        taken = {item.namespace for item in self.environments}
        if self.main_namespace is not None:
            taken.add(self.main_namespace)
        if main_namespace is not None and self.named(self.main_branch) is None:
            taken.add(main_namespace)
        if name in taken:
            raise EnvError(
                "env-namespace-collision",
                f"branch {branch!r} maps to namespace {name}, which a named "
                "environment uses; rename the branch",
            )
        return name

    def describe(self) -> dict[str, Any]:
        return {
            "prefix": self.prefix,
            "main_namespace": self.main_namespace,
            "main_branch": self.main_branch,
            "branches": list(self.branches),
            "max_envs": self.max_envs,
            "quota": dict(self.quota or {}),
            "claim_sizes": dict(self.claim_sizes),
            "seed_from": self.seed_from,
            "auto_approve": self.auto_approve,
            "allow_egress": list(self.allow_egress),
            # 0.14 fields: only when declared, so 0.13 descriptions stay equal.
            **(
                {"environments": [item.describe() for item in self.environments]}
                if self.environments
                else {}
            ),
            **(
                {"branch_stack": self.branch_stack.describe()}
                if self.branch_stack is not None
                else {}
            ),
            **(
                {"branch_nodes": _describe_nodes(self.branch_nodes)}
                if self.branch_nodes is not None
                else {}
            ),
            **(
                {"idle_stop_seconds": self.idle_stop_seconds}
                if self.idle_stop is not None
                else {}
            ),
        }


def _is_label(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= _MAX
        and bool(_DNS_LABEL.fullmatch(value))
    )


@dataclass(frozen=True)
class BranchEnv:
    """How one environment's pipeline renders (attached as ``pipeline.branch_env``).

    :param branch: The branch name.
    :param namespace: Its namespace.
    :param config: The pipeline's :class:`EnvConfig`.
    :param main_namespace: The main branch's namespace.
    :param isolated: Apply the branch isolation (``False`` for the main branch).
    :param images: Build image name → pinned reference (prebuilt images).
    :param pin_node: Node that workloads using a prebuilt image run on
        (node-loopback delivery), unless they choose their node.
    :param mirrors: Canonical source → mirrored reference of the delivery.
    """

    branch: str
    namespace: str
    config: EnvConfig
    main_namespace: str
    isolated: bool = True
    images: Mapping[str, str] = field(default_factory=dict)
    pin_node: str | None = None
    mirrors: Mapping[str, str] = field(default_factory=dict)
    #: Render only this stack of the app (``Environment.stack``, ``branch_stack``).
    stack: Stack | None = None
    #: Node names or a label selector for every workload.
    on_nodes: Any = None
    #: A ResourceQuota of a named environment (branch envs use ``config.quota``).
    quota: Mapping[str, str] | None = None
    #: Images are given (``False``: a named environment that builds itself).
    prebuilt: bool = True
    #: The named environment's name (``None`` for main and branches).
    environment: str | None = None


@dataclass(frozen=True)
class EnvStatus:
    """One environment as ``piceli envs`` shows it.

    ``state`` is ``running``, ``stopped`` (scaled to zero by the budget or
    ``idle_stop``) or
    ``absent`` (the main branch before its first deploy); ``health`` is
    ``healthy``, ``degraded``, ``stopped`` or ``unknown``. ``gitops`` is the
    controller's entry for the branch (wanted and deployed commit, state,
    plan hash, reason) when a GitOps controller publishes its status.
    """

    branch: str
    namespace: str
    main: bool
    state: str
    health: str
    commit: str | None = None
    build: str | None = None
    deploy: str | None = None
    created_at: str | None = None
    pushed_at: str | None = None
    age_seconds: int | None = None
    workloads: Sequence[Mapping[str, Any]] = ()
    #: The GitOps controller's view of the branch (``piceli gitops``), if any.
    gitops: Mapping[str, Any] | None = None
    #: A named environment (``EnvConfig(environments=[...])``); ``branch``
    #: is then its name.
    fixed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "namespace": self.namespace,
            "main": self.main,
            "state": self.state,
            "health": self.health,
            "commit": self.commit,
            "build": self.build,
            "deploy": self.deploy,
            "created_at": self.created_at,
            "pushed_at": self.pushed_at,
            "age_seconds": self.age_seconds,
            "workloads": [dict(item) for item in self.workloads],
            "gitops": None if self.gitops is None else dict(self.gitops),
            "fixed": self.fixed,
        }
