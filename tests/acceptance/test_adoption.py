"""Acceptance: adoption by ownership transfer (metadata-only and takeover).

The fake API runs with its field-ownership model enabled, so server-side apply
conflicts, ``force=true`` and ``managedFields`` replacement behave like a real
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


def prepare(provider, desired, *, adopt=(), inherited=(), grant_inherited=()):
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
        PlanAuthorization(TARGET, tuple(adopt), inherited_owner_ids=tuple(inherited)),
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
        "displaced_managers": [],
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
    labelled = ResourceIntent.from_manifest(pvc(labels={"tier": "db"}))
    with pytest.raises(ValueError, match="already contains the desired manifest"):
        prepare(provider, [labelled], adopt=(labelled.ref,))
    assert mutations(api) == []


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


def test_retained_kinds_are_never_force_applied(api_url, provider, tmp_path):
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
            current, current.manifest, displaced_managers=("kubectl-client-side-apply",)
        )
    assert mutations(api) == [] and forced(api) == []


# --------------------------------------------------------------- workloads


def test_deployment_takeover_displaces_kubectl_managers(api_url, provider, tmp_path):
    api, _ = api_url
    kubectl_deployment(api)
    intent = desired_deployment()
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    action = plan.actions[0]
    # kubectl-rollout only owns the restart annotation, which is not desired.
    assert action.adoption == Adoption(
        AdoptionMode.TAKEOVER, None, ("kubectl-client-side-apply", "kubectl-set")
    )

    run = executor(provider, tmp_path)
    assert run.run("takeover", plan, snapshot, authorization)["state"] == "ready"

    apply, cleanup = mutations(api)
    assert apply["content_type"] == "application/apply-patch+yaml"
    assert apply["query"]["force"] == ["true"]
    assert apply["body"]["metadata"]["uid"] == action.precondition.uid
    assert cleanup["content_type"] == "application/merge-patch+json"
    assert set(cleanup["body"]["metadata"]) == {
        "uid",
        "resourceVersion",
        "managedFields",
    }
    live = api.objects[("Deployment", "worker")]
    container = live["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "example.invalid/worker:new"
    assert live["metadata"]["annotations"]["piceli.io/owner"] == provider.owner_id
    owners = api.managers("Deployment", "worker")
    assert set(owners) == {f"{MANAGER}/Apply", "kubectl-rollout/Update"}
    desired_paths = {
        path
        for path in owners[f"{MANAGER}/Apply"]
        if path[0] == "f:spec" and RESTARTED not in path[-1]
    }
    assert desired_paths and not desired_paths & owners["kubectl-rollout/Update"]
    [payload] = payloads(run, "takeover")
    assert payload["adoption"] == {
        "mode": "takeover",
        "previous_owner": None,
        "displaced_managers": ["kubectl-client-side-apply", "kubectl-set"],
        "removed_managers": ["kubectl-client-side-apply", "kubectl-set"],
    }
    assert len(forced(api)) == 2  # dry-run admission check + the takeover itself
    assert all(r["query"].get("dryRun") == ["All"] for r in forced(api)[:1])


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
    assert all(r["query"].get("force") != ["true"] for r in api.requests)


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
    # A plan that under-reports the managers it would displace.
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


def test_takeover_cleanup_retries_a_concurrent_write(api_url, provider, tmp_path):
    api, _ = api_url
    kubectl_deployment(api)
    intent = desired_deployment()
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    # Let the forced apply through and make the managedFields cleanup meet a
    # concurrent write (a 409 on its resourceVersion precondition) once.
    original = api.route

    def route(request):
        if request["content_type"] == "application/merge-patch+json" and not getattr(
            route, "failed", False
        ):
            route.failed = True  # type: ignore[attr-defined]
            return 409, {}
        return original(request)

    api.route = route  # type: ignore[method-assign]
    run = executor(provider, tmp_path)
    assert run.run("retry", plan, snapshot, authorization)["state"] == "ready"
    assert set(api.managers("Deployment", "worker")) == {
        f"{MANAGER}/Apply",
        "kubectl-rollout/Update",
    }


def test_interrupted_takeover_cleanup_is_finished_on_resume(
    api_url, provider, tmp_path
):
    api, _ = api_url
    kubectl_deployment(api)
    intent = desired_deployment()
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    original = api.route
    calls = {"cleanup": 0}

    def route(request):
        if request["content_type"] == "application/merge-patch+json":
            calls["cleanup"] += 1
            if calls["cleanup"] == 1:
                return 500, {}
        return original(request)

    api.route = route  # type: ignore[method-assign]
    run = executor(provider, tmp_path)
    first = run.run("resume-me", plan, snapshot, authorization)
    assert first["state"] == "blocked"
    assert run.journal.actions("resume-me")[0]["state"] == "intent"
    assert "kubectl-set/Update" in api.managers("Deployment", "worker")
    second = run.run("resume-me", plan, snapshot, authorization, resume=True)
    assert second["state"] == "ready"
    assert set(api.managers("Deployment", "worker")) == {
        f"{MANAGER}/Apply",
        "kubectl-rollout/Update",
    }
    assert payloads(run, "resume-me")[0]["adoption"]["removed_managers"] == [
        "kubectl-client-side-apply",
        "kubectl-set",
    ]
    assert len([r for r in forced(api) if r["query"].get("dryRun") != ["All"]]) == 1


def test_compensating_a_takeover_restores_the_previous_spec(
    api_url, provider, tmp_path
):
    api, _ = api_url
    kubectl_deployment(api)
    intent = desired_deployment()
    plan, snapshot, authorization = prepare(provider, [intent], adopt=(intent.ref,))
    run = executor(provider, tmp_path)
    assert run.run("undo", plan, snapshot, authorization)["state"] == "ready"
    result = run.compensate("undo", plan, snapshot, authorization)
    assert result["state"] == "compensated-with-retention"
    live = api.objects[("Deployment", "worker")]
    template = live["spec"]["template"]
    assert template["spec"]["containers"][0]["image"] == "example.invalid/worker:old"
    assert template["metadata"]["annotations"] == {RESTARTED: "2026-09-24"}
    # Ownership stays with Piceli; the undo itself is not forced.
    assert live["metadata"]["annotations"]["piceli.io/owner"] == provider.owner_id
    undo = mutations(api)[-1]
    assert undo["query"]["force"] == ["false"]
    assert len([r for r in forced(api) if r["query"].get("dryRun") != ["All"]]) == 1


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
    api.put(manifest("ConfigMap", "mine"), owned=True)
    deployment = provider.get(
        ResourceIdentity("apps/v1", "Deployment", TARGET.namespace, "worker")
    )
    mine = provider.get(ResourceIdentity("v1", "ConfigMap", TARGET.namespace, "mine"))
    body = deployment.manifest
    body["metadata"].pop("managedFields")
    with pytest.raises(ProviderError, match="ownership-precondition-failed"):
        provider.take_over(deployment, body, displaced_managers=())  # no owner claim
    body["metadata"]["annotations"] = {
        "piceli.io/owner": provider.owner_id,
        "piceli.io/operation": "a" * 32,
    }
    stale = copy.deepcopy(body)
    stale["metadata"]["resourceVersion"] = "0"
    with pytest.raises(ProviderError, match="uid-version-precondition-failed"):
        provider.take_over(deployment, stale, displaced_managers=())
    with pytest.raises(ProviderError, match="ownership-precondition-failed"):
        provider.take_over(mine, mine.manifest, displaced_managers=())
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
