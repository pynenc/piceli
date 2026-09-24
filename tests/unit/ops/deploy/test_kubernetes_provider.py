"""Regression coverage for Kubernetes API list normalization."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from piceli.k8s.ops.discovery import (
    ApiResource,
    DiscoveredResource,
    DiscoveryCoverage,
    EvidenceSource,
    Ownership,
    PlanTarget,
    ResourceIdentity,
    ResourceListRequest,
    ResourceScope,
    ResourceType,
)
from piceli.k8s.ops.kubernetes_provider import KubernetesProvider
from piceli.k8s.ops.plan import (
    DeploymentComposition,
    ObservedResource,
    ObservedSnapshot,
    PlanAuthorization,
    PlanOperation,
    build_plan,
)

TARGET = PlanTarget("cluster-uid", "app-test")
CONFIG_MAP = ResourceType("v1", "ConfigMap")


class Response:
    """Minimal successful JSON response accepted by the provider transport."""

    status = 200

    def __init__(self, value: dict) -> None:
        self.value = json.dumps(value).encode()

    def read(self, *_args, **_kwargs) -> bytes:
        return self.value

    def close(self) -> None:
        pass


def provider(
    items: list[dict],
    *,
    owner_id: str = "owner",
    inherited_owner_ids: tuple[str, ...] = (),
) -> KubernetesProvider:
    payload = {
        "apiVersion": "v1",
        "kind": "ConfigMapList",
        "metadata": {"resourceVersion": "1"},
        "items": items,
    }
    configuration = SimpleNamespace(
        host="https://api.example.test",
        verify_ssl=True,
        refresh_api_key_hook=None,
        proxy=None,
    )
    client = SimpleNamespace(
        configuration=configuration,
        update_params_for_auth=lambda *_args: None,
        rest_client=SimpleNamespace(
            pool_manager=SimpleNamespace(
                request=lambda *_args, **_kwargs: Response(payload)
            )
        ),
    )
    value = KubernetesProvider(
        client,
        target=TARGET,
        field_manager="piceli-test",
        owner_id=owner_id,
        source=EvidenceSource.LIVE,
        cluster_uid="cluster-uid",
        namespace_uid="namespace-uid",
        inherited_owner_ids=inherited_owner_ids,
    )
    value._apis[CONFIG_MAP] = ApiResource(
        CONFIG_MAP, ResourceScope.NAMESPACED, "configmaps"
    )
    return value


def item(**extra: object) -> dict:
    return {
        "metadata": {
            "name": "settings",
            "namespace": "app-test",
            "uid": "uid",
            "resourceVersion": "1",
        },
        "data": {"mode": "test"},
        **extra,
    }


def request() -> ResourceListRequest:
    return ResourceListRequest(
        TARGET, ApiResource(CONFIG_MAP, ResourceScope.NAMESPACED, "configmaps"), 10
    )


def test_list_normalizes_omitted_item_type_from_its_list() -> None:
    page = provider([item()]).list_resources(request())
    assert page.failure is None
    assert page.resources[0].identity.api_version == "v1"
    assert page.resources[0].identity.kind == "ConfigMap"
    assert page.resources[0].manifest["kind"] == "ConfigMap"


def test_dry_run_create_accepts_a_nonpersisted_admission_response() -> None:
    value = provider([])
    value._request = lambda *_args, **_kwargs: {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "settings",
            "namespace": "app-test",
            "annotations": {
                "piceli.io/owner": "owner",
                "piceli.io/operation": "operation",
            },
        },
    }
    assert (
        value.write(
            ResourceIdentity("v1", "ConfigMap", "app-test", "settings"),
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": "settings",
                    "namespace": "app-test",
                    "annotations": {
                        "piceli.io/owner": "owner",
                        "piceli.io/operation": "operation",
                    },
                },
                "data": {"mode": "test"},
            },
            create=True,
            dry_run=True,
        )
        is None
    )


def test_pending_claim_is_not_globally_ready() -> None:
    value = provider([])
    claim = DiscoveredResource.from_manifest(
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": "state",
                "namespace": "app-test",
                "uid": "uid",
                "resourceVersion": "1",
            },
            "status": {"phase": "Pending"},
        },
        scope=ResourceScope.NAMESPACED,
    )
    assert value.readiness(claim).status.value == "not-ready"


@pytest.mark.parametrize("field,value", [("apiVersion", "v2"), ("kind", "Secret")])
def test_list_rejects_supplied_item_type_that_conflicts_with_list(
    field: str, value: str
) -> None:
    page = provider([item(**{field: value})]).list_resources(request())
    assert page.failure is not None


def owned(name: str, owner: str | None) -> dict:
    value = item()
    value["metadata"] = dict(value["metadata"], name=name, uid=f"uid-{name}")
    if owner is not None:
        value["metadata"]["annotations"] = {"piceli.io/owner": owner}
    return value


FOREIGN_OWNERS = ["team-b", "team-a2", "team-a-x", "team", "TEAM-A", "team-a "]


def ownership_of(value: KubernetesProvider, owner: str | None) -> Ownership:
    value.client.rest_client.pool_manager.request = lambda *_a, **_k: Response(
        {
            "apiVersion": "v1",
            "kind": "ConfigMapList",
            "metadata": {"resourceVersion": "1"},
            "items": [owned("settings", owner)],
        }
    )
    return value.list_resources(request()).resources[0].ownership


def test_ownership_is_an_exact_owner_match() -> None:
    value = provider([], owner_id="team-a")
    assert ownership_of(value, "team-a") is Ownership.MANAGED
    assert ownership_of(value, None) is Ownership.UNMANAGED
    assert ownership_of(value, "") is Ownership.UNMANAGED


@pytest.mark.parametrize("foreign", FOREIGN_OWNERS)
def test_owner_never_manages_objects_of_prefix_sharing_owners(foreign: str) -> None:
    assert ownership_of(provider([], owner_id="team-a"), foreign) is Ownership.UNMANAGED


def test_inherited_owner_ids_are_explicit_and_exact() -> None:
    value = provider([], owner_id="team-a-v2", inherited_owner_ids=("team-a-v1",))
    assert ownership_of(value, "team-a-v2") is Ownership.MANAGED
    assert ownership_of(value, "team-a-v1") is Ownership.MANAGED
    for foreign in ("team-a-v3", "team-a", "team-a-v1x", "team-b"):
        assert ownership_of(value, foreign) is Ownership.UNMANAGED


def test_inherited_owner_ids_reject_a_bare_string() -> None:
    with pytest.raises(ValueError):
        provider([], owner_id="team-a", inherited_owner_ids="team-a-v1")  # type: ignore[arg-type]


def prune_plan(value: KubernetesProvider, items: list[dict]) -> set[str]:
    value.client.rest_client.pool_manager.request = lambda *_a, **_k: Response(
        {
            "apiVersion": "v1",
            "kind": "ConfigMapList",
            "metadata": {"resourceVersion": "1"},
            "items": items,
        }
    )
    observed = tuple(
        ObservedResource.from_manifest(
            resource.manifest,
            ownership=resource.ownership,
            scope=ResourceScope.NAMESPACED,
        )
        for resource in value.list_resources(request()).resources
    )
    coverage = DiscoveryCoverage(
        "unit-fake",
        "capture-1",
        "defaults-v1",
        (CONFIG_MAP,),
        (CONFIG_MAP,),
        (ApiResource(CONFIG_MAP, ResourceScope.NAMESPACED, "configmaps"),),
    )
    plan = build_plan(
        DeploymentComposition(()),
        ObservedSnapshot(TARGET, coverage, observed, ()),
        PlanAuthorization(TARGET, prune_managed=True),
    )
    assert all(a.operation is PlanOperation.DELETE for a in plan.actions)
    return {action.resource.ref.name for action in plan.actions}


def test_pruning_never_selects_foreign_owned_objects() -> None:
    items = [owned("mine", "team-a")] + [
        owned(f"foreign-{index}", owner) for index, owner in enumerate(FOREIGN_OWNERS)
    ]
    items.append(owned("unannotated", None))
    assert prune_plan(provider([], owner_id="team-a"), items) == {"mine"}


def test_pruning_includes_only_listed_inherited_owners() -> None:
    items = [
        owned("current", "team-a-v2"),
        owned("previous", "team-a-v1"),
        owned("older", "team-a-v0"),
        owned("other-team", "team-b"),
    ]
    value = provider([], owner_id="team-a-v2", inherited_owner_ids=("team-a-v1",))
    assert prune_plan(value, items) == {"current", "previous"}
