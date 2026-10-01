"""The forward-mode UI over HTTP: composition views, Sync, and the launch Secret.

Uses the public fake Kubernetes API with the real client; no cluster.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from piceli.gitops.install import connect
from piceli.gitops.state import DirectoryChannel
from piceli.infra import Cluster, Controller, Ui
from piceli.infra.ui_install import LAUNCH_SECRET, NAMESPACE, render_ui
from piceli.k8s.cli.ui_forward import LaunchError, publish_token, read_launch
from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.server.app import create_app
from piceli.services.composition_control import CompositionControl
from piceli.services.query import QueryService
from piceli.services.registration import Registration
from piceli.testing import FakeAPI, fake_cluster, manifest

ORIGIN = "http://127.0.0.1:8790"
API = "/api/v1"
SHA = "3f9c2d1e8a7b4c5d6e0f1a2b3c4d5e6f7a8b9c0d"
STATUS: dict[str, Any] = {
    "schema": "piceli.gitops-status.v1",
    "controller": {"state": "running", "last_poll": "2026-10-01T09:30:00Z"},
    "sources": {
        "product": {
            "url": "https://git.example/shop/product.git",
            "refs": {"main": SHA},
            "last_poll": "2026-10-01T09:30:00Z",
        }
    },
    "envs": {
        "main": {
            "namespace": "piceli-test",
            "state": "deployed",
            "health": "healthy",
            "revision": {"product": SHA},
            "components": {
                "web": {
                    "source": "product",
                    "commit": SHA,
                    "digest": "sha256:" + "a" * 64,
                    "state": "synced",
                    "health": "healthy",
                }
            },
        }
    },
}


def _headers(client: TestClient) -> dict[str, str]:
    return {
        "Origin": ORIGIN,
        "X-Piceli-CSRF": next(
            value
            for name, value in client.cookies.items()
            if name.startswith("piceli_csrf_")
        ),
    }


@pytest.fixture
def ui(tmp_path: Path) -> Iterator[tuple[TestClient, DirectoryChannel, Any]]:
    channel = DirectoryChannel(tmp_path / "gitops")
    channel.publish(STATUS)
    with fake_cluster() as cluster:
        cluster.api.put(manifest("Deployment", "web"), owned=True)
        cluster.api.put(manifest("Secret", "credential", value="cHJpdmF0ZQ=="))
        query = QueryService(
            [
                Registration(
                    "cluster",
                    "piceli-system",
                    KubeconfigTarget(
                        cluster.kubeconfig(tmp_path / "config"),
                        "fake",
                        "piceli-test",
                        transport="loopback-http",
                    ),
                )
            ]
        )

        @contextmanager
        def open_channel() -> Iterator[DirectoryChannel]:
            yield channel

        static = tmp_path / "assets"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><html><body></body></html>")
        app = create_app(
            query,
            origin=ORIGIN,
            static_dir=static,
            composition_control=CompositionControl(query, "cluster", open_channel),
            launch_token="t" * 43,
        )
        client = TestClient(app, base_url=ORIGIN)
        assert client.get(f"{API}/composition").status_code == 403
        assert client.get("/?token=" + "t" * 43).status_code == 200
        yield client, channel, cluster


def test_views_list_sources_environments_and_open_workloads(
    ui: tuple[TestClient, DirectoryChannel, Any],
) -> None:
    client, _, _ = ui
    actions = client.get(f"{API}/capabilities").json()["actions"]
    assert actions["composition"]["allowed"] and actions["composition_sync"]["allowed"]
    overview = client.get(f"{API}/composition").json()
    assert overview["configured"] is True
    assert overview["sources"][0]["refs"] == {"main": SHA}
    (env,) = overview["environments"]
    assert env["revision"] == {"product": SHA}
    detail = client.get(f"{API}/composition/environments/main").json()
    assert detail["environment"]["components"][0]["state"] == "synced"
    # The environment's namespace opens in the Resources view, without Secrets.
    resources = client.get(f"{API}/applications/{env['application_id']}/resources")
    names = {item["identity"]["name"] for item in resources.json()["items"]}
    assert "web" in names and "credential" not in names


def test_sync_button_writes_one_request_and_refusals_are_friendly(
    ui: tuple[TestClient, DirectoryChannel, Any],
) -> None:
    client, channel, cluster = ui
    # Without the CSRF header nothing is written.
    assert (
        client.post(f"{API}/composition/sync", json={"env": "main"}).status_code == 403
    )
    response = client.post(
        f"{API}/composition/sync",
        json={"env": "main", "component": "web"},
        headers=_headers(client),
    )
    assert response.status_code == 202
    assert response.json()["state"] == "requested"
    (body,) = channel.requests().values()
    assert body["kind"] == "sync" and body["component"] == "web"
    unknown = client.post(
        f"{API}/composition/sync", json={"env": "other"}, headers=_headers(client)
    )
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "ui-sync-target-unknown"
    assert "Refresh" in unknown.json()["message"]
    invalid = client.post(
        f"{API}/composition/sync", json={"env": "Bad Name"}, headers=_headers(client)
    )
    assert invalid.json()["code"] == "ui-invalid-request"
    missing = client.get(f"{API}/composition/environments/other")
    assert missing.json()["code"] == "ui-sync-target-unknown"
    # The views never write to the cluster.
    assert all(request["method"] == "GET" for request in cluster.api.requests)


def test_launch_token_round_trip_through_the_secret(tmp_path: Path) -> None:
    api = FakeAPI(namespace=NAMESPACE)
    with fake_cluster(api) as cluster:
        config = cluster.kubeconfig(tmp_path / "config")
        with connect(config, "fake", transport="loopback-http") as session:
            with pytest.raises(LaunchError) as missing:
                read_launch(session.client)
            assert missing.value.code == "access-ui-not-installed"
            cluster_ = Cluster(
                "my-cluster",
                api="https://192.0.2.1:6443",
                credentials="my-cluster",
                controller=Controller(
                    on="node-a", image="r.example/p@sha256:" + "a" * 64
                ),
                ui=Ui(),
            )
            for item in render_ui(cluster_):
                if item["kind"] in {"Service", "Secret"}:
                    api.put(item)
            with pytest.raises(LaunchError) as starting:
                read_launch(session.client)
            assert starting.value.code == "access-ui-not-ready"
            publish_token(session.client, "x" * 43)
            assert read_launch(session.client) == "x" * 43
            stored = api.objects[("Secret", LAUNCH_SECRET)]["data"]["token"]
            assert base64.b64decode(stored).decode() == "x" * 43
            publish_token(session.client, "")
            with pytest.raises(LaunchError):
                read_launch(session.client)
