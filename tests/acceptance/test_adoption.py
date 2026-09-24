"""Acceptance: adoption by ownership transfer (metadata-only and takeover).

The fake API runs with its field-ownership model enabled, so server-side apply
conflicts, pruning and ``managedFields`` replacement behave like a real
API server for the paths exercised here.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from piceli.k8s.ops.discovery import (
    DiscoveryLimits,
    DiscoveryRequest,
    ResourceIdentity,
    ResourceType,
    capture_discovery,
)
from piceli.k8s.ops.execution_journal import ExecutionJournal
from piceli.k8s.ops.executor import (
    ActionGrant,
    ExecutionAuthorization,
    PlanExecutor,
)
from piceli.k8s.ops.kubernetes_provider import ProviderError
from piceli.k8s.ops.plan import (
    Adoption,
    AdoptionMode,
    DeploymentComponent,
    DeploymentComposition,
    ObservedSnapshot,
    PlanAuthorization,
    PlanOperation,
    ResourceIntent,
    build_plan,
    field_drift,
)
from piceli.k8s.ops.secret_versions import SecretVersionStore
from tests.acceptance.fake_api import TARGET, manifest, provider_at, serve

MANAGER = "piceli-acceptance"
RESTARTED = "kubectl.kubernetes.io/restartedAt"


@pytest.fixture
def api_url():
    with serve() as (api, url):
        api.field_ownership = True
        yield api, url


@pytest.fixture
def provider(api_url):
    _, url = api_url
    value = provider_at(url, inherited_owner_ids=("previous-owner",))
    yield value
    value.client.close()


def mutations(api):
    return [
        request
        for request in api.requests
        if request["method"] in {"POST", "PATCH", "DELETE"}
        and request["query"].get("dryRun") != ["All"]
    ]


def forced(api):
    return [r for r in api.requests if r["query"].get("force") == ["true"]]


def kinds_of(intents):
    return tuple(
        sorted({ResourceType(item.ref.api_version, item.ref.kind) for item in intents})
    )


def prepare(
    provider, desired, *, adopt=(), inherited=(), grant_inherited=(), replace_refs=()
):
    intents = tuple(
        item if isinstance(item, ResourceIntent) else ResourceIntent.from_manifest(item)
        for item in desired
    )
    snapshot = ObservedSnapshot.from_discovery(
        capture_discovery(
            provider,
            DiscoveryRequest(TARGET, kinds_of(intents), DiscoveryLimits()),
            capture_id="adoption-capture",
            captured_at=datetime.now(UTC).isoformat(),
            policy_revision="adoption/v1",
        )
    )
    composition = DeploymentComposition((DeploymentComponent("app", intents),))
    plan = build_plan(
        composition,
        snapshot,
        PlanAuthorization(
            TARGET,
            tuple(adopt),
            inherited_owner_ids=tuple(inherited),
            replace_resources=tuple(replace_refs),
        ),
    )
    return plan, snapshot, grant(provider, plan, snapshot, grant_inherited)


def grant(provider, plan, snapshot, inherited=()):
    return ExecutionAuthorization(
        "adoption-grant",
        TARGET,
        provider.provenance,
        plan.plan_hash,
        snapshot.snapshot_hash,
        provider.field_manager,
        provider.owner_id,
        tuple(ActionGrant.for_action(action) for action in plan.actions),
        (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        compensation_resources=tuple(action.resource.ref for action in plan.actions),
        inherited_owner_ids=tuple(inherited),
    )


def executor(provider, tmp_path, **kwargs):
    directory = tmp_path / "private"
    return PlanExecutor(
        provider,
        ExecutionJournal(directory / "journal.sqlite"),
        SecretVersionStore(directory / "versions.sqlite"),
        **kwargs,
    )


def pvc(name="data", storage="1Gi", **metadata):
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": TARGET.namespace, **metadata},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": storage}},
        },
    }


def live_pvc(api, *, owner=None):
    value = pvc()
    value["spec"]["volumeName"] = "pv-0001"
    value["spec"]["storageClassName"] = "standard"
    if owner:
        value["metadata"]["annotations"] = {"piceli.io/owner": owner}
    return api.put(value, managers=[("kubectl-client-side-apply", "Update", value)])


def kubectl_deployment(api, *, image="example.invalid/worker:old"):
    """A Deployment written by kubectl apply, then set image, then rollout restart."""
    value = manifest("Deployment", "worker")
    value["metadata"]["annotations"] = {
        "kubectl.kubernetes.io/last-applied-configuration": "{}"
    }
    containers = value["spec"]["template"]["spec"]["containers"]
    containers[0]["image"] = image
    value["spec"]["template"]["metadata"]["annotations"] = {RESTARTED: "2026-09-24"}
    applied = copy.deepcopy(value)
    applied["spec"]["template"]["metadata"].pop("annotations")
    applied["spec"]["template"]["spec"]["containers"][0].pop("image")
    set_image = {
        "spec": {
            "template": {"spec": {"containers": [{"name": "worker", "image": image}]}}
        }
    }
    restart = {
        "spec": {"template": {"metadata": {"annotations": {RESTARTED: "2026-09-24"}}}}
    }
    return api.put(
        value,
        managers=[
            ("kubectl-client-side-apply", "Update", applied),
            ("kubectl-set", "Update", set_image),
            ("kubectl-rollout", "Update", restart),
        ],
    )


def desired_deployment(image="example.invalid/worker:new"):
    value = manifest("Deployment", "worker")
    value["spec"]["template"]["spec"]["containers"][0]["image"] = image
    return ResourceIntent.from_manifest(value)


def payloads(run, execution):
    return [row["payload"] for row in run.journal.actions(execution)]


# --------------------------------------------------------------- retained


def test_unowned_pvc_adoption_changes_only_metadata_annotations(
    api_url, provider, tmp_path
):
    api, _ = api_url
    before = live_pvc(api)
    intent = ResourceIntent.from_manifest(pvc())
    with pytest.raises(ValueError, match="explicit adoption"):
        prepare(provider, [intent])
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    action = plan.actions[0]
    assert action.operation is PlanOperation.ADOPT
    assert action.adoption == Adoption(AdoptionMode.METADATA_ONLY, None)
    assert action.summary()["adoption"] == {
        "mode": "metadata-only",
        "previous_owner": None,
        "transferred_managers": [],
    }

    run = executor(provider, tmp_path)
    assert run.run("adopt-pvc", plan, snapshot, authorization)["state"] == "ready"

    [write] = mutations(api)
    assert write["method"] == "PATCH"
    assert write["content_type"] == "application/merge-patch+json"
    assert set(write["body"]) == {"metadata"}
    assert set(write["body"]["metadata"]) == {"uid", "resourceVersion", "annotations"}
    assert set(write["body"]["metadata"]["annotations"]) == {
        "piceli.io/owner",
        "piceli.io/operation",
    }
    assert forced(api) == []
    after = api.objects[("PersistentVolumeClaim", "data")]
    assert after["spec"] == before["spec"]
    assert after["metadata"]["annotations"]["piceli.io/owner"] == provider.owner_id
    assert {k: v for k, v in after["metadata"].items() if k != "annotations"} | {
        "resourceVersion": None,
        "managedFields": None,
    } == {k: v for k, v in before["metadata"].items() if k != "annotations"} | {
        "resourceVersion": None,
        "managedFields": None,
    }
    assert payloads(run, "adopt-pvc")[0]["adoption"] == {
        "mode": "metadata-only",
        "previous_owner": None,
    }


def test_inherited_owner_pvc_adoption_restamps_only_the_owner(
    api_url, provider, tmp_path
):
    api, _ = api_url
    before = live_pvc(api, owner="previous-owner")
    intent = ResourceIntent.from_manifest(pvc())
    # Without the inherited-owner authorization the object is not adoptable.
    with pytest.raises(ValueError, match="adoption authorization does not match"):
        prepare(provider, [intent], adopt=(intent.ref,))
    plan, snapshot, authorization = prepare(
        provider,
        [intent],
        adopt=(intent.ref,),
        inherited=("previous-owner",),
        grant_inherited=("previous-owner",),
    )
    assert plan.actions[0].adoption == Adoption(
        AdoptionMode.METADATA_ONLY, "previous-owner"
    )
    run = executor(provider, tmp_path)
    assert run.run("restamp", plan, snapshot, authorization)["state"] == "ready"
    after = api.objects[("PersistentVolumeClaim", "data")]
    assert after["spec"] == before["spec"]
    assert after["metadata"]["annotations"]["piceli.io/owner"] == provider.owner_id
    assert forced(api) == []


def test_inherited_owner_grant_is_honoured_for_retained_objects(
    api_url, provider, tmp_path
):
    api, _ = api_url
    live_pvc(api, owner="previous-owner")
    intent = ResourceIntent.from_manifest(pvc())
    plan, snapshot, authorization = prepare(
        provider, [intent], grant_inherited=("previous-owner",)
    )
    assert plan.actions[0].operation in {PlanOperation.APPLY, PlanOperation.NOOP}
    run = executor(provider, tmp_path)
    assert run.run("honoured", plan, snapshot, authorization)["state"] == "ready"
    assert mutations(api) == []
    # A provider-only inheritance (no grant) keeps the old, exact-owner rule.
    plan, snapshot, authorization = prepare(provider, [intent])
    report = executor(provider, tmp_path / "other").run(
        "ungranted", plan, snapshot, authorization
    )
    assert report["failure_category"] == "ownership-precondition-failed"
    assert mutations(api) == []


def test_inherited_owner_grant_cannot_exceed_the_provider(api_url, provider, tmp_path):
    api, _ = api_url
    live_pvc(api, owner="previous-owner")
    intent = ResourceIntent.from_manifest(pvc())
    plan, snapshot, authorization = prepare(
        provider, [intent], grant_inherited=("someone-else",)
    )
    with pytest.raises(ValueError, match="inherited owner grant"):
        executor(provider, tmp_path).run("wide", plan, snapshot, authorization)
    assert mutations(api) == []


def test_retained_adoption_refuses_a_spec_difference(api_url, provider, tmp_path):
    api, _ = api_url
    live_pvc(api)
    intent = ResourceIntent.from_manifest(pvc(storage="2Gi"))
    with pytest.raises(ValueError, match="already contains the desired manifest"):
        prepare(provider, [intent], adopt=(intent.ref,))
    # Labels and annotations are metadata: they may differ (see below).
    labelled = ResourceIntent.from_manifest(pvc(labels={"tier": "db"}))
    plan, _, _ = prepare(provider, [labelled], adopt=(labelled.ref,))
    assert plan.actions[0].adoption == Adoption(
        AdoptionMode.METADATA_ONLY, None, metadata_changes=("labels/tier",)
    )
    assert mutations(api) == []


def metadata_write(api):
    [write] = mutations(api)
    assert write["method"] == "PATCH"
    assert write["content_type"] == "application/merge-patch+json"
    assert set(write["body"]) == {"metadata"}
    return write["body"]["metadata"]


def test_retained_adoption_with_a_metadata_difference_writes_only_metadata(
    api_url, provider, tmp_path
):
    api, _ = api_url
    before = live_pvc(api)
    intent = ResourceIntent.from_manifest(
        pvc(labels={"tier": "db"}, annotations={"example.test/backup": "daily"})
    )
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    assert plan.actions[0].summary()["adoption"] == {
        "mode": "metadata-only",
        "previous_owner": None,
        "transferred_managers": [],
        "metadata_changes": ["annotations/example.test/backup", "labels/tier"],
    }
    run = executor(provider, tmp_path)
    assert run.run("labels", plan, snapshot, authorization)["state"] == "ready"
    body = metadata_write(api)
    assert set(body) == {"uid", "resourceVersion", "labels", "annotations"}
    assert body["labels"] == {"tier": "db"}
    assert set(body["annotations"]) == {
        "example.test/backup",
        "piceli.io/owner",
        "piceli.io/operation",
    }
    after = api.objects[("PersistentVolumeClaim", "data")]
    assert after["spec"] == before["spec"]
    assert after["metadata"]["labels"] == {"tier": "db"}
    assert payloads(run, "labels")[0]["adoption"]["metadata_changes"] == [
        "annotations/example.test/backup",
        "labels/tier",
    ]
    assert forced(api) == []


def test_inherited_owner_retained_apply_with_a_metadata_difference(
    api_url, provider, tmp_path
):
    """A retained claim of a retired owner, managed through inherited owners,
    whose only difference is metadata: a metadata-only write, never spec."""
    api, _ = api_url
    before = live_pvc(api, owner="previous-owner")
    intent = ResourceIntent.from_manifest(
        pvc(
            labels={"app.kubernetes.io/part-of": "shop"},
            annotations={"example.test/tier": "state"},
        )
    )
    plan, snapshot, authorization = prepare(
        provider, [intent], grant_inherited=("previous-owner",)
    )
    [action] = plan.actions
    assert action.operation is PlanOperation.APPLY
    assert action.metadata_changes == (
        "annotations/example.test/tier",
        "labels/app.kubernetes.io/part-of",
    )
    assert action.summary()["metadata_only"] == list(action.metadata_changes)
    run = executor(provider, tmp_path)
    assert run.run("inherited", plan, snapshot, authorization)["state"] == "ready"
    body = metadata_write(api)
    assert set(body) == {"uid", "resourceVersion", "labels", "annotations"}
    assert "spec" not in body
    after = api.objects[("PersistentVolumeClaim", "data")]
    assert after["spec"] == before["spec"]
    assert after["metadata"]["uid"] == before["metadata"]["uid"]
    assert after["metadata"]["labels"] == {"app.kubernetes.io/part-of": "shop"}
    assert after["metadata"]["annotations"]["example.test/tier"] == "state"
    assert after["metadata"]["annotations"]["piceli.io/owner"] == provider.owner_id
    assert payloads(run, "inherited")[0]["metadata_only"] == {
        "mode": "metadata-only",
        "previous_owner": "previous-owner",
        "metadata_changes": list(action.metadata_changes),
    }
    assert forced(api) == []

    # The next plan has nothing left to write.
    writes = len(mutations(api))
    plan, snapshot, authorization = prepare(
        provider, [intent], grant_inherited=("previous-owner",)
    )
    assert not plan.actions[0].metadata_changes
    run = executor(provider, tmp_path / "again")
    assert run.run("again", plan, snapshot, authorization)["state"] == "ready"
    assert len(mutations(api)) == writes


def test_inherited_retained_metadata_apply_without_the_grant_writes_nothing(
    api_url, provider, tmp_path
):
    api, _ = api_url
    live_pvc(api, owner="previous-owner")
    intent = ResourceIntent.from_manifest(pvc(labels={"tier": "db"}))
    plan, snapshot, authorization = prepare(provider, [intent])
    report = executor(provider, tmp_path).run(
        "ungranted", plan, snapshot, authorization
    )
    assert report["failure_category"] == "ownership-precondition-failed"
    assert mutations(api) == []


def test_retained_apply_with_a_spec_difference_is_refused_at_plan_time(
    api_url, provider
):
    api, _ = api_url
    live_pvc(api, owner=provider.owner_id)
    intent = ResourceIntent.from_manifest(pvc(storage="5Gi", labels={"tier": "db"}))
    with pytest.raises(ValueError, match="only metadata labels and annotations"):
        prepare(provider, [intent])
    assert mutations(api) == []


def test_forged_metadata_changes_are_refused_before_any_write(
    api_url, provider, tmp_path
):
    api, _ = api_url
    live_pvc(api, owner=provider.owner_id)
    intent = ResourceIntent.from_manifest(pvc(labels={"tier": "db"}))
    plan, snapshot, _ = prepare(provider, [intent])
    forged = replace(
        plan, actions=(replace(plan.actions[0], metadata_changes=("labels/other",)),)
    )
    with pytest.raises(ValueError, match="metadata-only changes"):
        executor(provider, tmp_path).run(
            "forged", forged, snapshot, grant(provider, forged, snapshot)
        )
    assert mutations(api) == []


def test_interrupted_metadata_write_is_reconciled_on_resume(
    api_url, provider, tmp_path
):
    api, _ = api_url
    live_pvc(api, owner=provider.owner_id)
    intent = ResourceIntent.from_manifest(pvc(labels={"tier": "db"}))
    plan, snapshot, authorization = prepare(provider, [intent])
    api.inject("PATCH", "/data", disconnect_after=True, dry_run=False)
    run = executor(provider, tmp_path)
    first = run.run("cut", plan, snapshot, authorization)
    assert first["state"] == "blocked"
    assert run.journal.actions("cut")[0]["state"] == "intent"
    second = run.run("cut", plan, snapshot, authorization, resume=True)
    assert second["state"] == "ready"
    assert len(mutations(api)) == 1
    assert api.objects[("PersistentVolumeClaim", "data")]["metadata"]["labels"] == {
        "tier": "db"
    }


def test_secret_adoption_with_a_different_private_value_is_refused_before_writes(
    api_url, provider, tmp_path
):
    api, _ = api_url
    api.put(manifest("Secret", "credentials", value="bGl2ZQ=="))
    run = executor(provider, tmp_path)
    other = run.secrets.put(TARGET, "b3RoZXI=")
    intent = ResourceIntent.from_manifest(
        manifest("Secret", "credentials")
    ).with_secret("/data/password", other)
    config = ResourceIntent.from_manifest(manifest("ConfigMap", "settings"))
    plan, snapshot, authorization = prepare(
        provider, [config, intent], adopt=(intent.ref,)
    )
    # The public plan cannot tell whether the private value matches.
    assert plan.actions[-1].adoption == Adoption(AdoptionMode.METADATA_ONLY)
    with pytest.raises(ValueError, match="contain the desired manifest") as error:
        run.run("secret", plan, snapshot, authorization)
    assert "bGl2ZQ" not in str(error.value) and "b3RoZXI" not in str(error.value)
    assert mutations(api) == []

    same = run.secrets.put(TARGET, "bGl2ZQ==")
    intent = ResourceIntent.from_manifest(
        manifest("Secret", "credentials")
    ).with_secret("/data/password", same)
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    assert run.run("secret-ok", plan, snapshot, authorization)["state"] == "ready"
    secret = api.objects[("Secret", "credentials")]
    assert secret["data"] == {"password": "bGl2ZQ=="}
    assert secret["metadata"]["annotations"]["piceli.io/owner"] == provider.owner_id


def test_rollback_of_a_retained_adoption_leaves_the_volume_untouched(
    api_url, provider, tmp_path
):
    api, _ = api_url
    live_pvc(api)
    intent = ResourceIntent.from_manifest(pvc())
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    run = executor(provider, tmp_path)
    assert run.run("keep", plan, snapshot, authorization)["state"] == "ready"
    adopted = copy.deepcopy(api.objects[("PersistentVolumeClaim", "data")])
    writes = len(mutations(api))
    run.compensate("keep", plan, snapshot, authorization)
    assert len(mutations(api)) == writes
    assert api.objects[("PersistentVolumeClaim", "data")] == adopted


def test_retained_kinds_are_never_taken_over(api_url, provider, tmp_path):
    api, _ = api_url
    live_pvc(api)
    intent = ResourceIntent.from_manifest(pvc())
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    forged_action = replace(
        plan.actions[0], adoption=Adoption(AdoptionMode.TAKEOVER, None, ())
    )
    forged = replace(plan, actions=(forged_action,))
    with pytest.raises(ValueError, match="adoption details"):
        executor(provider, tmp_path).run(
            "forged", forged, snapshot, grant(provider, forged, snapshot)
        )
    current = provider.get(snapshot.discovery.resources[0].identity)
    with pytest.raises(ProviderError, match="retained-resource"):
        provider.take_over(
            current,
            current.manifest,
            transferred_managers=("kubectl-client-side-apply",),
        )
    with pytest.raises(ProviderError, match="retained-resource"):
        provider.converge_takeover(current, current.manifest, transferred_managers=())
    assert mutations(api) == [] and forced(api) == []


# --------------------------------------------------------------- workloads

CONTROLLER_ENTRIES = [
    # Deployment controller bookkeeping on the main resource: kept.
    (
        "kube-controller-manager",
        "Update",
        {"metadata": {"annotations": {"deployment.kubernetes.io/revision": "3"}}},
    ),
]


def kubectl_created(api, *, replicas_scale=None):
    """`kubectl create deployment web --image=busybox` then `kubectl set image`."""
    value = manifest("Deployment", "worker")
    value["metadata"]["labels"] = {"app": "worker"}
    value["metadata"]["annotations"] = {"deployment.kubernetes.io/revision": "3"}
    value["spec"]["template"]["spec"]["containers"] = [
        {
            "name": "busybox",
            "image": "busybox:1.36.1",
            "command": ["sleep", "infinity"],
        }
    ]
    created = copy.deepcopy(value)
    created["metadata"].pop("annotations")
    created["spec"]["template"]["spec"]["containers"][0]["image"] = None
    created["spec"]["template"]["spec"]["containers"][0].pop("image")
    set_image = {
        "spec": {
            "template": {
                "spec": {"containers": [{"name": "busybox", "image": "busybox:1.36.1"}]}
            }
        }
    }
    stored = api.put(
        value,
        managers=[
            ("kubectl-create", "Update", created),
            ("kubectl-set", "Update", set_image),
            *CONTROLLER_ENTRIES,
        ],
    )
    entries = api.objects[("Deployment", "worker")]["metadata"]["managedFields"]
    entries.append(
        {
            "manager": "kube-controller-manager",
            "operation": "Update",
            "apiVersion": "apps/v1",
            "fieldsType": "FieldsV1",
            "subresource": "status",
            "fieldsV1": {"f:status": {"f:replicas": {}}},
        }
    )
    if replicas_scale is not None:
        api.objects[("Deployment", "worker")]["spec"]["replicas"] = replicas_scale
        entries.append(
            {
                "manager": "autoscaler",
                "operation": "Update",
                "apiVersion": "apps/v1",
                "fieldsType": "FieldsV1",
                "subresource": "scale",
                "fieldsV1": {"f:spec": {"f:replicas": {}}},
            }
        )
    return stored


def containers(api):
    live = api.objects[("Deployment", "worker")]
    return [item["name"] for item in live["spec"]["template"]["spec"]["containers"]]


def test_takeover_transfers_every_client_manager_and_removes_undeclared_fields(
    api_url, provider, tmp_path
):
    api, _ = api_url
    kubectl_deployment(api)
    intent = desired_deployment()
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    action = plan.actions[0]
    # Every client manager is transferred, not only those owning declared fields.
    assert action.adoption == Adoption(
        AdoptionMode.TAKEOVER,
        None,
        ("kubectl-client-side-apply", "kubectl-rollout", "kubectl-set"),
    )
    assert action.summary()["adoption"]["removes_undeclared_fields"] is True

    run = executor(provider, tmp_path)
    assert run.run("takeover", plan, snapshot, authorization)["state"] == "ready"

    transfer, apply = mutations(api)
    assert transfer["content_type"] == "application/merge-patch+json"
    assert set(transfer["body"]["metadata"]) == {
        "uid",
        "resourceVersion",
        "managedFields",
    }
    assert [e["manager"] for e in transfer["body"]["metadata"]["managedFields"]] == [
        MANAGER
    ]
    assert apply["content_type"] == "application/apply-patch+yaml"
    assert apply["query"]["force"] == ["false"]
    live = api.objects[("Deployment", "worker")]
    assert live["spec"]["template"]["spec"]["containers"] == [
        {"name": "worker", "image": "example.invalid/worker:new"}
    ]
    # Undeclared client-written fields are gone, including the kubectl
    # client-side-apply record and the rollout restart annotation.
    assert not live["spec"]["template"]["metadata"].get("annotations")
    assert set(live["metadata"]["annotations"]) == {
        "piceli.io/owner",
        "piceli.io/operation",
    }
    assert set(api.managers("Deployment", "worker")) == {f"{MANAGER}/Apply"}
    [payload] = payloads(run, "takeover")
    assert payload["adoption"] == {
        "mode": "takeover",
        "previous_owner": None,
        "transferred_managers": [
            "kubectl-client-side-apply",
            "kubectl-rollout",
            "kubectl-set",
        ],
        "completed_transfer": [
            "kubectl-client-side-apply",
            "kubectl-rollout",
            "kubectl-set",
        ],
    }
    # The one forced request is the dry-run admission check.
    assert [r["query"].get("dryRun") for r in forced(api)] == [["All"]]


def test_takeover_with_another_container_name_leaves_only_declared_containers(
    api_url, provider, tmp_path
):
    api, _ = api_url
    kubectl_created(api)
    value = manifest("Deployment", "worker")
    value["metadata"]["labels"] = {"app": "worker"}
    value["spec"]["template"]["spec"]["containers"] = [
        {"name": "web", "image": "example.invalid/web:1"}
    ]
    intent = ResourceIntent.from_manifest(value)
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    # kubectl-set only owns the busybox image (not declared); it is still listed.
    assert plan.actions[0].adoption.transferred_managers == (
        "kubectl-create",
        "kubectl-set",
    )
    run = executor(provider, tmp_path)
    assert run.run("rename", plan, snapshot, authorization)["state"] == "ready"
    assert containers(api) == ["web"]
    owners = api.managers("Deployment", "worker")
    assert set(owners) == {f"{MANAGER}/Apply", "kube-controller-manager/Update"}
    live = api.objects[("Deployment", "worker")]
    # Controller bookkeeping and status entries are kept.
    assert live["metadata"]["annotations"]["deployment.kubernetes.io/revision"] == "3"
    assert any(
        entry.get("subresource") == "status"
        for entry in live["metadata"]["managedFields"]
    )


def test_autoscaler_scale_entry_is_kept_and_a_real_conflict_surfaces(
    api_url, provider, tmp_path
):
    api, _ = api_url
    kubectl_created(api, replicas_scale=1)
    value = manifest("Deployment", "worker")
    value["metadata"]["labels"] = {"app": "worker"}
    intent = ResourceIntent.from_manifest(value)
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    assert "autoscaler" not in plan.actions[0].adoption.transferred_managers
    run = executor(provider, tmp_path)
    assert run.run("scaled", plan, snapshot, authorization)["state"] == "ready"
    live = api.objects[("Deployment", "worker")]
    assert any(
        entry["manager"] == "autoscaler" and entry.get("subresource") == "scale"
        for entry in live["metadata"]["managedFields"]
    )

    # The autoscaler owns replicas=4; the release declares 1: a real conflict,
    # reported instead of forced.
    api.objects.pop(("Deployment", "worker"))
    kubectl_created(api, replicas_scale=4)
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    report = executor(provider, tmp_path / "second").run(
        "conflict", plan, snapshot, authorization
    )
    assert report["failure_category"] == "conflict"
    assert api.objects[("Deployment", "worker")]["spec"]["replicas"] == 4
    assert all(r["query"].get("dryRun") == ["All"] for r in forced(api))


def test_later_kubectl_edit_shows_as_drift(api_url, provider, tmp_path):
    api, _ = api_url
    kubectl_deployment(api)
    intent = desired_deployment()
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    assert executor(provider, tmp_path).run("t", plan, snapshot, authorization)[
        "state"
    ] == ("ready")
    composition = DeploymentComposition((DeploymentComponent("app", (intent,)),))

    def drift():
        _, observed, _ = prepare(provider, [intent])
        return field_drift(composition, observed, MANAGER)

    assert drift() == []
    # `kubectl set image` after the adoption: an Update by another manager.
    live = api.objects[("Deployment", "worker")]
    live["spec"]["template"]["spec"]["containers"][0]["image"] = "hand/edited:1"
    live["metadata"]["managedFields"].append(
        {
            "manager": "kubectl-set",
            "operation": "Update",
            "apiVersion": "apps/v1",
            "fieldsType": "FieldsV1",
            "fieldsV1": {
                "f:spec": {
                    "f:template": {
                        "f:spec": {
                            "f:containers": {'k:{"name":"worker"}': {"f:image": {}}}
                        }
                    }
                }
            },
        }
    )
    assert drift() == [{"resource": intent.ref.__dict__, "managers": ["kubectl-set"]}]
    plan, snapshot, authorization = prepare(provider, [intent])
    assert plan.actions[0].operation is PlanOperation.APPLY


def test_adopting_a_managed_object_again_reclaims_foreign_fields(
    api_url, provider, tmp_path
):
    """Recovery: a managed object that clients wrote to converges again."""
    api, _ = api_url
    kubectl_created(api)
    live = api.objects[("Deployment", "worker")]
    live["metadata"]["annotations"]["piceli.io/owner"] = provider.owner_id
    live["spec"]["template"]["spec"]["containers"].append(
        {"name": "web", "image": "example.invalid/web:1"}
    )
    live["metadata"]["managedFields"].append(
        {
            "manager": MANAGER,
            "operation": "Apply",
            "apiVersion": "apps/v1",
            "fieldsType": "FieldsV1",
            "fieldsV1": {
                "f:spec": {
                    "f:template": {
                        "f:spec": {
                            "f:containers": {
                                'k:{"name":"web"}': {"f:name": {}, "f:image": {}}
                            }
                        }
                    }
                }
            },
        }
    )
    value = manifest("Deployment", "worker")
    value["metadata"]["labels"] = {"app": "worker"}
    value["spec"]["template"]["spec"]["containers"] = [
        {"name": "web", "image": "example.invalid/web:1"}
    ]
    intent = ResourceIntent.from_manifest(value)
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    [action] = plan.actions
    assert action.operation is PlanOperation.ADOPT
    assert action.adoption.previous_owner == provider.owner_id
    run = executor(provider, tmp_path)
    assert run.run("reclaim", plan, snapshot, authorization)["state"] == "ready"
    assert containers(api) == ["web"]
    assert f"{MANAGER}/Apply" in api.managers("Deployment", "worker")
    assert not {"kubectl-create/Update", "kubectl-set/Update"} & set(
        api.managers("Deployment", "worker")
    )


def test_update_meeting_a_foreign_manager_still_conflicts(api_url, provider, tmp_path):
    api, _ = api_url
    kubectl_deployment(api)
    intent = desired_deployment()
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    # An adopt restored from an older plan has no takeover mode: never forced.
    legacy = replace(plan, actions=(replace(plan.actions[0], adoption=None),))
    report = executor(provider, tmp_path).run(
        "legacy", legacy, snapshot, grant(provider, legacy, snapshot)
    )
    assert report["state"] == "failed"
    assert report["failure_category"] == "conflict"
    assert forced(api) == []


def test_unauthorized_adoption_is_refused_before_any_write(api_url, provider, tmp_path):
    api, _ = api_url
    kubectl_deployment(api)
    intent = desired_deployment()
    with pytest.raises(ValueError, match="explicit adoption"):
        prepare(provider, [intent])
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    run = executor(provider, tmp_path)
    # A grant that does not name the ADOPT action.
    apply_grant = replace(
        authorization,
        actions=(replace(authorization.actions[0], operation=PlanOperation.APPLY),),
    )
    with pytest.raises(ValueError, match="action/private-version scope"):
        run.run("no-grant", plan, snapshot, apply_grant)
    # A plan that under-reports the managers it would transfer.
    narrowed = replace(
        plan,
        actions=(
            replace(
                plan.actions[0],
                adoption=Adoption(AdoptionMode.TAKEOVER, None, ("kubectl-set",)),
            ),
        ),
    )
    with pytest.raises(ValueError, match="adoption details"):
        run.run("narrowed", narrowed, snapshot, grant(provider, narrowed, snapshot))
    assert mutations(api) == [] and forced(api) == []


def test_managers_changed_since_planning_block_the_takeover(
    api_url, provider, tmp_path
):
    api, _ = api_url
    kubectl_deployment(api)
    intent = desired_deployment()
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    api.objects[("Deployment", "worker")]["metadata"]["managedFields"].append(
        {
            "manager": "late-editor",
            "operation": "Update",
            "apiVersion": "apps/v1",
            "fieldsType": "FieldsV1",
            "fieldsV1": {"f:spec": {"f:replicas": {}}},
        }
    )
    report = executor(provider, tmp_path).run("late", plan, snapshot, authorization)
    assert report["failure_category"] == "field-owner-precondition-failed"
    assert mutations(api) == [] and forced(api) == []


def test_takeover_retries_a_concurrent_write(api_url, provider, tmp_path):
    api, _ = api_url
    kubectl_deployment(api)
    intent = desired_deployment()
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    # The managedFields transfer meets a concurrent write (a 409 on its
    # resourceVersion precondition) once.
    original = api.route

    def route(request):
        if request["content_type"] == "application/merge-patch+json" and not getattr(
            route, "failed", False
        ):
            route.failed = True  # type: ignore[attr-defined]
            api.version += 1
            api.objects[("Deployment", "worker")]["metadata"]["resourceVersion"] = str(
                api.version
            )
            return 409, {}
        return original(request)

    api.route = route  # type: ignore[method-assign]
    run = executor(provider, tmp_path)
    assert run.run("retry", plan, snapshot, authorization)["state"] == "ready"
    assert set(api.managers("Deployment", "worker")) == {f"{MANAGER}/Apply"}


def test_interrupted_takeover_is_converged_on_resume(api_url, provider, tmp_path):
    api, _ = api_url
    kubectl_created(api)
    value = manifest("Deployment", "worker")
    value["metadata"]["labels"] = {"app": "worker"}
    value["spec"]["template"]["spec"]["containers"] = [
        {"name": "web", "image": "example.invalid/web:1"}
    ]
    intent = ResourceIntent.from_manifest(value)
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    original = api.route
    calls = {"apply": 0}

    def route(request):
        if request["content_type"] == "application/apply-patch+yaml" and request[
            "query"
        ].get("dryRun") != ["All"]:
            calls["apply"] += 1
            if calls["apply"] == 1:
                return 500, {}
        return original(request)

    api.route = route  # type: ignore[method-assign]
    run = executor(provider, tmp_path)
    first = run.run("resume-me", plan, snapshot, authorization)
    assert first["state"] == "blocked"
    assert run.journal.actions("resume-me")[0]["state"] == "intent"
    assert containers(api) == ["busybox"]  # transferred, not yet applied
    second = run.run("resume-me", plan, snapshot, authorization, resume=True)
    assert second["state"] == "ready"
    assert containers(api) == ["web"]
    assert payloads(run, "resume-me")[0]["adoption"]["completed_transfer"] == [
        "kubectl-create",
        "kubectl-set",
    ]


def test_compensating_a_takeover_restores_the_previous_spec(
    api_url, provider, tmp_path
):
    api, _ = api_url
    kubectl_created(api)
    value = manifest("Deployment", "worker")
    value["metadata"]["labels"] = {"app": "worker"}
    value["spec"]["template"]["spec"]["containers"] = [
        {"name": "web", "image": "example.invalid/web:1"}
    ]
    intent = ResourceIntent.from_manifest(value)
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    run = executor(provider, tmp_path)
    assert run.run("undo", plan, snapshot, authorization)["state"] == "ready"
    assert containers(api) == ["web"]
    result = run.compensate("undo", plan, snapshot, authorization)
    assert result["state"] == "compensated-with-retention"
    live = api.objects[("Deployment", "worker")]
    assert live["spec"]["template"]["spec"]["containers"] == [
        {
            "name": "busybox",
            "image": "busybox:1.36.1",
            "command": ["sleep", "infinity"],
        }
    ]
    # Ownership stays with Piceli; the undo itself is not forced.
    assert live["metadata"]["annotations"]["piceli.io/owner"] == provider.owner_id
    assert mutations(api)[-1]["query"]["force"] == ["false"]
    assert all(r["query"].get("dryRun") == ["All"] for r in forced(api))


def test_planned_takeover_detail_is_bound_into_the_plan_hash(api_url, provider):
    api, _ = api_url
    kubectl_deployment(api)
    intent = desired_deployment()
    plan, _, _ = prepare(provider, [intent], adopt=(intent.ref,))
    narrowed = replace(
        plan,
        actions=(
            replace(
                plan.actions[0],
                adoption=Adoption(AdoptionMode.TAKEOVER, None, ("kubectl-set",)),
            ),
        ),
    )
    assert narrowed.plan_hash != plan.plan_hash


def test_provider_takeover_guards_fail_closed(api_url, provider):
    api, _ = api_url
    kubectl_deployment(api)
    deployment = provider.get(
        ResourceIdentity("apps/v1", "Deployment", TARGET.namespace, "worker")
    )
    body = deployment.manifest
    body["metadata"].pop("managedFields")
    with pytest.raises(ProviderError, match="ownership-precondition-failed"):
        provider.take_over(deployment, body, transferred_managers=())  # no claim
    body["metadata"]["annotations"] = {
        "piceli.io/owner": provider.owner_id,
        "piceli.io/operation": "a" * 32,
    }
    stale = copy.deepcopy(body)
    stale["metadata"]["resourceVersion"] = "0"
    with pytest.raises(ProviderError, match="uid-version-precondition-failed"):
        provider.take_over(deployment, stale, transferred_managers=())
    # A transferable manager that the plan did not list stops the takeover.
    with pytest.raises(ProviderError, match="field-owner-precondition-failed"):
        provider.converge_takeover(
            deployment, body, transferred_managers=("kubectl-set",)
        )
    with pytest.raises(ProviderError, match="not-retained"):
        provider.adopt_metadata(deployment, operation_id="a" * 32)
    with pytest.raises(ProviderError, match="invalid-force"):
        provider._write(
            deployment.identity,
            body,
            create=True,
            dry_run=True,
            deadline=None,
            force=True,
        )
    assert forced(api) == [] and mutations(api) == []
