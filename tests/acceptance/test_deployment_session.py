"""DeploymentSession identity and recovery acceptance over the local API."""

from __future__ import annotations

import json
from dataclasses import replace
import pytest

from piceli.k8s.ops.discovery import ResourceType
from piceli.k8s.ops.executor import ActionGrant, ExecutionAuthorization, PlanExecutor
from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    ObservedSnapshot,
    PlanAuthorization,
    ResourceIntent,
)
from piceli.k8s.ops.session import DeploymentSession
from piceli.k8s.ops.secret_versions import SecretVersionStore
from tests.acceptance.fake_api import TARGET, manifest
from tests.acceptance.test_local_executor import discover, executor, mutations


def _authorization(provider, *, max_evidence_age_seconds=300):
    def factory(plan, snapshot):
        return ExecutionAuthorization(
            "session-grant",
            TARGET,
            provider.provenance,
            plan.plan_hash,
            snapshot.snapshot_hash,
            provider.field_manager,
            provider.owner_id,
            tuple(ActionGrant.for_action(action) for action in plan.actions),
            "2030-01-01T00:00:00+00:00",
            compensation_resources=tuple(
                action.resource.ref for action in plan.actions
            ),
            max_evidence_age_seconds=max_evidence_age_seconds,
        )

    return factory


def _composition(inputs):
    secret = ResourceIntent.from_manifest(manifest("Secret", "credential")).with_secret(
        "/data/password", inputs["password"]
    )
    worker = ResourceIntent.from_manifest(manifest("Deployment", "worker"))
    return DeploymentComposition(
        (
            DeploymentComponent("credential", (secret,)),
            DeploymentComponent("worker", (worker,), dependencies=("credential",)),
        )
    )


def _session(provider, run, *, session_id="a" * 32, max_evidence_age_seconds=300):
    snapshot = ObservedSnapshot.from_discovery(
        discover(
            provider,
            (ResourceType("v1", "Secret"), ResourceType("apps/v1", "Deployment")),
        )
    )
    return (
        DeploymentSession.create(
            private_inputs={"password": "never-report-this"},
            composition_factory=_composition,
            snapshot=snapshot,
            plan_authorization=PlanAuthorization(TARGET),
            authorization_factory=_authorization(
                provider, max_evidence_age_seconds=max_evidence_age_seconds
            ),
            journal=run.journal,
            secrets=run.secrets,
            session_id=session_id,
            execution_id="session-execution",
        ),
        snapshot,
    )


def test_session_preview_apply_restart_resume_and_archive_are_identical(
    local_api, tmp_path
):
    api, provider = local_api
    run = executor(provider, tmp_path)
    session, snapshot = _session(provider, run)
    before = len(api.requests)

    preview = session.preview()
    assert preview["plan"]["plan_hash"] == session.revision.plan.plan_hash
    assert len(api.requests) == before
    archive = session.archive.to_json()
    assert "never-report-this" not in archive
    assert "never-report-this" not in json.dumps(session.report())
    assert "store_id" not in json.dumps(session.report())

    crash = PlanExecutor(
        provider,
        run.journal,
        run.secrets,
        after_response=lambda _: (_ for _ in ()).throw(SystemExit()),
    )
    with pytest.raises(SystemExit):
        session.apply(crash)
    reopened = DeploymentSession.open(
        archive,
        composition_factory=_composition,
        snapshot=snapshot,
        plan_authorization=PlanAuthorization(TARGET),
        authorization_factory=_authorization(provider),
        journal=run.journal,
        secrets=run.secrets,
    )
    assert reopened.archive.to_json() == archive
    assert reopened.bundle.action_ids == session.bundle.action_ids
    assert reopened.resume(run)["state"] == "ready"
    assert [request["body"]["kind"] for request in mutations(api)] == [
        "Secret",
        "Deployment",
    ]


def test_session_archive_preserves_non_default_authorization_bounds(
    local_api, tmp_path
):
    """Recovery cannot silently widen a caller-selected evidence-age bound."""
    _, provider = local_api
    run = executor(provider, tmp_path)
    session, snapshot = _session(provider, run, max_evidence_age_seconds=120)

    assert (
        session.archive.to_dict()["revision"]["authorization"][
            "max_evidence_age_seconds"
        ]
        == 120
    )
    reopened = DeploymentSession.open(
        session.archive.to_json(),
        composition_factory=_composition,
        snapshot=snapshot,
        plan_authorization=PlanAuthorization(TARGET),
        authorization_factory=_authorization(provider, max_evidence_age_seconds=120),
        journal=run.journal,
        secrets=run.secrets,
    )
    assert reopened.archive.to_json() == session.archive.to_json()


def test_session_rejects_private_store_composition_and_grant_drift(local_api, tmp_path):
    _, provider = local_api
    run = executor(provider, tmp_path)
    session, snapshot = _session(provider, run)

    with pytest.raises(ValueError, match="private reference/store mismatch"):
        DeploymentSession.open(
            session.archive.to_json(),
            composition_factory=_composition,
            snapshot=snapshot,
            plan_authorization=PlanAuthorization(TARGET),
            authorization_factory=_authorization(provider),
            journal=run.journal,
            secrets=SecretVersionStore(tmp_path / "other" / "versions.sqlite"),
        )

    def changed(inputs):
        composition = _composition(inputs)
        worker_manifest = manifest("Deployment", "worker")
        worker_manifest["spec"]["replicas"] = 2
        changed_worker = ResourceIntent.from_manifest(worker_manifest)
        return DeploymentComposition(
            (
                composition.components[0],
                DeploymentComponent(
                    "worker", (changed_worker,), dependencies=("credential",)
                ),
            )
        )

    with pytest.raises(ValueError, match="composition changed"):
        DeploymentSession.open(
            session.archive.to_json(),
            composition_factory=changed,
            snapshot=snapshot,
            plan_authorization=PlanAuthorization(TARGET),
            authorization_factory=_authorization(provider),
            journal=run.journal,
            secrets=run.secrets,
        )

    def replaced_grant(plan, observed):
        grant = _authorization(provider)(plan, observed)
        return ExecutionAuthorization(
            "replacement",
            grant.target,
            grant.provenance,
            grant.plan_hash,
            grant.snapshot_hash,
            grant.field_manager,
            grant.owner_id,
            grant.actions,
            grant.expires_at,
        )

    with pytest.raises(ValueError, match="revision or authorization changed"):
        DeploymentSession.open(
            session.archive.to_json(),
            composition_factory=_composition,
            snapshot=snapshot,
            plan_authorization=PlanAuthorization(TARGET),
            authorization_factory=replaced_grant,
            journal=run.journal,
            secrets=run.secrets,
        )

    def expired_grant(plan, observed):
        return replace(
            _authorization(provider)(plan, observed),
            expires_at="2000-01-01T00:00:00+00:00",
        )

    with pytest.raises(ValueError, match="authorization expired"):
        DeploymentSession.open(
            session.archive.to_json(),
            composition_factory=_composition,
            snapshot=snapshot,
            plan_authorization=PlanAuthorization(TARGET),
            authorization_factory=expired_grant,
            journal=run.journal,
            secrets=run.secrets,
        )


def test_session_rotation_creates_a_new_private_binding_and_revision(
    local_api, tmp_path
):
    _, provider = local_api
    run = executor(provider, tmp_path)
    first, snapshot = _session(provider, run, session_id="b" * 32)
    second = first.rotate(
        private_inputs={"password": "never-report-this"},
        composition_factory=_composition,
        snapshot=snapshot,
        plan_authorization=PlanAuthorization(TARGET),
        authorization_factory=_authorization(provider),
        session_id="c" * 32,
        execution_id="rotated-execution",
    )
    assert first.archive.inputs()["password"] != second.archive.inputs()["password"]
    assert first.revision.revision_id != second.revision.revision_id
    assert first.archive.to_json() == json.dumps(
        run.journal.session("b" * 32), sort_keys=True, separators=(",", ":")
    )
    with pytest.raises(ValueError, match="open it or rotate"):
        DeploymentSession.create(
            private_inputs={"password": "changed-but-uncomparable"},
            composition_factory=_composition,
            snapshot=snapshot,
            plan_authorization=PlanAuthorization(TARGET),
            authorization_factory=_authorization(provider),
            journal=run.journal,
            secrets=run.secrets,
            session_id="b" * 32,
        )


def test_session_wffc_retained_resources_and_owner_scoped_stop(local_api, tmp_path):
    api, provider = local_api
    api.wait_for_first_consumer.add("data")
    run = executor(provider, tmp_path)
    snapshot = ObservedSnapshot.from_discovery(
        discover(
            provider,
            (
                ResourceType("v1", "PersistentVolumeClaim"),
                ResourceType("apps/v1", "Deployment"),
            ),
        )
    )

    def wffc(_inputs):
        claim = ResourceIntent.from_manifest(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": "data", "namespace": TARGET.namespace},
                "spec": {"accessModes": ["ReadWriteOnce"]},
            }
        )
        worker = ResourceIntent.from_manifest(manifest("Deployment", "wffc-worker"))
        worker_manifest = worker.manifest
        worker_manifest["spec"]["template"]["spec"]["volumes"] = [
            {"name": "data", "persistentVolumeClaim": {"claimName": "data"}}
        ]
        worker = ResourceIntent.from_manifest(worker_manifest)
        return DeploymentComposition(
            (
                DeploymentComponent("volume", (claim,)),
                DeploymentComponent("worker", (worker,), dependencies=("volume",)),
            )
        )

    session = DeploymentSession.create(
        private_inputs={},
        composition_factory=wffc,
        snapshot=snapshot,
        plan_authorization=PlanAuthorization(TARGET),
        authorization_factory=_authorization(provider),
        journal=run.journal,
        secrets=run.secrets,
        session_id="d" * 32,
        execution_id="wffc-execution",
    )
    assert session.apply(run)["state"] == "ready"
    provider.owner_id = "another-owner"
    with pytest.raises(ValueError, match="owner mismatch"):
        session.stop(run)
    provider.owner_id = "acceptance-owner"
    assert session.stop(run)["state"] == "cancelled"
