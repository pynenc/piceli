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

_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_PREFIX = re.compile(r"[a-z]([-a-z0-9]{0,38}[-a-z0-9])?")
_SIZE = re.compile(r"[0-9]+(\.[0-9]+)?([KMGTPE]i?)?")
_HASH = 8
_MAX = 63

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

    # ------------------------------------------------------------ mapping
    def is_main(self, branch: str) -> bool:
        return branch == self.main_branch

    def allows(self, branch: str) -> bool:
        """Whether ``branch`` may have an environment (the main branch always may)."""
        return self.is_main(branch) or any(
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


@dataclass(frozen=True)
class EnvStatus:
    """One environment as ``piceli envs`` shows it.

    ``state`` is ``running``, ``stopped`` (scaled to zero by the budget) or
    ``absent`` (the main branch before its first deploy); ``health`` is
    ``healthy``, ``degraded``, ``stopped`` or ``unknown``.
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
        }
