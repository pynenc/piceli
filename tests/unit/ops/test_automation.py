"""Unit tests for Piceli automation: promotion, approvals, partial rollout, and rollback."""

import json
from pathlib import Path

import pytest

from piceli.k8s.automation import (
    ApprovalStore,
    PRApproval,
    RollbackHealthError,
    health_aware_rollback,
    promote_release,
)
from piceli.k8s.operator_state import FileStateStore, StandingPolicy
from piceli.k8s.ops.session import DeploymentSessionArchive
from piceli.k8s.release import ReleaseCatalog, ReleaseRecord, ReleaseSource


def _make_archive(session_id: str) -> DeploymentSessionArchive:
    return DeploymentSessionArchive.from_json(
        json.dumps(
            {
                "bundle": {},
                "composition": [],
                "private_inputs": [],
                "revision": {},
                "schema_version": 1,
                "session_id": session_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def test_promote_release_preserves_digest_and_source(tmp_path: Path) -> None:
    cat_file = tmp_path / "releases.json"
    catalog = ReleaseCatalog(cat_file)

    archive = _make_archive("1" * 32)
    source = ReleaseSource(
        kind="git",
        identity="a" * 40,
        artifact_digest="sha256:" + "e" * 64,
    )
    record = ReleaseRecord(
        name="v1-0-0-rc1",
        source=source,
        namespace="test-ns",
        archive=archive,
    )
    catalog.add(record, select=True)

    # Promote to production tag
    promoted = promote_release(catalog, "v1-0-0-rc1", "production")
    assert promoted.name == "production"
    assert promoted.source.artifact_digest == "sha256:" + "e" * 64
    assert promoted.archive.session_id == "1" * 32
    assert catalog.selected().name == "production"
    assert catalog.get("v1-0-0-rc1").source.artifact_digest == promoted.source.artifact_digest


def test_standing_policy_blocks_unauthorized_promotion(tmp_path: Path) -> None:
    cat_file = tmp_path / "releases.json"
    catalog = ReleaseCatalog(cat_file)
    archive = _make_archive("2" * 32)
    record = ReleaseRecord(
        name="branch-x",
        source=ReleaseSource("git", "b" * 40, artifact_digest="sha256:" + "f" * 64),
        namespace="forbidden-ns",
        archive=archive,
    )
    catalog.add(record)

    policy = StandingPolicy(
        name="strict",
        allowed_namespaces=("allowed-ns",),
        allowed_operations=("promote",),
    )

    with pytest.raises(PermissionError, match="does not authorize promotion"):
        promote_release(catalog, "branch-x", "staging", policy=policy)


def test_approval_store_and_untrusted_pr_checks(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path / "state")
    approvals = ApprovalStore(store)

    assert not approvals.is_approved(42, "c" * 40, "test-ns")

    approval = PRApproval(
        pr_id=42,
        commit_hash="c" * 40,
        approved_by="admin-bob",
        target_namespace="test-ns",
    )
    approvals.record_approval(approval)
    assert approvals.is_approved(42, "c" * 40, "test-ns")
    assert not approvals.is_approved(42, "c" * 40, "other-ns")
    assert not approvals.is_approved(43, "c" * 40, "test-ns")


def test_rollback_migration_incompatible_rejected(tmp_path: Path) -> None:
    cat_file = tmp_path / "releases.json"
    catalog = ReleaseCatalog(cat_file)
    archive = _make_archive("3" * 32)
    record = ReleaseRecord(
        name="old-release",
        source=ReleaseSource("git", "d" * 40, artifact_digest="sha256:" + "1" * 64),
        namespace="test-ns",
        archive=archive,
    )
    catalog.add(record)

    class DummyWorkflow:
        catalog = None
        namespace = "test-ns"

    wf = DummyWorkflow()
    wf.catalog = catalog

    with pytest.raises(RollbackHealthError, match="failed migration compatibility check"):
        health_aware_rollback(
            wf,
            "old-release",
            None,
            migration_compatible_fn=lambda ns, name: False,
        )
