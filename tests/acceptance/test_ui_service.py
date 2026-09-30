"""First-use and permission journeys through HTTP and the real fake-K8s client."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.k8s.release_spec import ReleaseSpec
from piceli.server.app import create_app
from piceli.services.query import QueryService
from piceli.services.registration import Registration
from piceli.testing import FakeCluster, fake_cluster, manifest

ORIGIN = "http://127.0.0.1:8000"
API = "/api/v1"


@pytest.fixture
def cluster(tmp_path: Path) -> Iterator[FakeCluster]:
    with fake_cluster() as running:
        running.api.put(manifest("Deployment", "web"), owned=True)
        secret = manifest("Secret", "credential", value="cHJpdmF0ZS1jcmVkZW50aWFs")
        secret["metadata"]["annotations"] = {
            "kubectl.kubernetes.io/last-applied-configuration": "private-last-applied",
        }
        running.api.put(secret)
        yield running
        # These journeys must not perform mutations, including dry-run writes.
        assert all(request["method"] == "GET" for request in running.api.requests)


def _registration(
    cluster: FakeCluster, directory: Path, id: str = "shop"
) -> Registration:
    return Registration(
        id=id,
        name=id.title(),
        target=KubeconfigTarget(
            cluster.kubeconfig(directory / f"{id}.kubeconfig"),
            "fake",
            cluster.namespace,
            cluster_uid="cluster-uid",
            namespace_uid="namespace-uid",
            transport="loopback-http",
        ),
    )


def _client(service: QueryService, directory: Path, *, prefix: str = "") -> TestClient:
    static = directory / "assets"
    static.mkdir(exist_ok=True)
    (static / "index.html").write_text(
        "<!doctype html><html><body>Piceli</body></html>"
    )
    return TestClient(
        create_app(service, origin=ORIGIN, url_prefix=prefix, static_dir=static),
        base_url=ORIGIN,
    )


def _bootstrap(client: TestClient, prefix: str = "") -> None:
    # Without the launch token a page grants nothing.
    assert client.get(prefix + "/").status_code == 401
    assert not client.cookies
    token = client.app.state.security.launch_token  # type: ignore[attr-defined]
    response = client.get(f"{prefix}/?token={token}")
    assert response.status_code == 200
    assert client.cookies
    cookie = response.headers.get("set-cookie", "").lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


def test_inventory_first_use_observes_resources_without_executing_definition(
    cluster: FakeCluster, tmp_path: Path
) -> None:
    service = QueryService([_registration(cluster, tmp_path)])
    with _client(service, tmp_path) as client:
        _bootstrap(client)
        apps = client.get(API + "/applications").json()
        assert [item["id"] for item in apps["items"]] == ["shop"]
        app = apps["items"][0]
        assert app["definition_kind"] == "inventory"
        assert app["relation"] == "unknown"
        assert app["target"]["namespace"] == cluster.namespace
        response = client.get(API + "/applications/shop/resources")
        assert response.status_code == 200
        resources = response.json()
        web = next(
            item for item in resources["items"] if item["identity"]["name"] == "web"
        )
        assert web["presence"] == "present"
        assert web["relation"] == "unknown"
        assert web["identity"]["target_id"] == app["target"]["id"]
        assert web["images"] == ["example.invalid/worker:test"]
        detail = client.get(API + f"/applications/shop/resources/{web['id']}")
        assert detail.status_code == 200
        assert detail.json()["identity"] == web["identity"]


def test_existing_release_registration_does_not_import_consumer_code(
    cluster: FakeCluster, tmp_path: Path
) -> None:
    kubeconfig = cluster.kubeconfig(tmp_path / "cluster.yaml")
    composition = tmp_path / "arbitrary_consumer.py"
    composition.write_text(
        "raise AssertionError('inventory must not evaluate source')\n"
    )
    spec = ReleaseSpec.from_dict(
        {
            "target": {
                "kubeconfig": str(kubeconfig),
                "context": "fake",
                "namespace": cluster.namespace,
                "transport": "loopback-http",
            },
            "release": {
                "name": "shop",
                "owner": "shop",
                "field_manager": "shop",
                "composition": "arbitrary_consumer.py:build",
                "state_dir": "consumer-state",
            },
            "images": {"web": "registry.example/web@sha256:" + "a" * 64},
        },
        base=tmp_path,
    )
    registration = Registration.from_release("shop", "Shop", spec)
    with _client(QueryService([registration]), tmp_path) as client:
        _bootstrap(client)
        response = client.get(API + "/applications/shop")
        assert response.status_code == 200
        assert response.json()["definition_kind"] == "release"
        assert response.json()["source"]["kind"] == "local"
        assert str(tmp_path) not in response.text
        resources = client.get(API + "/applications/shop/resources")
        assert resources.status_code == 200
        assert any(
            item["identity"]["name"] == "web" for item in resources.json()["items"]
        )
    assert not (tmp_path / "consumer-state").exists()


def test_secret_values_and_private_annotations_never_reach_resource_responses(
    cluster: FakeCluster, tmp_path: Path
) -> None:
    with _client(QueryService([_registration(cluster, tmp_path)]), tmp_path) as client:
        _bootstrap(client)
        response = client.get(API + "/applications/shop/resources")
        assert response.status_code == 200
        secret = next(
            item
            for item in response.json()["items"]
            if item["identity"]["kind"] == "Secret"
        )
        detail = client.get(API + f"/applications/shop/resources/{secret['id']}")
        assert detail.status_code == 200
        for payload in (response.text, detail.text):
            assert "cHJpdmF0ZS1jcmVkZW50aWFs" not in payload
            assert "private-credential" not in payload
            assert "private-last-applied" not in payload
            assert str(tmp_path) not in payload


def test_denied_kind_is_partial_without_erasing_available_resources(
    cluster: FakeCluster, tmp_path: Path
) -> None:
    cluster.api.inject("GET", "/secrets", status=403)
    with _client(QueryService([_registration(cluster, tmp_path)]), tmp_path) as client:
        _bootstrap(client)
        response = client.get(API + "/applications/shop/resources")
    assert response.status_code == 200
    result = response.json()
    assert any(item["identity"]["name"] == "web" for item in result["items"])
    assert any("Secret" in error["scope"] for error in result["partial"])
    assert result["freshness"]["reason"]
    assert "server-password-do-not-publish" not in response.text


def test_explicit_target_identity_mismatch_does_not_fall_back_to_ambient_cluster(
    cluster: FakeCluster, tmp_path: Path
) -> None:
    registration = _registration(cluster, tmp_path)
    registration = replace(
        registration, target=replace(registration.target, cluster_uid="other-cluster")
    )
    with _client(QueryService([registration]), tmp_path) as client:
        _bootstrap(client)
        response = client.get(API + "/applications/shop/resources")
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["partial"]
    assert response.json()["freshness"]["state"] == "unavailable"


@pytest.mark.parametrize("namespace", ["kube-system", "piceli-test"])
def test_first_observation_pins_unpinned_target_and_rejects_recreation(
    cluster: FakeCluster, tmp_path: Path, namespace: str
) -> None:
    registration = _registration(cluster, tmp_path)
    registration = replace(
        registration,
        target=replace(registration.target, cluster_uid=None, namespace_uid=None),
    )
    service = QueryService([registration])
    with _client(service, tmp_path) as client:
        _bootstrap(client)
        first = client.get(API + "/applications/shop/resources")
        assert first.status_code == 200
        assert first.json()["items"]
        cluster.api.put(manifest("Namespace", namespace), uid="replacement-uid")
        count_before = len(cluster.api.requests)
        second = client.get(API + "/applications/shop/resources")
        assert second.status_code == 200
        assert second.json()["items"] == []
        assert second.json()["partial"]
        assert second.json()["freshness"]["state"] == "unavailable"
        assert not any(
            request["path"].endswith("/deployments")
            for request in cluster.api.requests[count_before:]
        )


def test_pagination_keeps_a_snapshot_and_rejects_cross_application_cursor(
    cluster: FakeCluster, tmp_path: Path
) -> None:
    first = _registration(cluster, tmp_path)
    other = _registration(cluster, tmp_path, "other")
    service = QueryService([first, other], page_size=1)
    with _client(service, tmp_path) as client:
        _bootstrap(client)
        page = client.get(API + "/applications/shop/resources").json()
        cursor = page["next_page"]
        assert cursor
        assert len(page["items"]) == 1
        next_page = client.get(
            API + "/applications/shop/resources", params={"cursor": cursor}
        )
        assert next_page.status_code == 200
        assert next_page.json()["cursor"] == page["cursor"]
        assert next_page.json()["items"][0]["id"] != page["items"][0]["id"]
        crossed = client.get(
            API + "/applications/other/resources", params={"cursor": cursor}
        )
        assert crossed.status_code == 409


def test_api_requires_bootstrap_identity_and_refuses_foreign_origin(
    cluster: FakeCluster, tmp_path: Path
) -> None:
    with _client(QueryService([_registration(cluster, tmp_path)]), tmp_path) as client:
        assert client.get(API + "/applications").status_code in {401, 403}
        _bootstrap(client)
        assert client.get(API + "/applications").status_code == 200
        foreign = client.get(
            API + "/applications", headers={"Origin": "http://evil.example"}
        )
        assert foreign.status_code == 403
        wrong_host = client.get(API + "/applications", headers={"Host": "evil.example"})
        assert wrong_host.status_code == 403


def test_missing_application_has_typed_private_detail_free_error(
    cluster: FakeCluster, tmp_path: Path
) -> None:
    with _client(QueryService([_registration(cluster, tmp_path)]), tmp_path) as client:
        _bootstrap(client)
        response = client.get(API + "/applications/not-registered")
    assert response.status_code == 404
    result = response.json()
    assert result["code"]
    assert result["correlation_id"]
    assert isinstance(result["retryable"], bool)
    assert str(tmp_path) not in json.dumps(result)


def test_capabilities_do_not_advertise_unimplemented_writes(
    cluster: FakeCluster, tmp_path: Path
) -> None:
    with _client(QueryService([_registration(cluster, tmp_path)]), tmp_path) as client:
        _bootstrap(client)
        response = client.get(API + "/capabilities")
    assert response.status_code == 200
    for name in ("deploy", "plan"):
        capability = response.json()["actions"][name]
        assert capability["allowed"] is False
        assert capability["reason"]
