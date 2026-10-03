"""Browser adapters for the existing environment and GitOps approval APIs.

The operator supplies the pipeline and controller target. Browser requests
never supply a module name, kubeconfig, command, or controller namespace.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, Any, Literal, cast

from piceli.envs import EnvError, env_down, env_up, list_envs, seed_env
from piceli.gitops import GitOpsError
from piceli.gitops.state import Channel, approve_request, promote_request
from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.pipeline import PipelineError
from piceli.services.query import QueryError, QueryService
from piceli.services.registration import Registration

if TYPE_CHECKING:
    from piceli.pipeline import Pipeline


def _public_entry(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep controller status useful without exposing arbitrary status fields."""
    return {
        key: value.get(key)
        for key in (
            "branch",
            "namespace",
            "commit",
            "deployed_commit",
            "state",
            "health",
            "build",
            "deploy",
            "plan_hash",
            "last_attempt",
        )
        if isinstance(value.get(key), (str, int, float, bool))
    }


class EnvironmentControl:
    """Exact-hash environment changes and requests to the installed controller."""

    def __init__(
        self,
        query: QueryService,
        application_id: str,
        *,
        pipeline: Pipeline | None = None,
        controller_target: KubeconfigTarget | None = None,
        controller_namespace: str = "piceli-system",
        channel_factory: Callable[[], AbstractContextManager[Channel]] | None = None,
    ) -> None:
        if pipeline is None and controller_target is None and channel_factory is None:
            raise ValueError(
                "environment control needs an explicit pipeline or controller"
            )
        if controller_target is not None and controller_target.allow_exec:
            raise ValueError("UI controller credentials cannot use an exec plugin")
        if (
            not controller_namespace
            or not controller_namespace.replace("-", "").isalnum()
        ):
            raise ValueError("invalid controller namespace")
        if application_id not in query.registrations:
            raise ValueError("environment control must name an installed scope")
        self.query = query
        self.application_id = application_id
        self.pipeline = pipeline
        self.controller_target = controller_target
        self.controller_namespace = controller_namespace
        self.channel_factory = channel_factory
        self._env_ids: set[str] = set()
        self._lock = threading.RLock()

    def _register_environments(self, items: list[dict[str, Any]]) -> None:
        pipeline = self.pipeline
        if pipeline is None or self.query.scope_policy is not None:
            return
        target = pipeline.target
        current: set[str] = set()
        with self._lock:
            for item in items:
                if item["main"]:
                    item["application_id"] = self.application_id
                    continue
                if item["state"] == "absent" or not item["namespace"]:
                    continue
                branch = item["branch"]
                identity = "env-" + hashlib.sha256(branch.encode()).hexdigest()[:24]
                registration = Registration(
                    id=identity,
                    name=f"{pipeline.name[:80]} / {branch[:120]}",
                    target=KubeconfigTarget(
                        kubeconfig=target.kubeconfig.absolute(),
                        context=target.context,
                        namespace=item["namespace"],
                        cluster_uid=target.cluster_uid,
                        transport=cast(
                            Literal["https", "loopback-http"], target.transport
                        ),
                        allow_exec=target.allow_exec,
                        exec_sha256=target.exec_sha256,
                        exec_pass_env=tuple(target.exec_pass_env),
                        exec_timeout_seconds=target.exec_timeout_seconds or 60,
                    ),
                )
                self.query.register_local(registration)
                item["application_id"] = identity
                current.add(identity)
            for identity in self._env_ids - current:
                self.query.remove_local(identity)
            self._env_ids = current

    def _authorize(self, action: str) -> None:
        self.query.registration(self.application_id, action=action)

    def _visible_entry(self, entry: Mapping[str, Any]) -> bool:
        if self.query.scope_policy is None:
            return True
        return (
            entry.get("namespace")
            == self.query.registration(self.application_id).target.namespace
        )

    @contextmanager
    def _channel(self) -> Iterator[Channel]:
        if self.channel_factory is not None:
            with self.channel_factory() as channel:
                yield channel
            return
        target = self.controller_target
        if target is None:
            raise QueryError("ui-operation-unavailable", 409)
        from piceli.gitops.install import connect
        from piceli.gitops.state import ConfigMapChannel

        try:
            with connect(
                target.kubeconfig,
                target.context,
                transport=target.transport,
                exec_policy=target.exec_policy,
            ) as api:
                yield ConfigMapChannel(api.client, self.controller_namespace)
        except GitOpsError:
            raise QueryError("ui-observation-unavailable", 503) from None

    def environments(self) -> dict[str, Any]:
        self._authorize("inspect")
        pipeline = self.pipeline
        if pipeline is None:
            status = self.gitops_status()
            return {
                "configured": status["configured"],
                "items": [
                    {
                        "branch": entry["branch"],
                        "namespace": entry.get("namespace") or "",
                        "main": False,
                        "state": entry.get("state") or "unknown",
                        "health": entry.get("health") or "unknown",
                        "commit": entry.get("commit"),
                        "age_seconds": None,
                        "build": entry.get("build"),
                        "deploy": entry.get("deploy"),
                        "workloads": [],
                        "gitops": entry,
                    }
                    for entry in status["envs"]
                ],
            }
        try:
            items = []
            for item in list_envs(pipeline):
                row = item.to_dict()
                row["gitops"] = (
                    _public_entry(row["gitops"])
                    if isinstance(row.get("gitops"), Mapping)
                    else None
                )
                items.append(row)
        except (EnvError, PipelineError):
            raise QueryError("ui-observation-unavailable", 503) from None
        self._register_environments(items)
        return {"items": items, "configured": True}

    def environment_action(
        self,
        verb: str,
        branch: str,
        *,
        approved_hash: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        self._authorize("deploy")
        pipeline = self.pipeline
        if pipeline is None or verb not in {"up", "down", "seed"}:
            raise QueryError("ui-operation-unavailable", 409)
        if (
            not branch
            or len(branch) > 250
            or (source is not None and len(source) > 250)
        ):
            raise QueryError("ui-invalid-request", 422)
        if approved_hash is not None and (
            len(approved_hash) != 71
            or not approved_hash.startswith("sha256:")
            or any(char not in "0123456789abcdef" for char in approved_hash[7:])
        ):
            raise QueryError("ui-invalid-request", 422)
        try:
            if verb == "up":
                result = env_up(pipeline, branch, approve=approved_hash)
            elif verb == "down":
                result = env_down(pipeline, branch, approve=approved_hash)
            else:
                result = seed_env(pipeline, branch, source or "", approve=approved_hash)
        except (EnvError, PipelineError) as error:
            raise QueryError(
                "ui-plan-stale"
                if error.code == "env-plan-changed"
                else "ui-operation-unavailable",
                409,
            ) from None
        # The core API re-plans and checks its own hash before any write.
        return {
            key: value
            for key, value in result.items()
            if key
            in {
                "schema",
                "state",
                "branch",
                "namespace",
                "main",
                "env_hash",
                "create_namespace",
                "stop",
                "delete",
                "seed",
                "source",
                "claims",
                "writers",
            }
        }

    def gitops_status(self) -> dict[str, Any]:
        self._authorize("inspect")
        with self._channel() as channel:
            try:
                document = channel.read_status()
            except GitOpsError:
                raise QueryError("ui-observation-unavailable", 503) from None
        if document is None:
            return {"configured": False, "controller": None, "envs": []}
        controller = document.get("controller") or {}
        return {
            "configured": True,
            "controller": {
                key: controller.get(key)
                for key in ("state", "last_poll", "poll_seconds")
                if isinstance(controller.get(key), (str, int, float, bool, list))
            },
            "envs": [
                {"branch": branch, **_public_entry(value)}
                for branch, value in sorted((document.get("envs") or {}).items())
                if isinstance(branch, str)
                and isinstance(value, Mapping)
                and self._visible_entry(value)
            ],
        }

    def approve_gitops(self, branch: str, plan_hash: str) -> dict[str, str]:
        self._authorize("deploy")
        with self._channel() as channel:
            try:
                document = channel.read_status() or {}
                entry = (document.get("envs") or {}).get(branch)
                if (
                    not isinstance(entry, Mapping)
                    or not self._visible_entry(entry)
                    or entry.get("state") != "approval-required"
                    or entry.get("plan_hash") != plan_hash
                ):
                    raise QueryError("ui-plan-stale", 409)
                key, body = approve_request(branch, plan_hash, via="ui")
                channel.add_request(key, body)
            except GitOpsError:
                raise QueryError("ui-operation-unavailable", 409) from None
        return {"state": "requested", "branch": branch, "plan_hash": plan_hash}

    def promote(self, branch: str, commit: str) -> dict[str, str]:
        self._authorize("deploy")
        with self._channel() as channel:
            try:
                document = channel.read_status() or {}
                entry = (document.get("envs") or {}).get(branch)
                if (
                    not isinstance(entry, Mapping)
                    or not self._visible_entry(entry)
                    or commit
                    not in {
                        entry.get("commit"),
                        entry.get("deployed_commit"),
                    }
                ):
                    raise QueryError("ui-plan-stale", 409)
                key, body = promote_request(f"{branch}@{commit}")
                channel.add_request(key, body)
            except GitOpsError:
                raise QueryError("ui-operation-unavailable", 409) from None
        return {"state": "requested", "branch": branch, "commit": commit}
