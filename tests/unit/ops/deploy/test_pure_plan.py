import copy
import subprocess
import sys
import textwrap
from unittest import mock

import pytest

from piceli.k8s.ops.discovery import (
    ApiResource,
    DiscoveryCoverage,
    PlanTarget,
    ResourceScope,
    ResourceType,
)
from piceli.k8s.ops.plan import (
    DefaultedField,
    DeploymentComponent,
    DeploymentComposition,
    ObservedResource,
    ObservedSnapshot,
    Ownership,
    PlanAuthorization,
    PlanOperation,
    ResourceIntent,
    build_plan,
)

TARGET = PlanTarget("kind-local", "app-test")


def manifest(kind: str, name: str, **spec: object) -> dict:
    return {
        "apiVersion": "v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": "app-test"},
        "spec": spec,
    }


def composition(resources: list[dict]) -> DeploymentComposition:
    return DeploymentComposition(
        (
            DeploymentComponent(
                "stack", tuple(ResourceIntent.from_manifest(item) for item in resources)
            ),
        )
    )


def observed(
    resource: dict,
    *,
    uid: str,
    version: str = "1",
    ownership: Ownership = Ownership.MANAGED,
    retained: bool | None = None,
    owner_uid: str | None = None,
    scope: ResourceScope | None = None,
) -> ObservedResource:
    value = copy.deepcopy(resource)
    value["metadata"]["uid"] = uid
    value["metadata"]["resourceVersion"] = version
    if owner_uid:
        value["metadata"]["ownerReferences"] = [{"uid": owner_uid, "name": "owner"}]
    return ObservedResource.from_manifest(
        value, ownership=ownership, retained=retained, scope=scope
    )


def plan_for(
    desired: list[dict],
    current: tuple[ObservedResource, ...] = (),
    *,
    adopt: tuple = (),
    prune: bool = False,
    defaulted: tuple[DefaultedField, ...] = (),
    coverage: DiscoveryCoverage | None = None,
):
    if coverage is None:
        types = {
            ResourceType(str(item["apiVersion"]), str(item["kind"])) for item in desired
        } | {
            ResourceType(item.intent.ref.api_version, item.intent.ref.kind)
            for item in current
        }
        coverage = DiscoveryCoverage(
            "unit-fake",
            "capture-1",
            "defaults-v1",
            tuple(types),
            tuple(types),
            tuple(
                ApiResource(item, ResourceScope.NAMESPACED, item.kind.lower() + "s")
                for item in types
            ),
        )
    snapshot = ObservedSnapshot(TARGET, coverage, current, defaulted)
    return build_plan(
        composition(desired),
        snapshot,
        PlanAuthorization(TARGET, adopt_resources=adopt, prune_managed=prune),
    )


def test_plan_is_deterministic_and_does_not_mutate_inputs() -> None:
    resources = [
        manifest("Service", "api", port=443),
        manifest("ConfigMap", "config", values={"mode": "test"}),
        manifest("Deployment", "worker", replicas=2),
    ]
    original = copy.deepcopy(resources)
    first = plan_for(resources)
    second = plan_for(list(reversed(resources)))

    assert first.plan_hash == second.plan_hash
    assert first.summary() == second.summary()
    assert resources == original
    assert all(action.operation is PlanOperation.CREATE for action in first.actions)
    assert all(action.precondition.must_not_exist for action in first.actions)


def test_cold_import_does_not_load_kubernetes_client() -> None:
    probe = textwrap.dedent(
        """
        import sys
        import types

        class ClientTrap(types.ModuleType):
            def __getattr__(self, name):
                raise AssertionError(f"planning imported Kubernetes client attribute {name}")

        sys.modules["piceli.k8s.k8s_client.client"] = ClientTrap("client")
        import piceli.k8s.ops.plan
        """
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


def test_plan_compares_supplied_snapshot_without_cluster_access() -> None:
    desired = manifest("Deployment", "worker", replicas=2)
    changed = observed(manifest("Deployment", "worker", replicas=1), uid="worker-uid")
    with mock.patch(
        "piceli.k8s.k8s_client.client.ClientManager.get_client",
        side_effect=AssertionError("planning must not create a Kubernetes client"),
    ):
        plan = plan_for([desired], (changed,))
    assert plan.actions[0].operation is PlanOperation.APPLY
    assert plan.actions[0].precondition.uid == "worker-uid"


def test_unmanaged_resource_requires_explicit_adoption() -> None:
    desired = manifest("Deployment", "worker", replicas=2)
    current = observed(desired, uid="worker-uid", ownership=Ownership.UNMANAGED)
    with pytest.raises(ValueError, match="explicit adoption"):
        plan_for([desired], (current,))
    adopted = plan_for([desired], (current,), adopt=(current.intent.ref,))
    assert adopted.actions[0].operation is PlanOperation.ADOPT


def test_adoption_authorization_must_match_an_unmanaged_desired_resource() -> None:
    desired = manifest("Deployment", "worker", replicas=2)
    unrelated = ResourceIntent.from_manifest(manifest("Deployment", "unrelated")).ref
    with pytest.raises(ValueError, match="adoption authorization does not match"):
        plan_for([desired], adopt=(unrelated,))


def test_target_binding_rejects_wrong_cluster_and_namespace() -> None:
    coverage = DiscoveryCoverage("fake", "capture", "policy", (), (), ())
    snapshot = ObservedSnapshot(TARGET, coverage)
    with pytest.raises(ValueError, match="authorization target"):
        build_plan(
            composition([]),
            snapshot,
            PlanAuthorization(PlanTarget("production", "app-test")),
        )
    wrong_namespace = manifest("Deployment", "worker", replicas=1)
    wrong_namespace["metadata"]["namespace"] = "other"
    with pytest.raises(ValueError, match="outside bound namespace"):
        build_plan(composition([wrong_namespace]), snapshot, PlanAuthorization(TARGET))


def test_plan_rejects_stale_or_recreated_resource_uid() -> None:
    desired = manifest("Deployment", "worker", replicas=2)
    resource_type = ResourceType("v1", "Deployment")
    coverage = DiscoveryCoverage(
        "fake",
        "capture",
        "policy",
        (resource_type,),
        (resource_type,),
        (ApiResource(resource_type, ResourceScope.NAMESPACED, "deployments"),),
    )
    initial = ObservedSnapshot(
        TARGET, coverage, (observed(desired, uid="uid-1", version="7"),)
    )
    plan = build_plan(composition([desired]), initial, PlanAuthorization(TARGET))
    plan.validate_for(initial)
    recreated = ObservedSnapshot(
        TARGET, coverage, (observed(desired, uid="uid-2", version="1"),)
    )
    with pytest.raises(ValueError, match="snapshot changed"):
        plan.validate_for(recreated)


def test_retained_resources_are_never_pruned() -> None:
    secret = manifest("Secret", "credentials", token="hidden")
    retained_config = manifest("ConfigMap", "audit", values={"days": 30})
    retained_config["metadata"]["annotations"] = {"piceli.io/retained": "true"}
    resources = (
        observed(secret, uid="secret-uid"),
        observed(retained_config, uid="config-uid"),
    )
    plan = plan_for([], resources, prune=True)
    assert not plan.actions
    assert set(plan.protected_resources) == {item.intent.ref for item in resources}


def test_prune_rejects_unmanaged_descendant_and_orders_managed_children_first() -> None:
    owner = observed(manifest("Deployment", "owner"), uid="owner", retained=False)
    unmanaged_child = observed(
        manifest("ConfigMap", "external-child"),
        uid="external-child",
        owner_uid="owner",
        ownership=Ownership.UNMANAGED,
    )
    with pytest.raises(ValueError, match="unmanaged descendants"):
        plan_for([], (owner, unmanaged_child), prune=True)

    child = observed(
        manifest("ConfigMap", "managed-child"),
        uid="managed-child",
        owner_uid="owner",
        retained=False,
    )
    plan = plan_for([], (owner, child), prune=True)
    assert [action.resource.ref.name for action in plan.actions] == [
        "managed-child",
        "owner",
    ]


def test_prune_rejects_cyclic_owner_references() -> None:
    first = observed(
        manifest("ConfigMap", "first"),
        uid="first",
        owner_uid="second",
        retained=False,
    )
    second = observed(
        manifest("ConfigMap", "second"),
        uid="second",
        owner_uid="first",
        retained=False,
    )
    with pytest.raises(ValueError, match="owner reference graph contains a cycle"):
        plan_for([], (first, second), prune=True)


def test_api_defaulting_is_ignored_but_real_drift_is_not() -> None:
    desired = manifest("Deployment", "worker", replicas=2)
    current_manifest = copy.deepcopy(desired)
    current_manifest["spec"]["revisionHistoryLimit"] = 10
    current = observed(current_manifest, uid="worker")
    field = DefaultedField(current.intent.ref, "/spec/revisionHistoryLimit")
    assert (
        plan_for([desired], (current,), defaulted=(field,)).actions[0].operation
        is PlanOperation.NOOP
    )
    assert plan_for([desired], (current,)).actions[0].operation is PlanOperation.APPLY


def test_explicit_desired_value_is_not_hidden_by_api_default_policy() -> None:
    desired = manifest("Deployment", "worker", replicas=3)
    current_manifest = manifest("Deployment", "worker", replicas=1)
    current = observed(current_manifest, uid="worker")
    field = DefaultedField(current.intent.ref, "/spec/replicas")
    assert (
        plan_for([desired], (current,), defaulted=(field,)).actions[0].operation
        is PlanOperation.APPLY
    )


def test_defaulting_policy_is_part_of_snapshot_identity() -> None:
    desired = manifest("Deployment", "worker", replicas=2)
    current = observed(desired, uid="worker")
    resource_type = ResourceType("v1", "Deployment")
    plain_coverage = DiscoveryCoverage(
        "fake",
        "capture",
        "defaults-v1",
        (resource_type,),
        (resource_type,),
        (ApiResource(resource_type, ResourceScope.NAMESPACED, "deployments"),),
    )
    changed_coverage = DiscoveryCoverage(
        "fake",
        "capture",
        "defaults-v2",
        (resource_type,),
        (resource_type,),
        (ApiResource(resource_type, ResourceScope.NAMESPACED, "deployments"),),
    )
    plain_snapshot = ObservedSnapshot(TARGET, plain_coverage, (current,))
    changed_snapshot = ObservedSnapshot(
        TARGET,
        changed_coverage,
        (current,),
        (DefaultedField(current.intent.ref, "/spec/replicas"),),
    )
    plan = build_plan(composition([desired]), plain_snapshot, PlanAuthorization(TARGET))

    assert plain_snapshot.snapshot_hash != changed_snapshot.snapshot_hash
    with pytest.raises(ValueError, match="snapshot changed"):
        plan.validate_for(changed_snapshot)


def test_incomplete_discovery_cannot_prove_absence_or_authorize_pruning() -> None:
    resource_type = ResourceType("v1", "Deployment")
    incomplete = DiscoveryCoverage(
        "fake", "partial", "policy", (resource_type,), (), ()
    )
    with pytest.raises(ValueError, match="cannot infer resource absence"):
        plan_for([manifest("Deployment", "worker")], coverage=incomplete)

    current = observed(manifest("Deployment", "old"), uid="old", retained=False)
    with pytest.raises(ValueError, match="complete discovery coverage"):
        plan_for([], (current,), prune=True, coverage=incomplete)


def test_builtin_and_custom_resource_scope_are_explicit() -> None:
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": "app-test"},
    }
    custom = {
        "apiVersion": "infra.example/v1",
        "kind": "ClusterMachine",
        "metadata": {"name": "node-a", "namespace": "wrong"},
    }
    assert ResourceIntent.from_manifest(namespace).ref.namespace == ""
    assert (
        ResourceIntent.from_manifest(custom, scope=ResourceScope.CLUSTER).ref.namespace
        == ""
    )


def test_namespace_and_pvc_retention_cannot_be_disabled() -> None:
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": "app-test", "uid": "ns", "resourceVersion": "1"},
    }
    pvc = manifest("PersistentVolumeClaim", "data")
    with pytest.raises(ValueError, match="retention protection cannot be disabled"):
        ObservedResource.from_manifest(
            namespace,
            ownership=Ownership.MANAGED,
            retained=False,
        )
    with pytest.raises(ValueError, match="retention protection cannot be disabled"):
        observed(pvc, uid="pvc", retained=False)


def test_artifacts_redact_secrets_credentials_and_environment_values() -> None:
    deployment = manifest(
        "Deployment",
        "worker",
        template={
            "spec": {
                "containers": [
                    {
                        "name": "worker",
                        "env": [
                            {"name": "DB_PASSWORD", "value": "do-not-render"},
                            {"name": "MODE", "value": "test"},
                        ],
                        "authorization": "Bearer private",
                    }
                ]
            }
        },
    )
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "credentials", "namespace": "app-test"},
        "stringData": {"opaque": "also-private"},
    }
    rendered = str(plan_for([deployment, secret]).summary())
    assert "do-not-render" not in rendered
    assert "Bearer private" not in rendered
    assert "also-private" not in rendered
    assert "test" in rendered


def test_secret_values_do_not_create_a_low_entropy_digest_or_plan_hash_oracle() -> None:
    def secret(value: str) -> dict:
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "credentials", "namespace": "app-test"},
            "stringData": {"password": value},
        }

    first = plan_for([secret("guess-one")]).summary()
    second = plan_for([secret("guess-two")]).summary()
    assert first["plan_hash"] == second["plan_hash"]
    assert (
        first["actions"][0]["artifact_digest"]
        == second["actions"][0]["artifact_digest"]
    )


def test_component_dependencies_become_resource_dependencies() -> None:
    storage = DeploymentComponent(
        "storage", (ResourceIntent.from_manifest(manifest("Service", "api")),)
    )
    workers = DeploymentComponent(
        "workers",
        (ResourceIntent.from_manifest(manifest("Deployment", "rustvello")),),
        dependencies=("storage",),
    )
    plan = build_plan(
        DeploymentComposition((workers, storage)),
        ObservedSnapshot(
            TARGET,
            DiscoveryCoverage(
                "fake",
                "empty",
                "policy",
                (
                    ResourceType("v1", "Deployment"),
                    ResourceType("v1", "Service"),
                ),
                (
                    ResourceType("v1", "Deployment"),
                    ResourceType("v1", "Service"),
                ),
                (
                    ApiResource(
                        ResourceType("v1", "Deployment"),
                        ResourceScope.NAMESPACED,
                        "deployments",
                    ),
                    ApiResource(
                        ResourceType("v1", "Service"),
                        ResourceScope.NAMESPACED,
                        "services",
                    ),
                ),
            ),
        ),
        PlanAuthorization(TARGET),
    )
    worker_action = next(
        action for action in plan.actions if action.resource.ref.name == "rustvello"
    )
    assert [dependency.name for dependency in worker_action.dependencies] == ["api"]
