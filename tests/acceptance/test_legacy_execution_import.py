"""Fail-closed import acceptance using the disposable local Kubernetes API."""

from __future__ import annotations

import json

import pytest

from piceli.k8s.ops.execution_journal import ExecutionJournal
from piceli.k8s.ops.executor import PlanExecutor
from piceli.k8s.ops.legacy_execution import (
    LegacyEvidenceInsufficient,
    LegacyExecutionArchive,
    import_legacy_execution,
)
from piceli.k8s.ops.plan import ResourceIntent
from piceli.k8s.ops.revision import DeploymentRevision
from tests.acceptance.fake_api import TARGET, manifest
from tests.acceptance.test_local_executor import executor, mutations, prepare


def source_execution(provider, tmp_path, desired, *, execution_id="legacy"):
    source = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, desired)
    revision = DeploymentRevision.create(plan, snapshot, grant)
    assert source.run(execution_id, plan, snapshot, grant)["state"] in {
        "ready",
        "failed",
    }
    return source, LegacyExecutionArchive.from_revision(revision)


def import_into(source, archive, tmp_path):
    destination = ExecutionJournal(tmp_path / "migrated" / "journal.sqlite")
    imported = import_legacy_execution(
        legacy_journal=source.journal,
        legacy_execution_id="legacy",
        archive=archive,
        journal=destination,
        secrets=source.secrets,
    )
    return destination, imported


def test_import_preserves_exact_receipts_and_resumes_without_provider_writes(
    local_api, tmp_path
):
    api, provider = local_api
    source, archive = source_execution(provider, tmp_path, [manifest()])
    source_before = source.journal.export_execution("legacy")
    before = len(mutations(api))

    destination, imported = import_into(source, archive, tmp_path)

    assert imported.bundle.execution_id == "legacy"
    assert [row["operation_id"] for row in destination.actions("legacy")] == [
        row["operation_id"] for row in source.journal.actions("legacy")
    ]
    assert destination.actions("legacy") == source.journal.actions("legacy")
    assert source.journal.export_execution("legacy") == source_before
    assert len(mutations(api)) == before
    resumed = PlanExecutor(provider, destination, source.secrets)
    assert resumed.run_bundle(imported.bundle, resume=True)["state"] == "ready"
    assert len(mutations(api)) == before
    assert imported.lineage == destination.migration_lineage("legacy")


def _mutate_archive(
    case: str, archive: LegacyExecutionArchive
) -> LegacyExecutionArchive:
    value = archive.to_dict()
    if case == "hash":
        value["plan"]["plan_hash"] = "0" * 64
    elif case == "target":
        value["plan"]["target"]["cluster_id"] = "other-cluster"
    elif case == "ownership":
        value["snapshot"]["resources"][0]["ownership"] = "unmanaged"
    elif case == "scope":
        value["authorization"]["actions"][0]["operation"] = "delete"
    elif case == "coverage":
        value["discovery"]["coverage"]["completed"] = []
    elif case == "defaults":
        value["snapshot"]["defaulted_fields"] = [
            {
                "resource": value["snapshot"]["resources"][0]["resource"],
                "json_pointer": "/data/mode",
            }
        ]
    elif case == "manifest":
        value["plan"]["actions"][0]["manifest"]["data"]["mode"] = "changed"
    elif case == "expired":
        value["authorization"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    elif case == "grant":
        value["authorization"]["owner_id"] = "other-owner"
    else:
        raise AssertionError(case)
    return LegacyExecutionArchive.from_dict(value)


@pytest.mark.parametrize(
    "case",
    [
        "hash",
        "target",
        "ownership",
        "scope",
        "coverage",
        "defaults",
        "manifest",
        "expired",
        "grant",
    ],
)
def test_import_rejects_inconsistent_archive_evidence_without_writes(
    local_api, tmp_path, case
):
    api, provider = local_api
    api.put(manifest(), owned=True)
    source, archive = source_execution(provider, tmp_path, [manifest(value="two")])
    before = len(mutations(api))
    destination = ExecutionJournal(tmp_path / "migrated" / "journal.sqlite")

    with pytest.raises(LegacyEvidenceInsufficient) as error:
        import_legacy_execution(
            legacy_journal=source.journal,
            legacy_execution_id="legacy",
            archive=_mutate_archive(case, archive),
            journal=destination,
            secrets=source.secrets,
        )

    assert error.value.code
    assert len(mutations(api)) == before
    with pytest.raises(ValueError, match="unknown execution"):
        destination.summary("legacy")


def test_import_rejects_missing_private_ref_and_ambiguous_legacy_state(
    local_api, tmp_path
):
    api, provider = local_api
    source = executor(provider, tmp_path)
    private = source.secrets.put(TARGET, "c2VjcmV0")
    intent = ResourceIntent.from_manifest(
        manifest("Secret", "credentials")
    ).with_secret("/data/password", private)
    plan, snapshot, grant = prepare(provider, [intent])
    revision = DeploymentRevision.create(plan, snapshot, grant)
    assert source.run("legacy", plan, snapshot, grant)["state"] == "ready"
    archive = LegacyExecutionArchive.from_revision(revision)
    before = len(mutations(api))

    missing = archive.to_dict()
    missing["private_references"] = []
    with pytest.raises(LegacyEvidenceInsufficient, match="private-reference"):
        import_legacy_execution(
            legacy_journal=source.journal,
            legacy_execution_id="legacy",
            archive=LegacyExecutionArchive.from_dict(missing),
            journal=ExecutionJournal(tmp_path / "missing" / "journal.sqlite"),
            secrets=source.secrets,
        )

    source.journal.record(
        "legacy", 0, "intent", source.journal.actions("legacy")[0]["payload"]
    )
    with pytest.raises(LegacyEvidenceInsufficient):
        import_legacy_execution(
            legacy_journal=source.journal,
            legacy_execution_id="legacy",
            archive=archive,
            journal=ExecutionJournal(tmp_path / "ambiguous" / "journal.sqlite"),
            secrets=source.secrets,
        )
    assert len(mutations(api)) == before


def test_import_resumes_wffc_after_consumer_submission(local_api, tmp_path):
    api, provider = local_api
    api.wait_for_first_consumer.add("state")
    claim = manifest("PersistentVolumeClaim", "state")
    worker = manifest("Deployment", "worker")
    worker["spec"]["template"]["spec"]["volumes"] = [
        {"name": "state", "persistentVolumeClaim": {"claimName": "state"}}
    ]
    api.ready = False
    source, archive = source_execution(provider, tmp_path, [claim, worker])
    assert source.journal.summary("legacy")["state"] == "failed"
    before = len(mutations(api))
    destination, imported = import_into(source, archive, tmp_path)
    api.ready = True

    assert (
        PlanExecutor(provider, destination, source.secrets).run_bundle(
            imported.bundle, resume=True
        )["state"]
        == "ready"
    )
    assert len(mutations(api)) == before


@pytest.mark.parametrize(
    "kind,name", [("Secret", "credentials"), ("PersistentVolumeClaim", "state")]
)
def test_imported_retained_resources_reconcile_without_force_apply(
    local_api, tmp_path, kind, name
):
    api, provider = local_api
    existing = manifest(kind, name, value="c2VjcmV0")
    api.put(existing, owned=True)
    if kind == "Secret":
        source = executor(provider, tmp_path)
        private = source.secrets.put(TARGET, "c2VjcmV0")
        desired = [
            ResourceIntent.from_manifest(manifest(kind, name)).with_secret(
                "/data/password", private
            )
        ]
        plan, snapshot, grant = prepare(provider, desired)
        revision = DeploymentRevision.create(plan, snapshot, grant)
        assert source.run("legacy", plan, snapshot, grant)["state"] == "ready"
        archive = LegacyExecutionArchive.from_revision(revision)
    else:
        source, archive = source_execution(provider, tmp_path, [manifest(kind, name)])
    before = len(mutations(api))
    destination, imported = import_into(source, archive, tmp_path)

    assert (
        PlanExecutor(provider, destination, source.secrets).run_bundle(
            imported.bundle, resume=True
        )["state"]
        == "ready"
    )
    assert len(mutations(api)) == before
    assert all(request["query"].get("force") != ["true"] for request in api.requests)


def test_import_report_and_archive_do_not_disclose_secret_values(local_api, tmp_path):
    _, provider = local_api
    source = executor(provider, tmp_path)
    secret = "c2VjcmV0LXZhbHVl"
    private = source.secrets.put(TARGET, secret)
    intent = ResourceIntent.from_manifest(
        manifest("Secret", "credentials")
    ).with_secret("/data/password", private)
    plan, snapshot, grant = prepare(provider, [intent])
    revision = DeploymentRevision.create(plan, snapshot, grant)
    assert source.run("legacy", plan, snapshot, grant)["state"] == "ready"
    archive = LegacyExecutionArchive.from_revision(revision)
    _, imported = import_into(source, archive, tmp_path)

    assert secret not in archive.to_json()
    assert secret not in json.dumps(imported.report())
    assert private.store_id not in json.dumps(imported.report())
