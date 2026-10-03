"""Logs and Forwards workspaces over HTTP, on the public fake Kubernetes API.

Local `piceli ui serve` (an application scope plus a second profile's scope)
and the forward-mode composition UI (environment scopes). No cluster, no
ambient kubeconfig; forwards use a fake supervisor, never kubectl.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from piceli.gitops.state import DirectoryChannel
from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.k8s.owned_processes import OwnedProcessRegistry, process_identity
from piceli.profiles import save_profile
from piceli.server.app import create_app
from piceli.services.access import AccessService
from piceli.services.composition_control import CompositionControl
from piceli.services.log_workspace import ProfileScopes
from piceli.services.logs import LogService
from piceli.services.query import QueryService
from piceli.services.registration import Registration
from piceli.testing import fake_cluster, manifest
from tests.browser.showcase_resources import seed_resources

ORIGIN = "http://127.0.0.1:8000"
API = "/api/v1"
TOKEN = "w" * 43


class Supervisor:
    """A forward that reports a passing probe; it opens nothing."""

    def __init__(self, **_kwargs: object) -> None:
        self.closed = False

    def quick_start(self, _id: str, _namespace: str) -> None:
        pass

    def statuses(self) -> tuple[SimpleNamespace, ...]:
        return (SimpleNamespace(state="running", health="healthy", reachable=True),)

    def close(self) -> None:
        self.closed = True


def _headers(client: TestClient) -> dict[str, str]:
    return {
        "Origin": ORIGIN,
        "X-Piceli-CSRF": next(
            value
            for name, value in client.cookies.items()
            if name.startswith("piceli_csrf_")
        ),
    }


def _static(tmp_path: Path) -> Path:
    static = tmp_path / "assets"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><html><body></body></html>")
    return static


@pytest.fixture
def local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Any, OwnedProcessRegistry]]:
    monkeypatch.setenv("PICELI_PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.setenv("PICELI_SERVICE_ACCOUNT_DIR", str(tmp_path / "no-sa"))
    monkeypatch.delenv("PICELI_IN_CLUSTER", raising=False)
    with fake_cluster() as cluster:
        seed_resources(cluster.api)
        cluster.api.put(manifest("Secret", "credential", value="cHJpdmF0ZQ=="))
        cluster.api.pod_logs[("api-release-1", "api", False)] = (
            "2026-10-03T10:00:01Z INFO listening on :8080\n"
            "2026-10-03T10:00:05Z ERROR upstream failed token=abc123secret\n"
        )
        cluster.api.pod_logs[("api-release-2", "api", False)] = (
            "2026-10-03T10:00:03Z WARN slow request\n"
        )
        kubeconfig = cluster.kubeconfig(tmp_path / "kubeconfig")
        save_profile("demo-west", kubeconfig, "fake")
        query = QueryService(
            [
                Registration(
                    "shop",
                    "Shop",
                    KubeconfigTarget(
                        kubeconfig, "fake", cluster.namespace, transport="loopback-http"
                    ),
                )
            ]
        )
        registry = OwnedProcessRegistry(tmp_path / "forwards")
        access = AccessService(
            query,
            kubectl=Path(sys.executable),
            supervisor_factory=Supervisor,  # type: ignore[arg-type]
            registry=registry,
        )
        app = create_app(
            query,
            origin=ORIGIN,
            static_dir=_static(tmp_path),
            access=access,
            logs=LogService(query),
            launch_token=TOKEN,
            active_profile="demo-east",
            profile_switch=lambda _name: None,
            profile_scopes=ProfileScopes(query, transport="loopback-http"),
        )
        with TestClient(app, base_url=ORIGIN) as client:
            assert client.get(f"{API}/logs/sources").status_code == 403
            assert client.get(f"/?token={TOKEN}").status_code == 200
            yield client, cluster, registry


def test_logs_workspace_aggregates_the_application_and_a_second_profile(
    local: tuple[TestClient, Any, OwnedProcessRegistry],
) -> None:
    client, cluster, _ = local
    actions = client.get(f"{API}/capabilities").json()["actions"]
    assert actions["logs"]["allowed"] and actions["access"]["allowed"]
    assert actions["profile_scopes"]["allowed"]
    sources = client.get(f"{API}/logs/sources").json()
    assert [(s["id"], s["kind"], s["profile"]) for s in sources["scopes"]] == [
        ("shop", "application", "demo-east")
    ]
    pods = {item["pod"]["name"]: item for item in sources["items"]}
    assert set(pods) == {"api-release-1", "api-release-2"}
    assert pods["api-release-1"]["workload"] == {"kind": "Deployment", "name": "api"}

    body = {"profile": "demo-west", "namespace": cluster.namespace}
    assert client.post(f"{API}/profiles/scopes", json=body).status_code == 403
    added = client.post(f"{API}/profiles/scopes", json=body, headers=_headers(client))
    assert added.status_code == 201
    scope = added.json()
    assert (scope["kind"], scope["profile"], scope["namespace"]) == (
        "profile",
        "demo-west",
        cluster.namespace,
    )
    listed = client.get(f"{API}/profiles").json()
    assert [item["id"] for item in listed["scopes"]] == [scope["id"]]
    assert "kubeconfig" not in json.dumps(listed)
    sources = client.get(f"{API}/logs/sources").json()
    assert {s["id"] for s in sources["scopes"]} == {"shop", scope["id"]}
    assert len(sources["items"]) == 4

    one = pods["api-release-1"]["pod"]
    two = pods["api-release-2"]["pod"]
    streams = [
        f"shop/{one['name']}/{one['uid']}/api",
        f"{scope['id']}/{two['name']}/{two['uid']}/api",
    ]
    batch = client.get(f"{API}/logs/lines", params={"stream": streams}).json()
    assert [(line["stream"], line["level"]) for line in batch["lines"]] == [
        (0, "info"),
        (1, "warn"),
        (0, "error"),
    ]
    assert "abc123secret" not in json.dumps(batch)
    assert [stream["state"] for stream in batch["streams"]] == ["ok", "ok"]
    filtered = client.get(
        f"{API}/logs/lines",
        params={"stream": streams, "level": ["error"], "q": "upstream"},
    ).json()
    assert [line["text"] for line in filtered["lines"]] == [
        "ERROR upstream failed token=[REDACTED]"
    ]
    assert (
        client.get(f"{API}/logs/lines", params={"stream": ["../x"]}).status_code == 422
    )
    removed = client.delete(
        f"{API}/profiles/scopes/{scope['id']}", headers=_headers(client)
    )
    assert removed.status_code == 204
    assert [s["id"] for s in client.get(f"{API}/logs/sources").json()["scopes"]] == [
        "shop"
    ]
    assert all(request["method"] == "GET" for request in cluster.api.requests)


def test_forwards_workspace_lists_starts_stops_and_reaps_stale(
    local: tuple[TestClient, Any, OwnedProcessRegistry], tmp_path: Path
) -> None:
    client, _, registry = local
    assert client.get(f"{API}/forwards").json() == {
        "mode": "local",
        "reason": None,
        "items": [],
        "orphans": 0,
    }
    service = next(
        item
        for item in client.get(f"{API}/applications/shop/resources").json()["items"]
        if item["identity"]["kind"] == "Service"
    )
    started = client.post(
        f"{API}/applications/shop/access-sessions",
        json={
            "resource_id": service["id"],
            "resource_uid": service["identity"]["uid"],
            "local_port": 18080,
            "remote_port": 8080,
        },
        headers=_headers(client),
    )
    assert started.status_code == 201, started.text
    page = client.get(f"{API}/forwards").json()
    (entry,) = page["items"]
    assert entry["session"]["state"] == "ready"
    assert entry["url"] == "http://127.0.0.1:18080"
    assert entry["application_name"] == "Shop" and entry["stale"] is False
    assert entry["scope"]["kind"] == "application"
    stopped = client.delete(
        f"{API}/applications/shop/access-sessions/{entry['session']['id']}",
        headers=_headers(client),
    )
    assert stopped.json()["state"] == "stopped"

    # A forward left by a UI process that is gone: recorded, never adopted.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    try:
        deadline = time.monotonic() + 5
        identity = None
        while identity is None and time.monotonic() < deadline:
            identity = process_identity(child.pid)
        registry.directory.mkdir(parents=True, exist_ok=True)
        (registry.directory / f"{child.pid}.json").write_text(
            json.dumps(
                {
                    "pid": child.pid,
                    "identity": identity,
                    "owner_pid": gone.pid,
                    "owner_identity": "gone",
                }
            )
        )
        assert client.get(f"{API}/forwards").json()["orphans"] == 1
        assert client.get(f"{API}/navigation").json()["stale_forwards"] == 1
        assert client.post(f"{API}/forwards/stale/stop").status_code == 403
        result = client.post(f"{API}/forwards/stale/stop", headers=_headers(client))
        assert result.json() == {"stopped": 1, "orphans": 0}
        assert child.wait(timeout=10) is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_forward_mode_ui_reads_environment_logs_and_has_no_forwards(
    tmp_path: Path,
) -> None:
    channel = DirectoryChannel(tmp_path / "gitops")
    channel.publish(
        {
            "schema": "piceli.gitops-status.v1",
            "controller": {"state": "running"},
            "sources": {},
            "envs": {
                "main": {
                    "namespace": "piceli-test",
                    "state": "approval-required",
                    "health": "degraded",
                    "revision": {},
                    "components": {"web": {"state": "failed", "health": "degraded"}},
                }
            },
        }
    )
    with fake_cluster() as cluster:
        seed_resources(cluster.api)
        cluster.api.pod_logs[("api-release-1", "api", False)] = (
            "2026-10-03T10:00:01Z ready\n"
        )
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

        app = create_app(
            query,
            origin=ORIGIN,
            static_dir=_static(tmp_path),
            logs=LogService(query),
            composition_control=CompositionControl(query, "cluster", open_channel),
            launch_token=TOKEN,
        )
        with TestClient(app, base_url=ORIGIN) as client:
            client.get(f"/?token={TOKEN}")
            assert (
                "profile_scopes"
                not in client.get(f"{API}/capabilities").json()["actions"]
            )
            # Without reading the overview first, the environment is a scope.
            sources = client.get(f"{API}/logs/sources").json()
            kinds = {scope["name"]: scope["kind"] for scope in sources["scopes"]}
            assert kinds == {"piceli-system": "application", "main": "environment"}
            env = next(s for s in sources["scopes"] if s["kind"] == "environment")
            item = next(
                row
                for row in client.get(
                    f"{API}/logs/sources", params={"scope": [env["id"]]}
                ).json()["items"]
                if row["pod"]["name"] == "api-release-1"
            )
            stream = f"{env['id']}/api-release-1/{item['pod']['uid']}/api"
            lines = client.get(f"{API}/logs/lines", params={"stream": [stream]}).json()
            assert [line["text"] for line in lines["lines"]] == ["ready"]
            assert client.get(f"{API}/forwards").json()["mode"] == "unavailable"
            assert (
                client.post(
                    f"{API}/profiles/scopes",
                    json={"profile": "x", "namespace": "y"},
                    headers=_headers(client),
                ).status_code
                == 409
            )
            summary = client.get(f"{API}/navigation").json()
            assert summary["environments"] == [
                {
                    "name": "main",
                    "state": "approval-required",
                    "health": "degraded",
                    "application_id": env["id"],
                }
            ]
            assert (summary["approvals"], summary["degraded_environments"]) == (1, 1)
            assert summary["failed_builds"] == 1
            assert summary["stale_forwards"] is None


def test_pod_log_reader_returns_text_lines_not_a_bytes_repr(tmp_path: Path) -> None:
    """The generated client preloads a text body as "b'...'"; the reader decodes it."""
    from piceli.services.query import KubernetesReader

    with fake_cluster() as cluster:
        seed_resources(cluster.api)
        cluster.api.pod_logs[("api-release-1", "api", False)] = "one\ntwo é\n"
        reader = KubernetesReader(
            Registration(
                "shop",
                "Shop",
                KubeconfigTarget(
                    cluster.kubeconfig(tmp_path / "kubeconfig"),
                    "fake",
                    cluster.namespace,
                    transport="loopback-http",
                ),
            )
        )
        try:
            text = reader.logs(
                "api-release-1", cluster.namespace, "api", previous=False, tail_lines=10
            )
        finally:
            reader.close()
    assert text.splitlines() == ["one", "two é"]
