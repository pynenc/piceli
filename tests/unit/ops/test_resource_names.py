"""Live object names Kubernetes accepts must never crash observation readers."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from piceli.k8s.observe import (
    KubernetesDynamicInventoryReader,
    ObservationRef,
    ObservedObject,
    observe_session,
)
from piceli.k8s.operator import build_operator_report
from piceli.k8s.ops.discovery import (
    ApiResource,
    DiscoveredResource,
    EvidenceSource,
    PlanTarget,
    ResourceIdentity,
    ResourceListRequest,
    ResourceScope,
    ResourceType,
    valid_resource_name,
)
from piceli.k8s.ops.kubernetes_provider import KubernetesProvider

RBAC = "rbac.authorization.k8s.io/v1"
# Bootstrap RBAC objects that every kubeadm/kind cluster has in kube-system.
RBAC_NAMES = (
    "system:controller:token-cleaner",
    "system::leader-locking-kube-scheduler",
    "kubeadm:kubelet-config",
    "system:aggregate-to-admin",
)


@pytest.mark.parametrize("kind", ["Role", "RoleBinding"])
@pytest.mark.parametrize("name", RBAC_NAMES)
def test_namespaced_rbac_names_are_path_segments(kind: str, name: str) -> None:
    assert ResourceIdentity(RBAC, kind, "kube-system", name).name == name
    assert ObservationRef(RBAC, kind, "kube-system", name).name == name


@pytest.mark.parametrize("kind", ["ClusterRole", "ClusterRoleBinding"])
@pytest.mark.parametrize("name", [*RBAC_NAMES, "Mixed Case_name"])
def test_cluster_rbac_names_are_path_segments(kind: str, name: str) -> None:
    assert valid_resource_name(RBAC, kind, name)
    assert valid_resource_name("rbac.authorization.k8s.io/v1beta1", kind, name)
    ResourceIdentity(RBAC, kind, "", name)


@pytest.mark.parametrize("name", [".", "..", "a/b", "a%2Fb", "bad\nname", " "])
def test_rbac_names_still_reject_invalid_path_segments(name: str) -> None:
    with pytest.raises(ValueError):
        ResourceIdentity(RBAC, "Role", "kube-system", name)


@pytest.mark.parametrize(
    "name",
    ["system:x", "Upper", "under_score", "-leading", "trailing.", "a" * 254],
)
def test_other_kinds_require_a_dns_subdomain(name: str) -> None:
    assert not valid_resource_name("v1", "ConfigMap", name)
    # The RBAC relaxation is keyed by API group, not only by kind name.
    assert not valid_resource_name("example.com/v1", "Role", name)
    with pytest.raises(ValueError, match="invalid resource name"):
        ResourceIdentity("v1", "ConfigMap", "default", name)


@pytest.mark.parametrize("name", ["kube-root-ca.crt", "a", "a" * 253, "sh.helm.v1"])
def test_dns_subdomain_names_are_accepted(name: str) -> None:
    ResourceIdentity("v1", "ConfigMap", "default", name)


def test_discovered_rbac_resource_round_trips_through_public_wire() -> None:
    resource = DiscoveredResource.from_manifest(
        {
            "apiVersion": RBAC,
            "kind": "ClusterRole",
            "metadata": {
                "name": "system:controller:token-cleaner",
                "uid": "uid",
                "resourceVersion": "1",
            },
            "rules": [],
        },
        scope=ResourceScope.CLUSTER,
    )
    wire = resource.public_dict()
    assert wire["identity"]["name"] == "system:controller:token-cleaner"
    assert DiscoveredResource.from_public_dict(wire).identity == resource.identity


class _Response:
    status = 200

    def __init__(self, value: dict[str, Any]) -> None:
        self.value = json.dumps(value).encode()

    def read(self, *_args: object, **_kwargs: object) -> bytes:
        return self.value

    def close(self) -> None:
        pass


def test_provider_lists_rbac_objects_with_colon_names() -> None:
    target = PlanTarget("cluster-uid", "kube-system")
    role = ResourceType(RBAC, "Role")
    api = ApiResource(role, ResourceScope.NAMESPACED, "roles")
    payload = {
        "apiVersion": RBAC,
        "kind": "RoleList",
        "metadata": {"resourceVersion": "1"},
        "items": [
            {
                "metadata": {
                    "name": name,
                    "namespace": "kube-system",
                    "uid": f"uid-{index}",
                    "resourceVersion": "1",
                },
                "rules": [],
            }
            for index, name in enumerate(RBAC_NAMES)
        ],
    }
    client = SimpleNamespace(
        configuration=SimpleNamespace(
            host="https://api.example.test",
            verify_ssl=True,
            refresh_api_key_hook=None,
            proxy=None,
        ),
        update_params_for_auth=lambda *_args: None,
        rest_client=SimpleNamespace(
            pool_manager=SimpleNamespace(request=lambda *_a, **_k: _Response(payload))
        ),
    )
    provider = KubernetesProvider(
        client,
        target=target,
        field_manager="piceli-test",
        owner_id="owner",
        source=EvidenceSource.LIVE,
        cluster_uid="cluster-uid",
        namespace_uid="namespace-uid",
    )
    provider._apis[role] = api

    page = provider.list_resources(ResourceListRequest(target, api, 10))

    assert page.failure is None
    assert sorted(item.identity.name for item in page.resources) == sorted(RBAC_NAMES)


def _dynamic_reader(items: list[dict[str, Any]]) -> KubernetesDynamicInventoryReader:
    """A dynamic reader over canned list items, without any kubeconfig."""
    reader = object.__new__(KubernetesDynamicInventoryReader)
    listing = SimpleNamespace(to_dict=lambda: {"items": items})
    resource = SimpleNamespace(get=lambda **_kwargs: listing)
    reader._client = SimpleNamespace(
        resources=SimpleNamespace(get=lambda **_kwargs: resource)
    )
    reader._warnings = []
    return reader


def test_dynamic_reader_lists_rbac_names_and_skips_unmodellable_objects() -> None:
    reader = _dynamic_reader(
        [
            {"metadata": {"name": "system:controller:token-cleaner"}},
            {"metadata": {"name": "a/b"}},
            {"metadata": {"name": "kube-proxy"}},
        ]
    )

    listed = list(reader.list(RBAC, "Role", "kube-system"))

    assert [item.ref.name for item in listed] == [
        "system:controller:token-cleaner",
        "kube-proxy",
    ]
    assert reader.drain_warnings() == (
        f"{RBAC}/Role: skipped 1 object(s) with names Piceli cannot model",
    )
    assert reader.drain_warnings() == ()


def test_operator_report_includes_colon_named_rbac_and_skip_warnings() -> None:
    reader = _dynamic_reader(
        [
            {"metadata": {"name": "system:controller:bootstrap-signer"}},
            {"metadata": {"name": "a%2Fb"}},
        ]
    )

    report = build_operator_report(reader, "kube-system", include_common_types=True)

    rbac = {
        (item.ref.kind, item.ref.name)
        for item in report.unmanaged
        if item.ref.api_version == RBAC
    }
    assert ("Role", "system:controller:bootstrap-signer") in rbac
    assert ("RoleBinding", "system:controller:bootstrap-signer") in rbac
    assert f"{RBAC}/Role: skipped 1 object(s) with names Piceli cannot model" in (
        report.scan_errors
    )


def test_operator_report_turns_a_lazy_listing_failure_into_a_scan_error() -> None:
    class LazyFailureReader:
        def get(self, ref: ObservationRef) -> ObservedObject | None:
            return None

        def list(self, api_version: str, kind: str, namespace: str) -> Any:
            yield ObservedObject(ObservationRef(api_version, kind, namespace, "ok"))
            raise ValueError("undecodable object")

    report = build_operator_report(
        LazyFailureReader(), "default", include_common_types=True
    )

    assert report.unmanaged == ()
    assert "v1/ConfigMap: ValueError" in report.scan_errors


def test_observe_session_reports_reader_skip_warnings() -> None:
    class Archive:
        session_id = "a" * 32

        def to_dict(self) -> dict[str, object]:
            ref = {"api_version": RBAC, "kind": "Role", "namespace": "kube-system"}
            return {
                "composition": [{"resources": [{"resource": {**ref, "name": "x"}}]}]
            }

    reader = _dynamic_reader(
        [{"metadata": {"name": "system::leader-locking"}}, {"metadata": {"name": ".."}}]
    )
    # get() on the canned client returns the list payload; stub it separately.
    reader.get = lambda ref: None  # type: ignore[method-assign]

    report = observe_session(Archive(), reader, include_common_types=False)  # type: ignore[arg-type]

    assert [item.ref.name for item in report.undeclared] == ["system::leader-locking"]
    assert report.scan_errors == (
        f"{RBAC}/Role: skipped 1 object(s) with names Piceli cannot model",
    )
