"""What the GitOps controller needs from the rest of Piceli, as one small port.

The controller (:mod:`piceli.gitops.controller`) decides *when* a branch
deploys; the work itself belongs to other modules:

- loading the ``Pipeline`` of a commit (:func:`piceli.app.render.load_target`),
  pointed at the controller's own kubeconfig and state directory;
- building its images in the cluster
  (:func:`piceli.artifacts.cluster_build.run_build_job`), unless a laptop
  pushed the images for that commit (``piceli env push``: the ConfigMap
  ``piceli-env-<branch slug>`` in the environment's namespace);
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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from piceli.gitops import GitOpsError
from piceli.gitops.config import ControllerConfig

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
        checkout: Path,
    ) -> Mapping[str, Any]:
        """Build the pipeline's images for ``commit``; a build receipt.

        ``cache_key`` is the branch (one build cache per branch), ``checkout``
        the commit's working tree (the repository root).
        """
        ...

    def pushed_images(
        self, pipeline: Any, branch: str, namespace: str
    ) -> Mapping[str, Any] | None:
        """Images pushed from a laptop for the branch (``piceli env push``).

        ``{"commit": sha or None, "images": {name: {"digest": …,
        "pull_ref"?: …}}}`` or ``None``.
        """
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
        digests: Mapping[str, Any] | None,
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

    def env_stop(self, pipeline: Any, branch: str) -> None:
        """Scale an idle branch environment to zero (``EnvConfig(idle_stop=...)``)."""
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
        config: ControllerConfig,
        transport: str = "https",
        log: Callable[[str], None] = lambda _: None,
    ) -> None:
        self.kubeconfig = kubeconfig
        self.context = context
        self.state_dir = state_dir
        self.namespace = namespace
        self.config = config
        self.transport = transport
        self.log = log

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
        checkout: Path,
    ) -> Mapping[str, Any]:
        from piceli.artifacts.cluster_build import (
            DEFAULT_PLATFORMS,
            ClusterBuildConfig,
            run_build_job,
        )
        from piceli.pipeline import PipelineError

        config = self.config
        if config.builder_image is None:
            raise GitOpsError(
                "gitops-config-invalid",
                "no --builder-image: this controller deploys only images pushed "
                "with piceli env push",
            )
        settings: dict[str, Any] = {
            "image": config.builder_image,
            "repo": config.repo,
            "namespace": self.namespace,
            "storage": config.build_storage,
            "repo_root": checkout,
            "registry_url": config.build_registry,
            "node_registry": config.node_registry,
        }
        if config.build_git_secret:
            settings["git_secret"] = config.build_git_secret
        if config.builder_selector:
            settings["selector"] = dict(config.builder_selector)
        try:
            receipt = run_build_job(
                pipeline,
                commit,
                cache_key=cache_key,
                platforms=tuple(platforms) or DEFAULT_PLATFORMS,
                config=ClusterBuildConfig(**settings),
                approve=None,  # the controller applies the owner's settings
                say=self.log,
            )
        except PipelineError as error:
            raise GitOpsError(error.code, str(error)) from None
        return dict(receipt)

    def pushed_images(
        self, pipeline: Any, branch: str, namespace: str
    ) -> Mapping[str, Any] | None:
        import json

        from piceli.gitops.install import connect
        from piceli.k8s.cli.env_push import configmap_name

        name = configmap_name(branch)
        with connect(self.kubeconfig, self.context, transport=self.transport) as api:
            found = api.call(f"/api/v1/namespaces/{namespace}/configmaps/{name}", "GET")
        data = (found or {}).get("data") or {} if isinstance(found, dict) else {}
        try:
            images = json.loads(data.get("images") or "null")
        except ValueError:
            return None
        if not isinstance(images, dict) or not images:
            return None
        return {"commit": data.get("commit"), "images": images}

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
        digests: Mapping[str, Any] | None,
        approve: str | None,
    ) -> EnvOutcome:
        kwargs: dict[str, Any] = {"commit": commit, "approve": approve}
        # "policy": the owner's ApprovalPolicy, or EnvConfig(auto_approve=True)
        # for a branch; also without a policy when the owner allows branches.
        envs = getattr(pipeline, "envs", None)
        by_config = bool(
            envs is not None and envs.auto_approve and not envs.is_fixed(branch)
        )
        if approve == APPROVE_POLICY or (approve is None and by_config):
            kwargs.update(approve=None, approve_if_policy=True)
        if digests is not None:
            kwargs["digests"] = dict(digests)
        else:
            kwargs["build_receipt"] = receipt
        return EnvOutcome.from_result(self._envs().env_up(pipeline, branch, **kwargs))

    def env_down(self, pipeline: Any, branch: str) -> None:
        result = self._envs().env_down(pipeline, branch, approve_if_policy=True)
        if result.get("state") == "approval-required":
            raise GitOpsError(
                "gitops-step-failed",
                f"removing the environment of {branch} needs the owner: piceli "
                f"env down {branch} --approve {result.get('env_hash')} (or "
                "EnvConfig(auto_approve=True))",
            )

    def env_stop(self, pipeline: Any, branch: str) -> None:
        result = self._envs().env_stop(
            pipeline, branch, approve_if_policy=True, reason="idle"
        )
        if result.get("state") == "approval-required":
            raise GitOpsError(
                "gitops-step-failed",
                f"stopping the environment of {branch} needs EnvConfig(idle_stop=...)",
            )
