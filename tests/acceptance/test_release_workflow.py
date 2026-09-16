"""Acceptance coverage for Piceli's session-backed public release facade."""

from dataclasses import replace

import pytest

from piceli.k8s.ops.discovery import DiscoveryArtifact, ResourceType
from piceli.k8s.ops.plan import ObservedSnapshot, PlanAuthorization
from piceli.k8s.release import ReleaseCatalog, ReleaseSource, ReleaseWorkflow
from tests.acceptance.test_deployment_session import _authorization, _composition
from tests.acceptance.fake_api import manifest
from tests.acceptance.test_local_executor import discover, executor


def test_workflow_reopens_without_rotating_private_inputs(local_api, tmp_path):
    """Create, select, reopen and apply share one immutable session identity."""
    _, provider = local_api
    run = executor(provider, tmp_path)
    snapshot = ObservedSnapshot.from_discovery(
        discover(
            provider,
            (ResourceType("v1", "Secret"), ResourceType("apps/v1", "Deployment")),
        )
    )
    workflow = ReleaseWorkflow(
        ReleaseCatalog(tmp_path / "operator" / "releases.json"),
        provider.target.namespace,
        _composition,
        snapshot,
        PlanAuthorization(provider.target),
        _authorization(provider),
        run.journal,
        run.secrets,
    )
    record = workflow.create(
        name="branch-a",
        source=ReleaseSource("git", "a" * 40, artifact_digest="sha256:" + "1" * 64),
        private_inputs={"password": "never-report-this"},
        session_id="b" * 32,
        execution_id="release-execution",
    )
    reopened = workflow.reopen()

    assert reopened.archive.to_json() == record.archive.to_json()
    assert reopened.bundle.action_ids == workflow.reopen().bundle.action_ids
    assert "never-report-this" not in record.archive.to_json()
    assert workflow.apply(run)["state"] == "ready"
    assert workflow.preview()["revision_id"] == reopened.revision.revision_id
    before = workflow.catalog.path.read_bytes()
    modified = workflow.catalog.path.stat().st_mtime_ns
    assert workflow.preview("branch-a")["revision_id"] == reopened.revision.revision_id
    assert workflow.catalog.path.read_bytes() == before
    assert workflow.catalog.path.stat().st_mtime_ns == modified
    with pytest.raises(ValueError, match="already exists"):
        workflow.create(
            name="branch-a",
            source=record.source,
            private_inputs={"password": "must-not-be-materialized"},
        )
    assert workflow.reopen().archive.to_json() == record.archive.to_json()
    direct = replace(
        record,
        name="direct",
        source=ReleaseSource(
            "oci", "sha256:" + "1" * 64, artifact_digest="sha256:" + "1" * 64
        ),
    )
    workflow.catalog.add(direct, select=False)
    assert workflow.reopen("direct").bundle.action_ids == reopened.bundle.action_ids
    assert workflow.catalog.selected().name == "branch-a"
    with pytest.raises(ValueError, match="namespace"):
        replace(workflow, namespace="different-target")


def test_secret_bearing_discovery_round_trips_for_private_release_reopen(
    local_api, tmp_path
):
    """Owner-only discovery preserves exact hashes while public JSON redacts."""
    api, provider = local_api
    api.put(manifest("Secret", "existing", value="bmV2ZXItcmVwb3J0"), owned=True)
    artifact = discover(
        provider,
        (ResourceType("v1", "Secret"), ResourceType("apps/v1", "Deployment")),
    )
    private = artifact.to_private_json()
    public = artifact.to_json()

    assert "bmV2ZXItcmVwb3J0" in private
    assert "bmV2ZXItcmVwb3J0" not in public
    restored = DiscoveryArtifact.from_private_json(private)
    assert restored == artifact

    run = executor(provider, tmp_path)
    original_snapshot = ObservedSnapshot.from_discovery(artifact)
    workflow = ReleaseWorkflow(
        ReleaseCatalog(tmp_path / "operator" / "releases.json"),
        provider.target.namespace,
        _composition,
        original_snapshot,
        PlanAuthorization(provider.target),
        _authorization(provider),
        run.journal,
        run.secrets,
    )
    record = workflow.create(
        name="secret-snapshot",
        source=ReleaseSource("git", "a" * 40),
        private_inputs={"password": "never-report-this"},
        session_id="c" * 32,
        execution_id="secret-snapshot-execution",
    )
    reopened = replace(
        workflow,
        snapshot=ObservedSnapshot.from_discovery(restored),
    ).reopen("secret-snapshot")
    assert reopened.archive.to_json() == record.archive.to_json()
