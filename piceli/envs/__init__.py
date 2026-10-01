"""Per-branch environments: one namespace per Git branch of a pipeline.

Maturity: **preview** (the API may change before 1.0).

Declare them on the pipeline::

    pipeline = Pipeline(
        app, target, build=images, deliver=NodeLoopbackRegistry(),
        envs=EnvConfig(prefix="shop-", branches=["main", "wp-*"], max_envs=3),
    )

then ``piceli env up wp-login --digest api=…`` (or :func:`env_up`) deploys
branch ``wp-login`` into namespace ``shop-wp-login``, isolated at render time
(:mod:`piceli.envs.isolation`), with a ResourceQuota and a default-deny
NetworkPolicy across namespaces. :func:`env_down` deletes it with its claims
and volumes (never main's), :func:`seed_env` restores main's latest restore
point into its claims and :func:`list_envs` answers what runs where and
whether it is healthy. :class:`Environment` adds named, long-lived
environments with their own trigger (:class:`Branch`, :class:`Tag`,
:class:`Promote`), :class:`Stack` and placement; in a composition
(:mod:`piceli.infra`) ``follow`` maps each source to its rule and
:meth:`Environment.per_branch` declares the branch environments. See
``docs/environments.md``.

Importing this package reads no file and contacts nothing.
"""

from typing import TYPE_CHECKING, Any

from piceli.envs.model import (
    Branch,
    BranchEnv,
    BranchEnvironments,
    Branches,
    EnvConfig,
    EnvError,
    Environment,
    EnvStatus,
    Promote,
    Stack,
    Tag,
)

if TYPE_CHECKING:
    from piceli.envs.ops import (
        env_down,
        env_pipeline,
        env_stop,
        env_up,
        list_envs,
        namespace_for,
        seed_env,
    )

__all__ = [
    "Branch",
    "BranchEnv",
    "BranchEnvironments",
    "Branches",
    "EnvConfig",
    "EnvError",
    "EnvStatus",
    "Environment",
    "Promote",
    "Stack",
    "Tag",
    "env_down",
    "env_pipeline",
    "env_stop",
    "env_up",
    "list_envs",
    "namespace_for",
    "seed_env",
]

_OPS = {
    "env_down",
    "env_pipeline",
    "env_stop",
    "env_up",
    "list_envs",
    "namespace_for",
    "seed_env",
}


def __getattr__(name: str) -> Any:
    if name in _OPS:
        from piceli.envs import ops

        return getattr(ops, name)
    raise AttributeError(f"module 'piceli.envs' has no attribute {name!r}")
