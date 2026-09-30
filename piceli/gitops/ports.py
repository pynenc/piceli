"""What the GitOps controller needs from the rest of Piceli, as one small port.

The controller (:mod:`piceli.gitops.controller`) decides *when* a branch
deploys; the work itself belongs to other modules:

- loading the ``Pipeline`` of a commit (:func:`piceli.app.render.load_target`),
  pointed at the controller's own kubeconfig and state directory;
- building its images in the cluster
  (:func:`piceli.artifacts.cluster_build.run_build_job`);
- per-branch environments (:mod:`piceli.envs`: ``env_up``, ``env_down``,
  ``namespace_for``).

:class:`Ports` is the protocol; :class:`DefaultPorts` adapts the real modules
(imported lazily, when the controller runs) and tests pass fakes. Every
adapter returns plain data, so the controller never depends on the other
modules' types.

Importing this module is side-effect free.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from piceli.gitops import GitOpsError

#: ``approve`` values the controller passes to :meth:`Ports.env_up`.
APPROVE_POLICY = "policy"


@dataclass(frozen=True)
class EnvOutcome:
    """What one ``env_up`` did.

    :param state: ``deployed``, ``approval-required`` or ``failed``.
    :param plan_hash: The combined plan hash (always set for
        ``approval-required``; the hash ``piceli gitops approve`` must name).
    :param namespace: The environment's namespace.
    :param reason: A registered error code when it did not deploy.
    """

    state: str
    plan_hash: str | None = None
    namespace: str | None = None
    reason: str | None = None

    @classmethod
    def from_result(cls, value: Any) -> EnvOutcome:
        """Normalise an ``env_up`` result (a mapping or an object)."""
        if isinstance(value, EnvOutcome):
            return value

        def get(*names: str) -> Any:
            for name in names:
                found = (
                    value.get(name)
                    if isinstance(value, Mapping)
                    else getattr(value, name, None)
                )
                if found is not None:
                    return found
            return None

        state = str(get("state") or "failed")
        if state in {"ready", "deployed", "applied", "unchanged"}:
            state = "deployed"
        elif state not in {"approval-required", "failed"}:
            state = "failed"
        return cls(
            state=state,
            plan_hash=get("plan_hash", "combined_hash"),
            namespace=get("namespace"),
            reason=get("reason"),
        )


class Ports(Protocol):
    """The controller's dependencies (see the module docstring)."""

    def load_pipeline(self, checkout: Path, entry: str, env: str | None) -> Any:
        """The ``Pipeline`` declared at ``entry`` in the ``checkout``."""
        ...

    def build(
        self,
        pipeline: Any,
        commit: str,
        *,
        cache_key: str,
        platforms: Sequence[str],
    ) -> Mapping[str, Any]:
        """Build the pipeline's images for ``commit``; a build receipt."""
        ...

    def prepare_env(self, pipeline: Any, branch: str) -> str | None:
        """Grant the controller access to the branch's namespace; its name."""
        ...

    def env_up(
        self,
        pipeline: Any,
        branch: str,
        *,
        commit: str,
        receipt: Mapping[str, Any] | None,
        digests: Mapping[str, str] | None,
        approve: str | None,
    ) -> EnvOutcome:
        """Deploy ``commit`` to the branch environment.

        ``approve`` is ``None`` (plan only: ``approval-required`` with the
        hash), :data:`APPROVE_POLICY` (apply when the plan is inside the
        pipeline's ``auto_approve`` policy) or a combined plan hash.
        """
        ...

    def env_down(self, pipeline: Any, branch: str) -> None:
        """Remove the branch environment (never main's)."""
        ...


def retarget(pipeline: Any, kubeconfig: Path, context: str, state_dir: Path) -> Any:
    """``pipeline`` reached through the controller's kubeconfig and state.

    A pipeline declares the kubeconfig of its author's machine; inside the
    cluster the controller reaches the same cluster with its own service
    account. The namespace, node and UID pins stay as declared.
    """
    from piceli.pipeline.model import Target

    declared = pipeline.target
    selected = copy.copy(pipeline)
    selected._target = Target(
        kubeconfig,
        context=context,
        namespace=declared.namespace,
        cluster_uid=declared.cluster_uid,
        namespace_uid=declared.namespace_uid,
        nodes=declared.nodes,
        transport=declared.transport,
        request_seconds=declared.request_seconds,
    )
    selected.state_dir = state_dir
    return selected


class DefaultPorts:
    """The real adapters: load, build in the cluster, environments.

    :param kubeconfig: The controller's kubeconfig (explicit file).
    :param context: Its context.
    :param state_dir: Where pipelines keep their local state (the volume).
    :param namespace: The controller's namespace (for access grants).
    """

    def __init__(
        self,
        kubeconfig: Path,
        context: str,
        state_dir: Path,
        *,
        namespace: str,
        transport: str = "https",
    ) -> None:
        self.kubeconfig = kubeconfig
        self.context = context
        self.state_dir = state_dir
        self.namespace = namespace
        self.transport = transport

    def load_pipeline(self, checkout: Path, entry: str, env: str | None) -> Any:
        from piceli.app.render import RenderError, load_target
        from piceli.pipeline import Pipeline, PipelineError

        try:
            value = load_target(entry, checkout)
        except (RenderError, PipelineError) as error:
            raise GitOpsError(
                "gitops-pipeline-invalid", f"cannot load {entry}: {error}"
            ) from None
        except Exception as error:  # the module raised while importing
            raise GitOpsError(
                "gitops-pipeline-invalid",
                f"importing {entry} failed: {type(error).__name__}",
            ) from None
        if not isinstance(value, Pipeline):
            raise GitOpsError("gitops-pipeline-invalid", f"{entry} is not a Pipeline")
        if env is not None:
            value = value.for_environment(env)
        return retarget(
            value, self.kubeconfig, self.context, self.state_dir / "pipelines"
        )

    def build(
        self,
        pipeline: Any,
        commit: str,
        *,
        cache_key: str,
        platforms: Sequence[str],
    ) -> Mapping[str, Any]:
        try:
            from piceli.artifacts.cluster_build import (  # type: ignore[import-not-found,unused-ignore]
                run_build_job,
            )
        except ImportError:
            raise GitOpsError(
                "gitops-port-unavailable", "cluster builds are not available"
            ) from None
        receipt = run_build_job(
            pipeline, commit, cache_key=cache_key, platforms=tuple(platforms)
        )
        return dict(receipt) if isinstance(receipt, Mapping) else _as_dict(receipt)

    def _envs(self) -> Any:
        try:
            import piceli.envs as envs  # type: ignore[import-not-found,unused-ignore]
        except ImportError:
            raise GitOpsError(
                "gitops-port-unavailable", "branch environments are not available"
            ) from None
        return envs

    def prepare_env(self, pipeline: Any, branch: str) -> str | None:
        from piceli.gitops.install import grant_env_access

        namespace = str(self._envs().namespace_for(pipeline, branch))
        grant_env_access(
            self.kubeconfig,
            self.context,
            namespace,
            controller_namespace=self.namespace,
            branch=branch,
            transport=self.transport,
        )
        return namespace

    def env_up(
        self,
        pipeline: Any,
        branch: str,
        *,
        commit: str,
        receipt: Mapping[str, Any] | None,
        digests: Mapping[str, str] | None,
        approve: str | None,
    ) -> EnvOutcome:
        kwargs: dict[str, Any] = {"commit": commit, "approve": approve}
        if digests is not None:
            kwargs["digests"] = dict(digests)
        else:
            kwargs["build_receipt"] = receipt
        return EnvOutcome.from_result(self._envs().env_up(pipeline, branch, **kwargs))

    def env_down(self, pipeline: Any, branch: str) -> None:
        self._envs().env_down(pipeline, branch)


def _as_dict(value: Any) -> dict[str, Any]:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    raise GitOpsError("gitops-step-failed", "the build returned no receipt")
