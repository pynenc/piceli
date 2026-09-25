"""The state scope of a pipeline or a ``release.toml`` spec.

Importing this module is side-effect free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from piceli.state.backend import StateScope, StateSettings

if TYPE_CHECKING:
    from piceli.k8s.ops.provider_factory import KubeconfigTarget
    from piceli.k8s.release_spec import ReleaseSpec
    from piceli.pipeline.model import Pipeline, Target


def kubeconfig_target(target: Target) -> KubeconfigTarget:
    """The explicit ``KubeconfigTarget`` of a pipeline's :class:`Target`."""
    from piceli.k8s.ops.provider_factory import KubeconfigTarget

    return KubeconfigTarget(
        kubeconfig=target.kubeconfig,
        context=target.context,
        namespace=target.namespace,
        cluster_uid=target.cluster_uid,
        namespace_uid=target.namespace_uid,
        transport=target.transport,  # type: ignore[arg-type]
        request_seconds=target.request_seconds,
        allow_exec=target.allow_exec,
        exec_sha256=target.exec_sha256,
        exec_pass_env=tuple(target.exec_pass_env),
        exec_timeout_seconds=60.0
        if target.exec_timeout_seconds is None
        else float(target.exec_timeout_seconds),
    )


def pipeline_scope(pipeline: Pipeline) -> StateScope:
    """The state of ``piceli deploy``: the whole state directory, one lock per app."""
    return StateScope(
        directory=pipeline.state_dir,
        name=pipeline.app.name,
        namespace=pipeline.target.namespace,
        layout="pipeline",
        settings=pipeline.state,
        target=kubeconfig_target(pipeline.target),
        lock_code="pipeline-locked",
    )


def release_scope(spec: ReleaseSpec) -> StateScope:
    """The state of a ``release.toml`` spec (its ``state_dir``, one lock per release)."""
    release = spec.model.release
    return StateScope(
        directory=spec.state_dir,
        name=release.name,
        namespace=spec.model.target.namespace,
        layout="release",
        settings=StateSettings(release.state, release.state_lease_seconds),
        target=spec.kubeconfig_target(),
        lock_code="release-locked",
    )


def scope_of(value: Any) -> StateScope:
    """:func:`pipeline_scope` or :func:`release_scope`, by type."""
    from piceli.pipeline.model import Pipeline

    if isinstance(value, Pipeline):
        return pipeline_scope(value)
    return release_scope(value)
