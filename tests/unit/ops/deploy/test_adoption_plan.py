"""Unit coverage for adoption planning, field-manager overlap and drift."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from piceli.k8s.ops.discovery import (
    ApiResource,
    DiscoveryCoverage,
    DiscoveryProvenance,
    EvidenceSource,
    PlanTarget,
    ResourceScope,
    ResourceType,
)
from piceli.k8s.ops.executor import ActionGrant, ExecutionAuthorization
from piceli.k8s.ops.plan import (
    Adoption,
    AdoptionMode,
    DeploymentComponent,
    DeploymentComposition,
    FieldManagerEntry,
    ObservedResource,
    ObservedSnapshot,
    Ownership,
    PlanAction,
    PlanAuthorization,
    PlanOperation,
    ResourceIntent,
    build_plan,
    field_drift,
    field_manager_entries,
    manifest_contains,
    overlapping_managers,
)
from piceli.k8s.ops.revision import DeploymentRevision, ExecutionBundle

TARGET = PlanTarget("kind-local", "app-test")
CONTAINER_IMAGE = {
    "f:spec": {
        "f:template": {
            "f:spec": {"f:containers": {'k:{"name":"web"}': {"f:image": {}}}}
        }
    }
}


def deployment(image: str = "web:new") -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "web", "namespace": "app-test"},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "web"}},
            "template": {
                "metadata": {"labels": {"app": "web"}},
                "spec": {
                    "containers": [
                        {
                            "name": "web",
                            "image": image,
                            "ports": [{"containerPort": 80}],
                        }
                    ]
                },
            },
        },
    }


def claim(storage: str = "1Gi", **extra: object) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": "data", "namespace": "app-test", **extra},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": storage}},
        },
    }


def entry(manager: str, fields: dict, **extra: str) -> dict:
    return {
        "manager": manager,
        "operation": "Update",
        "apiVersion": "v1",
        "fieldsType": "FieldsV1",
        "fieldsV1": fields,
        **extra,
    }


def observed(
    value: dict,
    *,
    managers: list[dict] = (),  # type: ignore[assignment]
    owner: str | None = None,
    ownership: Ownership = Ownership.UNMANAGED,
) -> ObservedResource:
    value = copy.deepcopy(value)
    value["metadata"].update(
        {"uid": "uid-" + value["kind"], "resourceVersion": "7"},
        managedFields=list(managers),
    )
    if owner:
        value["metadata"].setdefault("annotations", {})["piceli.io/owner"] = owner
    return ObservedResource.from_manifest(value, ownership=ownership)


def snapshot(*resources: ObservedResource) -> ObservedSnapshot:
    types = tuple(
        {
            ResourceType(item.intent.ref.api_version, item.intent.ref.kind)
            for item in resources
        }
        | {
            ResourceType("apps/v1", "Deployment"),
            ResourceType("v1", "PersistentVolumeClaim"),
        }
    )
    coverage = DiscoveryCoverage(
        "unit",
        "capture",
        "policy",
        types,
        types,
        tuple(
            ApiResource(item, ResourceScope.NAMESPACED, item.kind.lower() + "s")
            for item in types
        ),
    )
    return ObservedSnapshot(TARGET, coverage, resources)


def plan(desired: list[dict], current: ObservedSnapshot, **authorization: object):
    composition = DeploymentComposition(
        (
            DeploymentComponent(
                "app", tuple(ResourceIntent.from_manifest(item) for item in desired)
            ),
        )
    )
    return build_plan(composition, current, PlanAuthorization(TARGET, **authorization))


def ref(value: dict):
    return ResourceIntent.from_manifest(value).ref


def test_field_overlap_follows_fields_v1_keys_values_and_leaves() -> None:
    desired = deployment()
    entries = field_manager_entries(
        {
            "metadata": {
                "managedFields": [
                    entry("kubectl-set", CONTAINER_IMAGE),
                    entry(
                        "kubectl-rollout",
                        {
                            "f:spec": {
                                "f:template": {
                                    "f:metadata": {
                                        "f:annotations": {
                                            ".": {},
                                            "f:kubectl.kubernetes.io/restartedAt": {},
                                        }
                                    }
                                }
                            }
                        },
                    ),
                    entry(
                        "port-owner",
                        {
                            "f:spec": {
                                "f:template": {
                                    "f:spec": {
                                        "f:containers": {
                                            'k:{"name":"web"}': {
                                                "f:ports": {
                                                    'k:{"containerPort":80,"protocol":"TCP"}': {
                                                        ".": {}
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        },
                    ),
                    entry(
                        "other-container",
                        {
                            "f:spec": {
                                "f:template": {
                                    "f:spec": {
                                        "f:containers": {
                                            'k:{"name":"sidecar"}': {"f:image": {}}
                                        }
                                    }
                                }
                            }
                        },
                    ),
                    entry(
                        "status-writer",
                        {"f:spec": {"f:replicas": {}}},
                        subresource="status",
                    ),
                    entry("selector", {"f:spec": {"f:selector": {}}}),
                    entry("self", {"f:spec": {"f:replicas": {}}}),
                ]
            }
        }
    )
    assert overlapping_managers(entries, desired, exclude=("self",)) == (
        "kubectl-set",
        "selector",
    )
    assert "self" in overlapping_managers(entries, desired)


def test_malformed_field_ownership_evidence_is_refused() -> None:
    with pytest.raises(ValueError, match="field ownership"):
        field_manager_entries({"metadata": {"managedFields": {"manager": "x"}}})
    with pytest.raises(ValueError, match="field ownership"):
        field_manager_entries({"metadata": {"managedFields": [{"fieldsV1": []}]}})
    assert field_manager_entries({"metadata": {}}) == ()


def test_manifest_containment_allows_defaults_inside_list_items() -> None:
    live = deployment()
    live["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"] = "Always"
    assert manifest_contains(live, deployment())
    assert not manifest_contains(live, deployment("web:other"))
    extra = copy.deepcopy(live)
    extra["spec"]["template"]["spec"]["containers"].append({"name": "sidecar"})
    assert not manifest_contains(extra, deployment())


def test_takeover_plan_lists_displaced_managers_and_binds_them() -> None:
    current = observed(
        deployment("web:old"),
        managers=[
            entry("kubectl-client-side-apply", {"f:spec": {"f:replicas": {}}}),
            entry("kubectl-set", CONTAINER_IMAGE),
            entry(
                "kube-controller-manager",
                {"f:status": {"f:replicas": {}}},
                subresource="status",
            ),
        ],
    )
    with pytest.raises(ValueError, match="explicit adoption"):
        plan([deployment()], snapshot(current))
    result = plan(
        [deployment()], snapshot(current), adopt_resources=(ref(deployment()),)
    )
    [action] = result.actions
    assert action.operation is PlanOperation.ADOPT
    assert action.adoption == Adoption(
        AdoptionMode.TAKEOVER, None, ("kubectl-client-side-apply", "kubectl-set")
    )
    assert action.summary()["adoption"]["mode"] == "takeover"
    tampered = replace(
        result,
        actions=(replace(action, adoption=Adoption(AdoptionMode.TAKEOVER)),),
    )
    assert tampered.plan_hash != result.plan_hash


def test_non_adopt_actions_keep_their_summary_shape() -> None:
    current = observed(deployment("web:old"), ownership=Ownership.MANAGED)
    [action] = plan([deployment()], snapshot(current)).actions
    assert action.operation is PlanOperation.APPLY
    assert "adoption" not in action.summary()
    with pytest.raises(ValueError, match="only adopt actions"):
        replace(action, adoption=Adoption(AdoptionMode.TAKEOVER))


def test_retained_adoption_is_metadata_only_and_requires_containment() -> None:
    live = claim()
    live["spec"]["volumeName"] = "pv-1"
    current = observed(
        live,
        managers=[
            entry("kubectl-client-side-apply", {"f:spec": {"f:accessModes": {}}})
        ],
    )
    [action] = plan(
        [claim()], snapshot(current), adopt_resources=(ref(claim()),)
    ).actions
    assert action.adoption == Adoption(AdoptionMode.METADATA_ONLY, None)
    with pytest.raises(ValueError, match="already contains the desired manifest"):
        plan([claim("2Gi")], snapshot(current), adopt_resources=(ref(claim()),))
    with pytest.raises(ValueError, match="cannot displace"):
        Adoption(AdoptionMode.METADATA_ONLY, None, ("kubectl",))


def test_inherited_owner_retained_objects_are_adoptable_only_when_listed() -> None:
    current = observed(claim(), owner="old-owner", ownership=Ownership.MANAGED)
    with pytest.raises(ValueError, match="adoption authorization does not match"):
        plan([claim()], snapshot(current), adopt_resources=(ref(claim()),))
    [action] = plan(
        [claim()],
        snapshot(current),
        adopt_resources=(ref(claim()),),
        inherited_owner_ids=("old-owner",),
    ).actions
    assert action.adoption == Adoption(AdoptionMode.METADATA_ONLY, "old-owner")
    # A managed workload of an inherited owner is not a takeover candidate.
    workload = observed(deployment(), owner="old-owner", ownership=Ownership.MANAGED)
    with pytest.raises(ValueError, match="adoption authorization does not match"):
        plan(
            [deployment()],
            snapshot(workload),
            adopt_resources=(ref(deployment()),),
            inherited_owner_ids=("old-owner",),
        )


def test_plan_authorization_validates_inherited_owner_ids() -> None:
    with pytest.raises(ValueError, match="inherited owner"):
        PlanAuthorization(TARGET, inherited_owner_ids="old")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="inherited owner"):
        PlanAuthorization(TARGET, inherited_owner_ids=("",))
    assert PlanAuthorization(
        TARGET, inherited_owner_ids=("b", "a", "b")
    ).inherited_owner_ids == ("a", "b")


def test_field_drift_reports_foreign_owners_of_declared_fields_only() -> None:
    managed = observed(
        deployment(),
        ownership=Ownership.MANAGED,
        managers=[
            entry("piceli", {"f:spec": {"f:replicas": {}}}),
            entry("kubectl-set", CONTAINER_IMAGE),
            entry(
                "kube-controller-manager",
                {
                    "f:metadata": {
                        "f:annotations": {"f:deployment.kubernetes.io/revision": {}}
                    }
                },
            ),
        ],
    )
    retained = observed(
        claim(),
        ownership=Ownership.MANAGED,
        managers=[
            entry("kubectl-client-side-apply", {"f:spec": {"f:accessModes": {}}})
        ],
    )
    composition = DeploymentComposition(
        (
            DeploymentComponent(
                "app",
                (
                    ResourceIntent.from_manifest(deployment()),
                    ResourceIntent.from_manifest(claim()),
                ),
            ),
        )
    )
    assert field_drift(composition, snapshot(managed, retained), "piceli") == [
        {"resource": ref(deployment()).__dict__, "managers": ["kubectl-set"]}
    ]


def test_ownership_evidence_is_excluded_from_the_snapshot_hash() -> None:
    first = observed(deployment(), managers=[entry("a", CONTAINER_IMAGE)])
    second = observed(deployment(), managers=[entry("b", CONTAINER_IMAGE)])
    assert first.field_managers != second.field_managers
    assert first.field_managers == (
        FieldManagerEntry("a", "Update", "", first.field_managers[0].fields_json),
    )
    assert snapshot(first).snapshot_hash == snapshot(second).snapshot_hash


def _grant(result, current, **extra):
    return ExecutionAuthorization(
        "grant",
        TARGET,
        DiscoveryProvenance(EvidenceSource.LIVE, "endpoint", "cluster", "namespace"),
        result.plan_hash,
        current.snapshot_hash,
        "piceli",
        "owner",
        tuple(ActionGrant.for_action(action) for action in result.actions),
        (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        **extra,
    )


def test_inherited_owner_grant_is_validated_and_bound_into_revisions() -> None:
    current = snapshot(observed(claim(), ownership=Ownership.MANAGED))
    result = plan([claim()], current)
    plain = _grant(result, current)
    inherited = _grant(result, current, inherited_owner_ids=("old", "old"))
    assert inherited.inherited_owner_ids == ("old",)
    with pytest.raises(ValueError, match="inherited owner"):
        _grant(result, current, inherited_owner_ids="old")
    with pytest.raises(ValueError, match="cannot also be inherited"):
        _grant(result, current, inherited_owner_ids=("owner",))
    before = DeploymentRevision.create(result, current, plain)
    after = DeploymentRevision.create(result, current, inherited)
    assert "inherited_owner_ids" not in before.material()["authorization"]
    assert after.material()["authorization"]["inherited_owner_ids"] == ["old"]
    assert before.revision_id != after.revision_id
    bundle = ExecutionBundle.create(before)
    renewed = replace(
        inherited, authorization_id="renewed", resume_revision_id=before.revision_id
    )
    with pytest.raises(ValueError, match="changes revision scope"):
        bundle.with_authorization(renewed)


def test_legacy_adopt_actions_carry_no_adoption_mode() -> None:
    action = PlanAction(
        PlanOperation.ADOPT,
        ResourceIntent.from_manifest(deployment()),
        (),
        observed(deployment()).precondition,
    )
    assert action.adoption is None and "adoption" not in action.summary()
