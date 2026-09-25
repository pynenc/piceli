"""Plan rules for the M3 kinds: immutable Job/StatefulSet fields, explicit
replace of managed Jobs and StatefulSets, and autoscaler-owned replicas."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from piceli.k8s.ops.discovery import (
    ApiResource,
    DiscoveredResource,
    DiscoveryArtifact,
    DiscoveryCoverage,
    DiscoveryLimits,
    DiscoveryProvenance,
    EvidenceSource,
    Ownership,
    PlanTarget,
    ResourceScope,
    ResourceType,
)
from piceli.k8s.ops.plan import (
    REPLACEABLE_MANAGED_KINDS,
    DeploymentComponent,
    DeploymentComposition,
    ObservedSnapshot,
    PlanAuthorization,
    PlanOperation,
    ResourceIntent,
    ResourceRef,
    autoscaled,
    build_plan,
    immutable_changes,
    replace_refusal,
)

TARGET = PlanTarget("unit-cluster", "app")
TYPES = (
    ResourceType("apps/v1", "Deployment"),
    ResourceType("apps/v1", "StatefulSet"),
    ResourceType("batch/v1", "Job"),
    ResourceType("autoscaling/v2", "HorizontalPodAutoscaler"),
    ResourceType("v1", "Service"),
    ResourceType("gateway.networking.k8s.io/v1", "HTTPRoute"),
)
MANAGER = "piceli-unit"


def pod(image: str = "example.invalid/api:1") -> dict[str, Any]:
    return {
        "metadata": {"labels": {"app": "x"}},
        "spec": {"containers": [{"name": "main", "image": image}]},
    }


def job(image: str = "example.invalid/api:1", **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "migrate", "namespace": "app"},
        "spec": {"template": pod(image), "backoffLimit": 1, **spec},
    }


def stateful_set(size: str = "1Gi", image: str = "example.invalid/db:1") -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": "db", "namespace": "app"},
        "spec": {
            "replicas": 1,
            "serviceName": "db",
            "selector": {"matchLabels": {"app": "x"}},
            "template": pod(image),
            "volumeClaimTemplates": [
                {
                    "metadata": {"name": "data"},
                    "spec": {"resources": {"requests": {"storage": size}}},
                }
            ],
        },
    }


def deployment(replicas: int | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "selector": {"matchLabels": {"app": "x"}},
        "template": pod(),
    }
    if replicas is not None:
        spec["replicas"] = replicas
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "app"},
        "spec": spec,
    }


def hpa() -> dict[str, Any]:
    return {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": "api", "namespace": "app"},
        "spec": {
            "scaleTargetRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": "api",
            },
            "minReplicas": 1,
            "maxReplicas": 5,
        },
    }


def served(manifest: dict, *, ownership: Ownership = Ownership.MANAGED) -> dict:
    """As the API server stores it: identity, owner and server-added fields."""
    value = copy.deepcopy(manifest)
    value["metadata"].update(
        {
            "uid": "uid-" + value["metadata"]["name"],
            "resourceVersion": "7",
            "annotations": {"piceli.io/owner": "owner"}
            if ownership is Ownership.MANAGED
            else {},
        }
    )
    spec = value.get("spec", {})
    if value["kind"] == "Job":
        # The Job controller adds a selector and labels to the template.
        spec["selector"] = {"matchLabels": {"controller-uid": "u"}}
        spec["template"]["metadata"]["labels"]["controller-uid"] = "u"
        spec["template"]["spec"]["restartPolicy"] = "Never"
    for claim in spec.get("volumeClaimTemplates", []):
        claim["spec"]["volumeMode"] = "Filesystem"
    return value


def snapshot(*manifests: dict, ownership: Ownership = Ownership.MANAGED):
    return ObservedSnapshot.from_discovery(
        DiscoveryArtifact(
            TARGET,
            "2026-01-01T00:00:00+00:00",
            DiscoveryLimits(),
            DiscoveryCoverage(
                "unit",
                "capture-1",
                "policy-1",
                TYPES,
                TYPES,
                tuple(
                    ApiResource(item, ResourceScope.NAMESPACED, item.kind.lower() + "s")
                    for item in TYPES
                ),
            ),
            tuple(
                DiscoveredResource.from_manifest(
                    item, scope=ResourceScope.NAMESPACED, ownership=ownership
                )
                for item in manifests
            ),
            provenance=DiscoveryProvenance(
                EvidenceSource.LOOPBACK, "endpoint", "cluster-uid", "namespace-uid"
            ),
        )
    )


def composition(*manifests: dict) -> DeploymentComposition:
    return DeploymentComposition(
        (
            DeploymentComponent(
                "app", tuple(ResourceIntent.from_manifest(item) for item in manifests)
            ),
        )
    )


def ref(manifest: dict) -> ResourceRef:
    return ResourceIntent.from_manifest(manifest).ref


def plan(desired: list[dict], live: list[dict], **authorization: Any):
    return build_plan(
        composition(*desired),
        snapshot(*live),
        PlanAuthorization(TARGET, field_manager=MANAGER, **authorization),
    )


# ----------------------------------------------------------- immutability


def test_job_template_change_is_detected_and_refused_without_replace():
    current = snapshot(served(job())).resources[0]
    unchanged = ResourceIntent.from_manifest(job())
    # Server-added selector and labels are not a change.
    assert immutable_changes(unchanged, current) == ()
    # A mutable field is not an immutable change.
    assert (
        immutable_changes(ResourceIntent.from_manifest(job(backoffLimit=3)), current)
        == ()
    )
    changed = ResourceIntent.from_manifest(job("example.invalid/api:2"))
    assert immutable_changes(changed, current) == ("spec.template",)
    assert immutable_changes(
        ResourceIntent.from_manifest(job(completions=2)), current
    ) == ("spec.completions",)
    # A planned removal inside the template is a change too.
    assert immutable_changes(
        unchanged, current, ("/spec/template/metadata/labels/tier",)
    ) == ("spec.template",)

    with pytest.raises(
        ValueError, match=r"immutable fields of .*spec\.template.*--replace Job/migrate"
    ):
        plan([job("example.invalid/api:2")], [served(job())])


def test_managed_job_is_replaced_when_named():
    result = plan(
        [job("example.invalid/api:2")],
        [served(job())],
        replace_resources=(ref(job()),),
    )
    (action,) = result.actions
    assert action.operation is PlanOperation.REPLACE
    assert action.summary()["replace"]["propagation"] == "Background"


def test_stateful_set_immutable_fields():
    current = snapshot(served(stateful_set())).resources[0]
    # The pod template and replicas may change in place.
    assert (
        immutable_changes(
            ResourceIntent.from_manifest(stateful_set(image="example.invalid/db:2")),
            current,
        )
        == ()
    )
    assert immutable_changes(
        ResourceIntent.from_manifest(stateful_set("2Gi")), current
    ) == ("spec.volumeClaimTemplates",)
    result = plan(
        [stateful_set("2Gi")],
        [served(stateful_set())],
        replace_resources=(ref(stateful_set()),),
    )
    (action,) = result.actions
    assert action.operation is PlanOperation.REPLACE
    # Pods are adopted by the new StatefulSet, claims are never deleted.
    assert action.summary()["replace"]["propagation"] == "Orphan"


def test_replace_of_managed_objects_is_limited_to_job_and_stateful_set():
    assert {"Job", "StatefulSet"} == REPLACEABLE_MANAGED_KINDS
    managed = snapshot(served(job()), served(deployment(1))).resources
    by_kind = {item.intent.ref.kind: item for item in managed}
    assert replace_refusal(by_kind["Job"]) is None
    assert "already managed" in (replace_refusal(by_kind["Deployment"]) or "")
    with pytest.raises(ValueError, match=r"cannot replace .*already managed"):
        plan(
            [deployment(2)],
            [served(deployment(1))],
            replace_resources=(ref(deployment()),),
        )


# ---------------------------------------------------------- autoscaling


def test_autoscaled_replicas_are_never_removed():
    previous = (ResourceIntent.from_manifest(deployment(3)),)
    live = served(deployment(3))
    # Without an autoscaler, a dropped replicas key is removed (defaulted).
    (action,) = plan([deployment()], [live], previous=previous).actions
    assert action.removals == ("/spec/replicas",)
    # With one, the HPA owns the count: no removal.
    assert autoscaled(composition(deployment(), hpa())) == {ref(deployment())}
    result = plan([deployment(), hpa()], [live], previous=previous)
    actions = {a.resource.ref.kind: a for a in result.actions}
    assert actions["Deployment"].removals == ()


def test_http_route_is_applied_after_services():
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "web", "namespace": "app"},
        "spec": {"ports": [{"port": 80}]},
    }
    route = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": "web", "namespace": "app"},
        "spec": {"rules": [{"backendRefs": [{"name": "web", "port": 80}]}]},
    }
    result = plan([route, service], [])
    kinds = [action.resource.ref.kind for action in result.actions]
    assert kinds == ["Service", "HTTPRoute"]
