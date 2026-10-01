"""Reviewed cluster builds dispatched from an installed, scoped UI.

The operator supplies only declarative build settings. The UI never imports
repository code: the existing build Job fetches the approved Git commit and
reads its host-build specification inside the separate token-free Pod.
"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from piceli import App, Build, Pipeline, Registry, Target
from piceli.artifacts.cluster_build import (
    ClusterBuildConfig,
    plan_build_job,
    run_build_job,
)
from piceli.pipeline import PipelineError
from piceli.services.query import QueryError, QueryService
from piceli.services.store import Store, digest, now

_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SPEC = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")


@dataclass(frozen=True)
class ClusterBuildProfile:
    spec_path: str
    config: ClusterBuildConfig
    platforms: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not _SPEC.fullmatch(self.spec_path)
            or any(part in {".", ".."} for part in self.spec_path.split("/"))
            or not self.platforms
            or any(
                item not in {"linux/amd64", "linux/arm64"} for item in self.platforms
            )
        ):
            raise ValueError("invalid installed build profile")


class ClusterBuildControl:
    """A private outbox worker sharing the release dispatcher's SQLite lock."""

    def __init__(
        self,
        query: QueryService,
        application_id: str,
        profile: ClusterBuildProfile,
        store: Store,
        control_dir: Path,
    ) -> None:
        if query.scope_policy is None or application_id not in query.registrations:
            raise ValueError("cluster build requires one authorized scope")
        self.query, self.application_id = query, application_id
        self.profile, self.store = profile, store
        self.control_dir = control_dir
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def _pipeline(self) -> Pipeline:
        target = self.query.registrations[self.application_id].target
        config = self.profile.config
        assert config.registry_url is not None
        return Pipeline(
            App(self.application_id),
            Target.kubeconfig(
                target.kubeconfig,
                context=target.context,
                namespace=target.namespace,
                cluster_uid=target.cluster_uid,
                namespace_uid=target.namespace_uid,
                transport=target.transport,
            ),
            build=Build.spec(
                self.control_dir / "build-specs" / self.profile.spec_path,
                builder="host",
            ),
            deliver=Registry(config.registry_url),
            state_dir=self.control_dir / "cluster-builds",
        )

    def start(self) -> None:
        # OperationService has already acquired this Store's lifetime lock.
        self.store.acquire_dispatcher()
        for item in self.store.active("cluster_build_operation"):
            if item["state"] == "running":
                self.store.update(
                    "cluster_build_operation",
                    {
                        **item,
                        "state": "interrupted",
                        "updated_at": now(),
                        "error_code": "ui-operation-interrupted",
                    },
                )
        self._stop.clear()
        self._thread = threading.Thread(target=self._dispatch, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            # Keep the shared dispatcher lock while the Job caller writes its
            # final receipt. A killed Pod releases the OS lock on process exit.
            self._thread.join()
        self._thread = None

    def plan(self, commit: str, cache_key: str) -> dict[str, Any]:
        self.query.registration(self.application_id, action="deploy")
        if not _COMMIT.fullmatch(commit) or not (1 <= len(cache_key) <= 200):
            raise QueryError("ui-invalid-request", 422)
        try:
            planned = plan_build_job(
                self._pipeline(),
                commit,
                cache_key=cache_key,
                platforms=self.profile.platforms,
                config=self.profile.config,
            )
        except PipelineError:
            raise QueryError("cluster-build-invalid", 409) from None
        item = {
            "id": uuid.uuid4().hex,
            "application_id": self.application_id,
            "state": "planned",
            "digest": planned.plan_hash,
            "commit": commit,
            "cache_key": cache_key,
            "preview": planned.preview(),
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            "created_at": now(),
        }
        self.store.put("cluster_build_plan", item)
        return item

    def get_plan(self, identity: str) -> dict[str, Any]:
        item, _ = self.store.get("cluster_build_plan", identity)
        self.query.registration(item["application_id"], action="deploy")
        return item

    def admit(
        self, identity: str, approved_digest: str, idempotency_key: str
    ) -> dict[str, Any]:
        self.query.registration(self.application_id, action="deploy")
        actor = self.query._principal().id
        fingerprint = digest({"build": identity, "approved": approved_digest})
        existing = self.store.idempotent(
            idempotency_key, fingerprint, "cluster_build_operation", principal=actor
        )
        if existing is not None:
            return existing
        plan = self.get_plan(identity)
        if plan["application_id"] != self.application_id:
            raise QueryError("ui-not-found")
        if plan["digest"] != approved_digest:
            raise QueryError("ui-approval-mismatch", 409)
        if datetime.fromisoformat(plan["expires_at"]) <= datetime.now(UTC):
            raise QueryError("ui-plan-stale", 409)
        try:
            current = plan_build_job(
                self._pipeline(),
                plan["commit"],
                cache_key=plan["cache_key"],
                platforms=self.profile.platforms,
                config=self.profile.config,
            )
        except PipelineError:
            raise QueryError("cluster-build-invalid", 409) from None
        if current.plan_hash != approved_digest:
            raise QueryError("ui-plan-stale", 409)
        operation = {
            "id": uuid.uuid4().hex,
            "application_id": self.application_id,
            "state": "queued",
            "actor": actor,
            "plan_id": identity,
            "approved_digest": approved_digest,
            "created_at": now(),
            "updated_at": now(),
        }
        result = self.store.admit(
            "cluster_build_operation",
            operation,
            private={"commit": plan["commit"], "cache_key": plan["cache_key"]},
            scope=digest(
                {"build": self.application_id, "target": plan["preview"]["namespace"]}
            ),
            key=idempotency_key,
            fingerprint=fingerprint,
            principal=actor,
        )
        self._wake.set()
        return result

    def operation(self, identity: str) -> dict[str, Any]:
        item, _ = self.store.get("cluster_build_operation", identity)
        self.query.registration(item["application_id"], action="deploy")
        return item

    def operations(self) -> dict[str, Any]:
        self.query.registration(self.application_id, action="deploy")
        return {
            "items": self.store.records("cluster_build_operation", self.application_id)
        }

    def _dispatch(self) -> None:
        while not self._stop.is_set():
            for item in self.store.active("cluster_build_operation"):
                if item["state"] != "queued":
                    continue
                item.update(state="running", updated_at=now())
                self.store.update("cluster_build_operation", item)
                _, private = self.store.get("cluster_build_operation", item["id"])
                try:
                    receipt = run_build_job(
                        self._pipeline(),
                        private["commit"],
                        cache_key=private["cache_key"],
                        platforms=self.profile.platforms,
                        config=self.profile.config,
                        approve=item["approved_digest"],
                    )
                    item.update(
                        state="succeeded",
                        updated_at=now(),
                        images=receipt.get("delivered", {}),
                        job=receipt.get("job"),
                    )
                except PipelineError as error:
                    outcome = error.details.get("outcome")
                    failure = (
                        {
                            key: value
                            for key, value in outcome.items()
                            if key
                            in {"state", "reason", "category", "exit_code", "cleaned"}
                        }
                        if isinstance(outcome, dict)
                        else {}
                    )
                    if isinstance(error.details.get("log_access"), str):
                        failure["log_access"] = error.details["log_access"]
                    item.update(
                        state="interrupted" if self._stop.is_set() else "failed",
                        updated_at=now(),
                        error_code=error.code,
                        failure=failure,
                    )
                except Exception as error:
                    item.update(
                        state="interrupted" if self._stop.is_set() else "failed",
                        updated_at=now(),
                        error_code="cluster-build-failed",
                        failure={"kind": type(error).__name__},
                    )
                self.store.update("cluster_build_operation", item)
            self._wake.wait(0.25)
            self._wake.clear()
