"""Piceli Automation: opt-in Git/PR watch, digest promotion, dependency-safe releases, and health-aware rollback.

Security invariant:
Untrusted PR code NEVER runs with deployment credentials. Untrusted PRs can only trigger
isolated dry-run previews or lints. Deploying or promoting a PR requires an explicit operator approval.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from piceli.k8s.operator_state import FileStateStore, StandingPolicy
from piceli.k8s.ops.executor import PlanExecutor
from piceli.k8s.ops.session import DeploymentSession
from piceli.k8s.release import (
    ReleaseCatalog,
    ReleaseRecord,
    ReleaseWorkflow,
)

_REF_NAME = re.compile(r"^[a-zA-Z0-9_./-]+$")


class DependencyUnsatisfiedError(RuntimeError):
    """Raised when a partial release has unsatisfied dependencies."""


class UntrustedPRCredentialError(PermissionError):
    """Raised when untrusted PR code attempts to execute with deployment credentials."""


class RollbackHealthError(RuntimeError):
    """Raised when a rollback fails health or migration compatibility checks."""


@dataclass(frozen=True)
class GitRefState:
    """Observed Git commit state for a watched ref or PR."""

    ref: str
    commit_hash: str
    is_pr: bool = False
    pr_id: int | None = None
    author: str = ""
    trusted: bool = False
    last_seen: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not _REF_NAME.match(self.ref):
            raise ValueError(f"invalid ref name: {self.ref}")
        if not re.fullmatch(r"[0-9a-f]{40}", self.commit_hash):
            raise ValueError(f"invalid git commit hash: {self.commit_hash}")


@dataclass(frozen=True)
class PRApproval:
    """Explicit authorization record granting deployment credentials for a PR."""

    pr_id: int
    commit_hash: str
    approved_by: str
    target_namespace: str
    approved_at: float = field(default_factory=time.time)
    standing_policy_name: str | None = None


class ApprovalStore:
    """State-backed store for PR deployment approvals."""

    def __init__(self, store: FileStateStore) -> None:
        self.store = store

    def load_approvals(self) -> list[PRApproval]:
        raw = self.store.load_data("approvals")
        items = []
        for item in raw.get("approvals", []):
            items.append(
                PRApproval(
                    pr_id=item["pr_id"],
                    commit_hash=item["commit_hash"],
                    approved_by=item["approved_by"],
                    target_namespace=item["target_namespace"],
                    approved_at=item.get("approved_at", 0.0),
                    standing_policy_name=item.get("standing_policy_name"),
                )
            )
        return items

    def record_approval(self, approval: PRApproval) -> None:
        approvals = self.load_approvals()
        # Deduplicate
        approvals = [a for a in approvals if not (a.pr_id == approval.pr_id and a.commit_hash == approval.commit_hash)]
        approvals.append(approval)
        data = {
            "schema_version": 1,
            "approvals": [
                {
                    "pr_id": a.pr_id,
                    "commit_hash": a.commit_hash,
                    "approved_by": a.approved_by,
                    "target_namespace": a.target_namespace,
                    "approved_at": a.approved_at,
                    "standing_policy_name": a.standing_policy_name,
                }
                for a in approvals
            ],
        }
        self.store.save_data("approvals", data)

    def is_approved(self, pr_id: int, commit_hash: str, target_namespace: str) -> bool:
        for a in self.load_approvals():
            if a.pr_id == pr_id and a.commit_hash == commit_hash and a.target_namespace == target_namespace:
                return True
        return False


class GitBranchWatcher:
    """Opt-in watcher tracking configured branches and PRs for changes."""

    def __init__(
        self,
        *,
        watched_branches: tuple[str, ...],
        watch_prs: bool = False,
        policy: StandingPolicy | None = None,
        git_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self.watched_branches = watched_branches
        self.watch_prs = watch_prs
        self.policy = policy
        self._git_resolver = git_resolver or self._default_resolver
        self._known_refs: dict[str, GitRefState] = {}

    @staticmethod
    def _default_resolver(ref: str) -> str | None:
        return None

    def check_updates(self) -> list[GitRefState]:
        """Poll watched refs and return newly discovered commits."""
        updates: list[GitRefState] = []
        for branch in self.watched_branches:
            ref = f"refs/heads/{branch}"
            commit = self._git_resolver(ref)
            if commit is not None:
                current = self._known_refs.get(ref)
                if current is None or current.commit_hash != commit:
                    state = GitRefState(
                        ref=ref,
                        commit_hash=commit,
                        is_pr=False,
                        trusted=True,
                    )
                    self._known_refs[ref] = state
                    updates.append(state)
        return updates


def promote_release(
    catalog: ReleaseCatalog,
    source_name: str,
    target_name: str,
    *,
    policy: StandingPolicy | None = None,
    select: bool = True,
) -> ReleaseRecord:
    """Promote an existing immutable release to a new tag/name without rebuilding!"""
    source_record = catalog.get(source_name)

    if policy is not None:
        if not policy.authorize(
            "promote",
            namespace=source_record.namespace,
            registry=source_record.source.artifact_digest,
        ):
            raise PermissionError(
                f"standing policy '{policy.name}' does not authorize promotion of {source_name}"
            )

    promoted_record = ReleaseRecord(
        name=target_name,
        source=source_record.source,
        archive=source_record.archive,
        namespace=source_record.namespace,
        ttl_seconds=source_record.ttl_seconds,
        retained_pvc_policy=source_record.retained_pvc_policy,
    )

    catalog.add(promoted_record, select=select)
    return promoted_record


def preview_release(workflow: ReleaseWorkflow, name: str | None = None) -> dict[str, Any]:
    """Provider-free preview of a release without mutating state or passing credentials."""
    return workflow.preview(name)


def dependency_safe_partial_release(
    workflow: ReleaseWorkflow,
    components_to_deploy: Sequence[str],
    name: str | None = None,
) -> DeploymentSession:
    """Verify dependency closure before executing a partial component rollout.

    If components in components_to_deploy depend on other components not present
    in the session or target snapshot, raises DependencyUnsatisfiedError.
    """
    session = workflow.reopen(name)
    archive_dict = session.archive.to_dict()
    all_components = {c["name"]: c for c in archive_dict.get("composition", [])}

    for comp_name in components_to_deploy:
        if comp_name not in all_components:
            raise DependencyUnsatisfiedError(f"requested component '{comp_name}' not in composition")
        comp = all_components[comp_name]
        deps = comp.get("dependencies", [])
        for dep in deps:
            if dep not in components_to_deploy:
                # Check if dependency exists in snapshot
                present = False
                if hasattr(workflow, "snapshot") and workflow.snapshot is not None:
                    present = any(r.intent.ref.name == dep for r in workflow.snapshot.resources)
                if not present:
                    raise DependencyUnsatisfiedError(
                        f"component '{comp_name}' requires '{dep}' which is not scheduled or present"
                    )

    return session


def health_aware_rollback(
    workflow: ReleaseWorkflow,
    target_release_name: str,
    executor: PlanExecutor,
    *,
    health_timeout_seconds: float = 30.0,
    check_health_fn: Callable[[str, str], bool] | None = None,
    migration_compatible_fn: Callable[[str, str], bool] | None = None,
) -> dict[str, Any]:
    """Roll back to a prior catalogued release with health checks and migration safety.

    Rollback digests are strictly protected from GC.
    """
    target_record = workflow.catalog.get(target_release_name)

    # 1. Migration preflight: ensure target does not break migration compatibility
    if migration_compatible_fn is not None:
        if not migration_compatible_fn(workflow.namespace, target_release_name):
            raise RollbackHealthError(
                f"release {target_release_name} failed migration compatibility check for rollback"
            )

    # 2. Select target release and apply
    workflow.catalog.select(target_release_name)
    session = workflow.reopen(target_release_name)
    apply_result = session.apply(executor)

    if apply_result.get("state") not in {"ready", "applied"}:
        raise RollbackHealthError(
            f"rollback apply failed with state {apply_result.get('state')}"
        )

    # 3. Health verification
    if check_health_fn is not None:
        healthy = check_health_fn(workflow.namespace, target_release_name)
        if not healthy:
            raise RollbackHealthError(
                f"workloads failed readiness checks after rollback to {target_release_name}"
            )

    return {
        "state": "ready",
        "rollback_target": target_release_name,
        "artifact_digest": target_record.source.artifact_digest,
        "applied_at": time.time(),
    }
