"""Regression coverage for Kubernetes API list normalization."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from piceli.k8s.ops.discovery import (
    ApiResource,
    EvidenceSource,
    PlanTarget,
    ResourceListRequest,
    ResourceIdentity,
    ResourceScope,
    ResourceType,
    DiscoveredResource,
)
from piceli.k8s.ops.kubernetes_provider import KubernetesProvider


TARGET = PlanTarget("cluster-uid", "ih-test")
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


def provider(items: list[dict]) -> KubernetesProvider:
    payload = {
        "apiVersion": "v1",
        "kind": "ConfigMapList",
        "metadata": {"resourceVersion": "1"},
        "items": items,
    }
    configuration = SimpleNamespace(
        host="https://api.ih.test",
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
        owner_id="owner",
        source=EvidenceSource.LIVE,
        cluster_uid="cluster-uid",
        namespace_uid="namespace-uid",
    )
    value._apis[CONFIG_MAP] = ApiResource(
        CONFIG_MAP, ResourceScope.NAMESPACED, "configmaps"
    )
    return value


def item(**extra: object) -> dict:
    return {
        "metadata": {
            "name": "settings",
            "namespace": "ih-test",
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
            "namespace": "ih-test",
            "annotations": {
                "piceli.io/owner": "owner",
                "piceli.io/operation": "operation",
            },
        },
    }
    assert (
        value.write(
            ResourceIdentity("v1", "ConfigMap", "ih-test", "settings"),
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": "settings",
                    "namespace": "ih-test",
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
                "namespace": "ih-test",
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
