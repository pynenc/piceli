"""Real injected Kubernetes ApiClient and HTTP acceptance for the local executor."""

import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from piceli.k8s.ops.discovery import (
    DiscoveryFailureKind,
    DiscoveryLimits,
    DiscoveryRequest,
    EvidenceSource,
    ResourceIdentity,
    ResourceType,
    capture_discovery,
)
from piceli.k8s.ops.execution_journal import ExecutionJournal
from piceli.k8s.ops.executor import (
    ActionGrant,
    ExecutionAuthorization,
    ExecutionLimits,
    PlanExecutor,
)
from piceli.k8s.ops.kubernetes_provider import ProviderError
from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    ObservedSnapshot,
    PlanAuthorization,
    PlanOperation,
    ResourceIntent,
    build_plan,
)
from piceli.k8s.ops.revision import DeploymentRevision, ExecutionBundle
from piceli.k8s.ops.secret_versions import SecretVersionStore
from tests.acceptance.fake_api import TARGET, manifest, provider_at


def discover(provider, kinds=None, **limits):
    kinds = kinds or (ResourceType("v1", "ConfigMap"),)
    return capture_discovery(
        provider,
        DiscoveryRequest(TARGET, tuple(kinds), DiscoveryLimits(**limits)),
        capture_id="acceptance-capture",
        captured_at=datetime.now(UTC).isoformat(),
        policy_revision="no-default-masking/v2",
    )


def prepare(provider, desired, *, adopt=(), prune=False, kinds=None):
    intents = tuple(
        item if isinstance(item, ResourceIntent) else ResourceIntent.from_manifest(item)
        for item in desired
    )
    kinds = (
        kinds
        or tuple(
            sorted(
                {ResourceType(item.ref.api_version, item.ref.kind) for item in intents}
            )
        )
        or (ResourceType("v1", "ConfigMap"),)
    )
    snapshot = ObservedSnapshot.from_discovery(discover(provider, kinds))
    plan = build_plan(
        DeploymentComposition((DeploymentComponent("acceptance", intents),)),
        snapshot,
        PlanAuthorization(TARGET, tuple(adopt), prune),
    )
    authorization = ExecutionAuthorization(
        "acceptance-grant",
        TARGET,
        provider.provenance,
        plan.plan_hash,
        snapshot.snapshot_hash,
        provider.field_manager,
        provider.owner_id,
        tuple(ActionGrant.for_action(action) for action in plan.actions),
        (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        cluster_resources=tuple(
            action.resource.ref
            for action in plan.actions
            if not action.resource.ref.namespace
        ),
        compensation_resources=tuple(action.resource.ref for action in plan.actions),
    )
    return plan, snapshot, authorization


def executor(provider, tmp_path, **kwargs):
    directory = tmp_path / "private"
    return PlanExecutor(
        provider,
        ExecutionJournal(directory / "journal.sqlite"),
        SecretVersionStore(directory / "versions.sqlite"),
        **kwargs,
    )


def mutations(api):
    return [
        request
        for request in api.requests
        if request["method"] in {"POST", "PATCH", "DELETE"}
        and request["query"].get("dryRun") != ["All"]
    ]


def crash_after_response(url, directory, plan, snapshot, grant):
    provider = provider_at(url)
    child = executor(
        provider,
        directory,
        after_response=lambda _: os.kill(os.getpid(), signal.SIGKILL),
    )
    child.run("crashed", plan, snapshot, grant)


def crash_bundle_after_response(url, directory, bundle):
    provider = provider_at(url)
    child = executor(
        provider,
        directory,
        after_response=lambda _: os.kill(os.getpid(), signal.SIGKILL),
    )
    child.run_bundle(bundle)


def test_apply_readiness_noop_reapply_and_preview(local_api, tmp_path):
    api, provider = local_api
    run = executor(provider, tmp_path)
    desired = [manifest(), manifest("Deployment", "worker")]
    plan, snapshot, grant = prepare(provider, desired)
    count = len(api.requests)
    assert run.preview(plan)["plan_hash"] == plan.plan_hash
    assert len(api.requests) == count
    assert run.run("first", plan, snapshot, grant)["state"] == "ready"
    assert len(mutations(api)) == 2
    assert any(request["query"].get("dryRun") == ["All"] for request in api.requests)
    plan2, snapshot2, grant2 = prepare(provider, desired)
    assert all(action.operation is PlanOperation.NOOP for action in plan2.actions)
    assert run.run("second", plan2, snapshot2, grant2)["state"] == "ready"
    assert len(mutations(api)) == 2


def test_example_composition_is_pure_and_runs_in_dependency_order(local_api, tmp_path):
    from examples.local_cluster_composition import composition

    api, provider = local_api
    run = executor(provider, tmp_path)
    private = run.secrets.put(TARGET, "bG9jYWwtb25seQ==")
    requests = len(api.requests)
    example = composition(TARGET.namespace, "example.invalid/worker:test", private)
    assert len(api.requests) == requests
    kinds = (ResourceType("v1", "Secret"), ResourceType("apps/v1", "Deployment"))
    snapshot = ObservedSnapshot.from_discovery(discover(provider, kinds))
    requests = len(api.requests)
    plan = build_plan(example, snapshot, PlanAuthorization(TARGET))
    assert len(api.requests) == requests
    grant = ExecutionAuthorization(
        "example",
        TARGET,
        provider.provenance,
        plan.plan_hash,
        snapshot.snapshot_hash,
        provider.field_manager,
        provider.owner_id,
        tuple(ActionGrant.for_action(action) for action in plan.actions),
        (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    )
    assert run.run("example", plan, snapshot, grant)["state"] == "ready"
    assert [request["body"]["kind"] for request in mutations(api)] == [
        "Secret",
        "Deployment",
    ]


@pytest.mark.parametrize("status", [401, 403, 409, 503])
def test_dry_run_rbac_conflict_and_unavailable_never_mutate(
    local_api, tmp_path, status
):
    api, provider = local_api
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [manifest()])
    api.inject("POST", "/configmaps", status=status, dry_run=True)
    assert run.run("denied", plan, snapshot, grant)["state"] == "failed"
    assert mutations(api) == []
    assert "server-password" not in json.dumps(run.journal.summary("denied"))


def test_lost_response_is_observed_then_resumed_without_duplicate_write(
    local_api, tmp_path
):
    api, provider = local_api
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [manifest()])
    api.inject("POST", "/configmaps", disconnect_after=True, dry_run=False)
    first = run.run("lost", plan, snapshot, grant)
    assert first["state"] == "blocked"
    assert first["actions"][0]["state"] == "intent"
    assert run.run("lost", plan, snapshot, grant, resume=True)["state"] == "ready"
    assert len(mutations(api)) == 1


def test_same_owner_update_resumes_by_content_without_replacing_operation_marker(
    local_api, tmp_path
):
    api, provider = local_api
    existing = manifest(value="one")
    existing.setdefault("metadata", {}).setdefault("annotations", {})[
        "piceli.io/operation"
    ] = "prior-release-operation"
    api.put(existing, owned=True)
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [manifest(value="two")])
    api.inject("PATCH", "/configmaps/settings", disconnect_after=True, dry_run=False)

    first = run.run("same-owner-lost", plan, snapshot, grant)
    assert first["state"] == "blocked"
    assert first["failure_category"] == "transport-error"
    row = run.journal.actions("same-owner-lost")[0]
    assert row["state"] == "intent"
    assert row["payload"]["same_owner_update"] is True
    assert (
        "piceli.io/operation"
        not in mutations(api)[0]["body"]["metadata"]["annotations"]
    )

    assert (
        run.run("same-owner-lost", plan, snapshot, grant, resume=True)["state"]
        == "ready"
    )
    assert len(mutations(api)) == 1


def test_ambiguous_absence_blocks_retry(local_api, tmp_path):
    api, provider = local_api
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [manifest()])
    api.inject("POST", "/configmaps", disconnect_before=True, dry_run=False)
    assert run.run("absent", plan, snapshot, grant)["state"] == "blocked"
    assert run.run("absent", plan, snapshot, grant, resume=True)["state"] == "blocked"
    assert len(mutations(api)) == 1


def test_delayed_mutation_is_bounded_and_detached_work_not_retried(local_api, tmp_path):
    api, original_provider = local_api
    provider = provider_at(original_provider.host, request_seconds=0.05)
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [manifest()])
    api.inject("POST", "/configmaps", delay=0.25, dry_run=False)
    start = time.monotonic()
    assert run.run("slow", plan, snapshot, grant)["state"] == "blocked"
    assert time.monotonic() - start < 0.5
    assert run.run("slow", plan, snapshot, grant, resume=True)["state"] == "blocked"
    time.sleep(0.3)
    assert run.run("slow", plan, snapshot, grant, resume=True)["state"] == "ready"
    assert len(mutations(api)) == 1
    provider.client.close()


def test_process_death_after_http_before_receipt_can_resume(local_api, tmp_path):
    api, provider = local_api
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [manifest()])
    process = multiprocessing.get_context("spawn").Process(
        target=crash_after_response,
        args=(provider.host, tmp_path, plan, snapshot, grant),
    )
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        process.join(timeout=2)
        pytest.fail("crash worker did not terminate")
    assert process.exitcode == -signal.SIGKILL
    run.journal.close()
    run.secrets.close()
    reopened = executor(provider, tmp_path)
    assert reopened.journal.actions("crashed")[0]["state"] == "intent"
    assert (
        reopened.run("crashed", plan, snapshot, grant, resume=True)["state"] == "ready"
    )
    assert len(mutations(api)) == 1


def test_execution_bundle_survives_real_process_death_with_stable_private_refs(
    local_api, tmp_path
):
    api, provider = local_api
    run = executor(provider, tmp_path)
    private = run.secrets.put(TARGET, "c2VjcmV0")
    intent = ResourceIntent.from_manifest(
        manifest("Secret", "credentials")
    ).with_secret("/data/password", private)
    plan, snapshot, grant = prepare(provider, [intent])
    bundle = ExecutionBundle.create(
        DeploymentRevision.create(plan, snapshot, grant), execution_id="bundle-crash"
    )
    process = multiprocessing.get_context("spawn").Process(
        target=crash_bundle_after_response,
        args=(provider.host, tmp_path, bundle),
    )
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        process.join(timeout=2)
        pytest.fail("bundle crash worker did not terminate")
    assert process.exitcode == -signal.SIGKILL
    run.journal.close()
    run.secrets.close()
    reopened = executor(provider, tmp_path)
    assert [
        row["operation_id"] for row in reopened.journal.actions("bundle-crash")
    ] == list(bundle.action_ids)
    assert reopened.run_bundle(bundle, resume=True)["state"] == "ready"
    assert len(mutations(api)) == 1


@pytest.mark.parametrize("namespace", ["kube-system", TARGET.namespace])
def test_server_cluster_and_namespace_uid_recreation_rejects_grant(
    local_api, tmp_path, namespace
):
    api, provider = local_api
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [manifest()])
    api.put(manifest("Namespace", namespace), uid="replacement-uid")
    with pytest.raises(ProviderError, match="server-target-identity"):
        run.run("wrong-server", plan, snapshot, grant)
    assert mutations(api) == []


@pytest.mark.parametrize("change", ["recreate", "version", "owner-fields"])
def test_resource_preconditions_fail_before_side_effects(local_api, tmp_path, change):
    api, provider = local_api
    api.put(manifest(), uid="original", owned=True)
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [manifest(value="two")])
    current = api.objects[("ConfigMap", "settings")]
    if change == "recreate":
        current["metadata"]["uid"] = "replacement"
    elif change == "version":
        current["metadata"]["resourceVersion"] = "new-version"
        current["data"]["mode"] = "external-change"
    else:
        current["metadata"]["managedFields"][0]["manager"] = "foreign-manager"
    assert run.run("stale", plan, snapshot, grant)["state"] == "failed"
    assert mutations(api) == []


def test_status_only_resource_version_change_uses_fresh_write_precondition(
    local_api, tmp_path
):
    api, provider = local_api
    api.put(manifest(), uid="original", owned=True)
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [manifest(value="two")])
    current = api.objects[("ConfigMap", "settings")]
    current["metadata"]["resourceVersion"] = "status-version"
    current["status"] = {"observedGeneration": 1}
    current["metadata"].setdefault("annotations", {})[
        "deployment.kubernetes.io/revision"
    ] = "2"

    assert run.run("status-race", plan, snapshot, grant)["state"] == "ready"
    write = mutations(api)[0]
    assert write["body"]["metadata"]["resourceVersion"] == "status-version"


def test_exact_adoption_is_required_and_ssa_uses_uid_version_manager(
    local_api, tmp_path
):
    api, provider = local_api
    api.field_ownership = True
    api.put(manifest(), uid="unmanaged")
    run = executor(provider, tmp_path)
    intent = ResourceIntent.from_manifest(manifest(value="two"))
    with pytest.raises(ValueError, match="adoption"):
        prepare(provider, [intent])
    plan, snapshot, grant = prepare(provider, [intent], adopt=(intent.ref,))
    assert run.run("adopt", plan, snapshot, grant)["state"] == "ready"
    transfer, request = mutations(api)
    assert transfer["content_type"] == "application/merge-patch+json"
    assert request["method"] == "PATCH"
    assert request["body"]["metadata"]["uid"] == "unmanaged"
    # A takeover transfers field ownership and then applies without force.
    assert request["query"] == {
        "fieldManager": [provider.field_manager],
        "force": ["false"],
    }
    assert api.objects[("ConfigMap", "settings")]["data"] == {"mode": "two"}
    writes = len(mutations(api))
    # Compensation restores the adopted object's previous content (force=false).
    run.compensate("adopt", plan, snapshot, grant)
    assert len(mutations(api)) == writes + 1
    assert mutations(api)[-1]["query"]["force"] == ["false"]
    assert api.objects[("ConfigMap", "settings")]["data"] == {"mode": "one"}


def test_private_versions_bind_rotation_without_public_digest_oracle(
    local_api, tmp_path
):
    api, provider = local_api
    run = executor(provider, tmp_path)
    first = run.secrets.put(TARGET, "c2VjcmV0LW9uZQ==")
    second = run.secrets.put(TARGET, "c2VjcmV0LXR3bw==")
    intent = ResourceIntent.from_manifest(
        manifest("Secret", "credentials")
    ).with_secret("/data/password", first)
    plan, snapshot, grant = prepare(provider, [intent])
    changed_intent = ResourceIntent.from_manifest(
        manifest("Secret", "credentials")
    ).with_secret("/data/password", second)
    changed = replace(
        plan, actions=(replace(plan.actions[0], resource=changed_intent),)
    )
    assert plan.plan_hash == changed.plan_hash
    with pytest.raises(ValueError, match="private-version"):
        run.run("rotation", changed, snapshot, grant)
    assert run.run("rotation", plan, snapshot, grant)["state"] == "ready"
    assert (
        api.objects[("Secret", "credentials")]["data"]["password"] == "c2VjcmV0LW9uZQ=="
    )
    public = json.dumps(plan.summary()) + json.dumps(run.journal.summary("rotation"))
    assert first.version not in public
    assert "c2VjcmV0LW9uZQ==" not in public
    assert b"c2VjcmV0LW9uZQ==" not in run.journal.path.read_bytes()
    run.compensate("rotation", plan, snapshot, grant)
    assert ("Secret", "credentials") in api.objects


def test_inline_secrets_and_wrong_target_versions_fail_closed(local_api, tmp_path):
    api, provider = local_api
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [manifest("Secret", "credentials")])
    with pytest.raises(ValueError, match="private version"):
        run.run("inline", plan, snapshot, grant)
    from piceli.k8s.ops.discovery import PlanTarget

    reference = run.secrets.put(
        PlanTarget("other-cluster", TARGET.namespace), "private"
    )
    intent = ResourceIntent.from_manifest(
        manifest("Secret", "credentials")
    ).with_secret("/data/password", reference)
    plan, snapshot, grant = prepare(provider, [intent])
    with pytest.raises(ValueError, match="outside target"):
        run.run("wrong-secret", plan, snapshot, grant)
    assert mutations(api) == []


def test_readiness_timeout_cancel_and_resume_preserve_completed_actions(
    local_api, tmp_path
):
    api, provider = local_api
    api.ready = False
    run = executor(
        provider,
        tmp_path,
        limits=ExecutionLimits(
            max_seconds=2, readiness_seconds=0.15, poll_seconds=0.02
        ),
    )
    plan, snapshot, grant = prepare(provider, [manifest("Deployment", "worker")])
    assert run.run("waiting", plan, snapshot, grant)["state"] == "failed"
    assert run.journal.actions("waiting")[0]["state"] == "applied"
    run.journal.cancel("waiting")
    assert run.run("waiting", plan, snapshot, grant)["state"] == "cancelled"
    api.ready = True
    assert run.run("waiting", plan, snapshot, grant, resume=True)["state"] == "ready"
    assert len(mutations(api)) == 1


def test_namespace_recreated_after_write_cannot_be_reported_ready(local_api, tmp_path):
    api, provider = local_api
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [manifest("Deployment", "worker")])
    run.after_response = lambda _: api.put(
        manifest("Namespace", TARGET.namespace), uid="replacement"
    )
    assert run.run("recreated-during-apply", plan, snapshot, grant)["state"] == "failed"
    assert run.journal.actions("recreated-during-apply")[0]["state"] == "applied"
    assert len(mutations(api)) == 1


@pytest.mark.parametrize(
    "create", [True, False], ids=["delete-created", "restore-updated"]
)
def test_compensation_lost_response_resumes_by_observation(local_api, tmp_path, create):
    api, provider = local_api
    if not create:
        api.put(manifest(), owned=True)
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [manifest(value="two")])
    assert run.run("undo-lost", plan, snapshot, grant)["state"] == "ready"
    api.inject(
        "DELETE" if create else "PATCH",
        "/configmaps/settings",
        disconnect_after=True,
        dry_run=False,
    )
    with pytest.raises(ProviderError):
        run.compensate("undo-lost", plan, snapshot, grant)
    assert run.journal.actions("undo-lost")[0]["state"] == "compensating"
    reopened = executor(provider, tmp_path)
    assert (
        reopened.compensate("undo-lost", plan, snapshot, grant)["state"]
        == "compensated-with-retention"
    )
    assert len(mutations(api)) == 2
    if create:
        assert ("ConfigMap", "settings") not in api.objects
    else:
        assert api.objects[("ConfigMap", "settings")]["data"]["mode"] == "one"


def test_compensation_read_capture_shares_execution_deadline(local_api, tmp_path):
    api, provider = local_api
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [manifest()])
    assert run.run("undo-timeout", plan, snapshot, grant)["state"] == "ready"
    run.limits = ExecutionLimits(
        max_seconds=0.05, readiness_seconds=0.02, poll_seconds=0.01
    )
    api.inject("GET", "/configmaps", delay=0.3)
    start = time.monotonic()
    with pytest.raises((ProviderError, ValueError, TimeoutError)):
        run.compensate("undo-timeout", plan, snapshot, grant)
    assert time.monotonic() - start < 0.25
    assert len(mutations(api)) == 1
    time.sleep(0.35)


def test_cold_api_discovery_obeys_read_deadline(local_api):
    api, provider = local_api
    api.inject("GET", "/api/v1", delay=0.3)
    start = time.monotonic()
    with pytest.raises(ProviderError, match="deadline"):
        provider.get(
            ResourceIdentity("v1", "ConfigMap", TARGET.namespace, "settings"),
            deadline=start + 0.04,
        )
    assert time.monotonic() - start < 0.2
    time.sleep(0.35)


def test_cancellation_between_actions_survives_reopen(local_api, tmp_path):
    api, provider = local_api
    run = executor(provider, tmp_path)
    run.after_response = lambda _: run.journal.cancel("cancel")
    plan, snapshot, grant = prepare(provider, [manifest(name="a"), manifest(name="b")])
    assert run.run("cancel", plan, snapshot, grant)["state"] == "cancelled"
    assert len(mutations(api)) == 1
    reopened = executor(provider, tmp_path)
    assert reopened.run("cancel", plan, snapshot, grant)["state"] == "cancelled"
    assert (
        reopened.run("cancel", plan, snapshot, grant, resume=True)["state"] == "ready"
    )
    assert len(mutations(api)) == 2


def test_compensation_restores_owned_update_and_removes_only_owned_create(
    local_api, tmp_path
):
    api, provider = local_api
    api.put(manifest(name="existing"), owned=True)
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(
        provider, [manifest(name="existing", value="two"), manifest(name="new")]
    )
    assert run.run("undo", plan, snapshot, grant)["state"] == "ready"
    result = run.compensate("undo", plan, snapshot, grant)
    assert result["state"] == "compensated-with-retention"
    assert api.objects[("ConfigMap", "existing")]["data"]["mode"] == "one"
    assert ("ConfigMap", "new") not in api.objects
    assert mutations(api)[-2]["body"]["propagationPolicy"] == "Orphan"


@pytest.mark.parametrize(
    "unsafe", ["recreated", "retained-child", "partial", "unscoped"]
)
def test_unsafe_compensation_fails_closed(local_api, tmp_path, unsafe):
    api, provider = local_api
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [manifest()])
    assert run.run("unsafe", plan, snapshot, grant)["state"] == "ready"
    if unsafe == "recreated":
        api.objects[("ConfigMap", "settings")]["metadata"]["uid"] = "replacement"
    elif unsafe == "retained-child":
        child = manifest(name="child")
        child["metadata"]["annotations"] = {"piceli.io/retained": "true"}
        child["metadata"]["ownerReferences"] = [
            {"uid": api.objects[("ConfigMap", "settings")]["metadata"]["uid"]}
        ]
        api.put(child, owned=True)
    elif unsafe == "partial":
        api.inject("GET", "/configmaps", status=403)
    else:
        grant = replace(grant, compensation_resources=())
    with pytest.raises((ValueError, ProviderError)):
        run.compensate("unsafe", plan, snapshot, grant)
    assert len(mutations(api)) == 1


def test_prune_is_conditional_orphan_delete_and_retention_is_permanent(
    local_api, tmp_path
):
    api, provider = local_api
    api.put(manifest(), owned=True)
    api.put(manifest("PersistentVolumeClaim", "disk"), owned=True)
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(
        provider,
        [],
        prune=True,
        kinds=(
            ResourceType("v1", "ConfigMap"),
            ResourceType("v1", "PersistentVolumeClaim"),
        ),
    )
    assert len(plan.protected_resources) == 1
    assert run.run("prune", plan, snapshot, grant)["state"] == "ready"
    assert ("PersistentVolumeClaim", "disk") in api.objects
    assert len(mutations(api)) == 1
    with pytest.raises(ProviderError, match="retained"):
        provider.delete(
            ResourceIdentity("v1", "PersistentVolumeClaim", TARGET.namespace, "disk"),
            uid="any",
            resource_version="1",
        )


def test_wffc_claim_submits_its_consumer_before_strict_bind_readiness(
    local_api, tmp_path
):
    api, provider = local_api
    api.wait_for_first_consumer.add("state")
    claim = manifest("PersistentVolumeClaim", "state")
    worker = manifest("Deployment", "worker")
    worker["spec"]["template"]["spec"]["volumes"] = [
        {"name": "state", "persistentVolumeClaim": {"claimName": "state"}}
    ]
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [claim, worker])

    assert run.run("wffc", plan, snapshot, grant)["state"] == "ready"
    persisted = [request["body"]["kind"] for request in mutations(api)]
    assert persisted == ["PersistentVolumeClaim", "Deployment"]
    assert api.objects[("PersistentVolumeClaim", "state")]["status"]["phase"] == "Bound"


def test_retained_private_revision_resumes_without_rewrite_or_private_leak(
    local_api, tmp_path
):
    api, provider = local_api
    existing = manifest("Secret", "credentials", value="c2VjcmV0")
    existing["metadata"]["annotations"] = {"piceli.io/operation": "previous-operation"}
    api.put(existing, owned=True)
    run = executor(provider, tmp_path)
    private = run.secrets.put(TARGET, "c2VjcmV0")
    intent = ResourceIntent.from_manifest(
        manifest("Secret", "credentials")
    ).with_secret("/data/password", private)
    plan, snapshot, grant = prepare(provider, [intent])
    revision = DeploymentRevision.create(plan, snapshot, grant)
    bundle = ExecutionBundle.create(revision, execution_id="retained-secret")

    # This represents a process death after durable intent, before the receipt.
    run.journal.start(
        bundle.execution_id, bundle.journal_binding(), list(bundle.action_ids)
    )
    run.journal.record(bundle.execution_id, 0, "intent", {})
    assert run.run_bundle(bundle, resume=True)["state"] == "ready"

    current = api.objects[("Secret", "credentials")]
    assert (
        current["metadata"]["annotations"]["piceli.io/operation"]
        == "previous-operation"
    )
    assert mutations(api) == []
    public = bundle.to_json() + json.dumps(run.journal.summary(bundle.execution_id))
    assert "c2VjcmV0" not in public
    assert private.store_id in public
    assert b"c2VjcmV0" not in run.journal.path.read_bytes()
    restored = DeploymentRevision.from_json(
        revision.to_json(), plan=plan, snapshot=snapshot, authorization=grant
    )
    assert ExecutionBundle.from_json(bundle.to_json(), revision=restored) == bundle


def test_retained_owner_or_generation_drift_blocks_without_force_apply(
    local_api, tmp_path
):
    api, provider = local_api
    existing = manifest("Secret", "credentials", value="c2VjcmV0")
    api.put(existing, owned=True)
    run = executor(provider, tmp_path)
    private = run.secrets.put(TARGET, "c2VjcmV0")
    intent = ResourceIntent.from_manifest(
        manifest("Secret", "credentials")
    ).with_secret("/data/password", private)
    plan, snapshot, grant = prepare(provider, [intent])
    bundle = ExecutionBundle.create(
        DeploymentRevision.create(plan, snapshot, grant), execution_id="retained-drift"
    )

    api.objects[("Secret", "credentials")]["metadata"]["generation"] += 1
    assert run.run_bundle(bundle)["state"] == "failed"
    assert mutations(api) == []

    # A fresh snapshot sees the generation but rejects a resource no longer in
    # this ownership domain, rather than applying with a new operation marker.
    plan, snapshot, grant = prepare(provider, [intent])
    bundle = ExecutionBundle.create(
        DeploymentRevision.create(plan, snapshot, grant), execution_id="owner-conflict"
    )
    api.objects[("Secret", "credentials")]["metadata"]["annotations"] = {}
    assert run.run_bundle(bundle)["state"] == "failed"
    assert mutations(api) == []


def test_retained_operation_content_conflict_never_force_applies(local_api, tmp_path):
    api, provider = local_api
    existing = manifest("Secret", "credentials", value="b2xk")
    existing["metadata"]["annotations"] = {"piceli.io/operation": "old-operation"}
    api.put(existing, owned=True)
    run = executor(provider, tmp_path)
    private = run.secrets.put(TARGET, "c2VjcmV0")
    intent = ResourceIntent.from_manifest(
        manifest("Secret", "credentials")
    ).with_secret("/data/password", private)
    plan, snapshot, grant = prepare(provider, [intent])
    bundle = ExecutionBundle.create(
        DeploymentRevision.create(plan, snapshot, grant),
        execution_id="operation-conflict",
    )

    report = run.run_bundle(bundle)
    assert report["state"] == "failed"
    assert report["failure_category"] == "retained-content-precondition-failed"
    assert (
        run.journal.summary(bundle.execution_id)["failure_category"]
        == "retained-content-precondition-failed"
    )
    assert mutations(api) == []


def test_revision_regrant_requires_explicit_adoption_and_preserves_action_ids(
    local_api, tmp_path
):
    _, provider = local_api
    run = executor(provider, tmp_path)
    private = run.secrets.put(TARGET, "c2VjcmV0")
    intent = ResourceIntent.from_manifest(
        manifest("Secret", "credentials")
    ).with_secret("/data/password", private)
    plan, snapshot, grant = prepare(provider, [intent])
    bundle = ExecutionBundle.create(DeploymentRevision.create(plan, snapshot, grant))
    renewed = replace(grant, authorization_id="renewed")
    with pytest.raises(ValueError, match="explicitly adopt"):
        bundle.with_authorization(renewed)
    adopted = bundle.with_authorization(
        replace(renewed, resume_revision_id=bundle.revision.revision_id)
    )
    assert adopted.action_ids == bundle.action_ids


@pytest.mark.parametrize(
    "change",
    ["actions", "owner", "manager", "expired", "endpoint", "synthetic", "incomplete"],
)
def test_execution_authorization_is_exact_and_fail_closed(local_api, tmp_path, change):
    api, provider = local_api
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [manifest()])
    with pytest.raises(ValueError):
        if change == "actions":
            grant = replace(grant, actions=(), compensation_resources=())
        elif change == "owner":
            grant = replace(grant, owner_id="other")
        elif change == "manager":
            grant = replace(grant, field_manager="other")
        elif change == "expired":
            grant = replace(
                grant,
                expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            )
        elif change == "endpoint":
            grant = replace(
                grant, provenance=replace(grant.provenance, endpoint_id="other")
            )
        elif change == "synthetic":
            grant = replace(
                grant,
                provenance=replace(grant.provenance, source=EvidenceSource.SYNTHETIC),
            )
        else:
            snapshot = replace(snapshot, discovery=None)
        run.run("bad-grant", plan, snapshot, grant)
    assert mutations(api) == []


def test_partial_lists_and_continuation_budgets(local_api):
    api, provider = local_api
    for name in ("a", "b", "c"):
        api.put(manifest(name=name))
    artifact = discover(provider, page_size=1, max_pages=2)
    assert not artifact.execution_authoritative
    assert len(artifact.resources) == 2
    assert artifact.coverage.failures[0].kind is DiscoveryFailureKind.LIMIT_EXCEEDED
    api.inject("GET", "/configmaps", status=403)
    assert (
        discover(provider).coverage.failures[0].kind is DiscoveryFailureKind.RBAC_DENIED
    )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"items": [], "items": []}',
        b'{"items": NaN}',
        b"[]",
        b"broken",
        b'{"apiVersion": 1}',
        b"x" * 1100000,
    ],
    ids=[
        "duplicate-key",
        "nan",
        "array",
        "invalid-json",
        "invalid-version",
        "oversized",
    ],
)
def test_malformed_or_oversized_http_json_never_proves_absence(local_api, raw):
    api, provider = local_api
    api.inject("GET", "/configmaps", raw=raw)
    artifact = discover(provider)
    assert not artifact.execution_authoritative
    assert not artifact.coverage.complete


def test_expired_deadline_never_sends_http(local_api):
    api, provider = local_api
    count = len(api.requests)
    with pytest.raises(ProviderError, match="deadline"):
        provider._request("GET", "/api/v1", deadline=time.monotonic() - 1)
    assert len(api.requests) == count


def test_actual_provider_artifact_matches_published_schema(local_api):
    from pathlib import Path

    from jsonschema import Draft202012Validator, FormatChecker

    from piceli.k8s.ops.discovery import DiscoveryArtifact

    api, provider = local_api
    api.put(manifest(), owned=True)
    artifact = discover(provider)
    schema = json.loads(
        Path("docs/schemas/piceli-discovery-v2.schema.json").read_text()
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(
        artifact.to_dict()
    )
    assert artifact.resources[0].retained is False
    assert DiscoveryArtifact.from_json(artifact.to_json()) == artifact


def test_fresh_process_import_and_planning_do_not_load_kubeconfig():
    script = """
import sys, types
class Trap(types.ModuleType):
    def __getattr__(self, name):
        raise AssertionError(name)
sys.modules['kubernetes'] = Trap('kubernetes')
import piceli.k8s.ops.plan
import piceli.k8s.ops.discovery
import piceli.k8s.ops.executor
"""
    subprocess.run([sys.executable, "-c", script], check=True)


def test_journal_excludes_parallel_operator_and_enforces_capacity(local_api, tmp_path):
    _, provider = local_api
    run = executor(provider, tmp_path)
    other = ExecutionJournal(run.journal.path)
    with (
        run.journal.exclusive(),
        pytest.raises(ValueError, match="already executing"),
        other.exclusive(),
    ):
        pass
    run.journal.max_bytes = 1
    plan, snapshot, grant = prepare(provider, [manifest()])
    with pytest.raises(ValueError, match="byte budget"):
        run.run("full", plan, snapshot, grant)


def test_secret_store_permissions_reopen_and_scope(tmp_path):
    directory = tmp_path / "private"
    store = SecretVersionStore(directory / "versions.sqlite")
    reference = store.put(TARGET, {"password": "private-secret"})
    store.close()
    reopened = SecretVersionStore(directory / "versions.sqlite")
    assert reopened.resolve(TARGET, reference) == {"password": "private-secret"}
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "versions.sqlite").stat().st_mode & 0o777 == 0o600
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="permissions"):
        SecretVersionStore(unsafe / "versions.sqlite")


def test_progress_reports_applying_and_readiness_waits(local_api, tmp_path):
    """Human progress while applying and waiting (B12); no values, only names."""
    api, provider = local_api
    api.ready = False
    lines: list[str] = []
    run = executor(
        provider,
        tmp_path,
        limits=ExecutionLimits(max_seconds=2, readiness_seconds=0.3, poll_seconds=0.02),
        progress=lines.append,
        progress_seconds=0.1,
    )
    plan, snapshot, grant = prepare(
        provider, [manifest(), manifest("Deployment", "worker")]
    )
    assert run.run("slow", plan, snapshot, grant)["state"] == "failed"
    assert lines[0].startswith("applying 1/2: ")
    waits = [line for line in lines if line.startswith("waiting for Deployment/worker")]
    assert waits and waits[0].endswith("to be ready (0s)")
    # Throttled: at most one line per interval while waiting on the same object.
    assert len(waits) <= 0.3 / 0.1 + 2
    assert not any("ConfigMap" in line and "waiting" in line for line in lines)

    def broken(_line: str) -> None:
        raise RuntimeError("terminal gone")

    api.ready = True
    quiet = executor(provider, tmp_path / "again", progress=broken)
    plan, snapshot, grant = prepare(provider, [manifest("Deployment", "other")])
    assert quiet.run("ok", plan, snapshot, grant)["state"] == "ready"
