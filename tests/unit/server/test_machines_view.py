"""The UI's Machines view: a declared Infrastructure's status, read-only, local only."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from piceli.infra import Cluster, Infrastructure, PrimaryIp, Server
from piceli.infra.machines.plan import INVENTORY
from piceli.infra.machines.state import write_record
from piceli.server.app import create_app
from piceli.services.machines import MachinesControl
from piceli.services.query import QueryService
from piceli.testing.infra import FakeProvider


def _infra(tmp_path: Path) -> Infrastructure:
    server = Server(
        "edge-1",
        provider=FakeProvider(),
        type="t1",
        image="img",
        ipv4=PrimaryIp("edge-1-v4"),
        cluster=Cluster(
            "edge-1", api="https://edge-1.example.net:6443", credentials="edge-1"
        ),
    )
    return Infrastructure("edge", servers=[server], state_dir=tmp_path / "state")


def test_machines_page_data(tmp_path: Path) -> None:
    infra = _infra(tmp_path)
    (tmp_path / "state").mkdir()
    write_record(
        tmp_path / "state",
        INVENTORY,
        {
            "resources": [{"address": "terraform_data.server-edge-1", "kind": "server", "name": "edge-1"}],
            "config_digest": "sha256:x",
            "servers": {"edge-1": {"id": "srv-1", "ipv4": "192.0.2.10", "status": "running"}},
            "estimate": {"currency": "EUR", "monthly_net": 4.29, "complete": True,
                         "items": [{"kind": "server", "server": "edge-1", "name": "edge-1", "monthly_net": 3.79}]},
        },
    )  # fmt: skip
    (tmp_path / "index.html").write_text("<!doctype html><title>Piceli</title>")
    app = create_app(
        QueryService([]),
        static_dir=tmp_path,
        machines_control=MachinesControl(infra, "machines.py:infra"),
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/v1/machines").status_code == 403  # no session yet
        client.get(f"/?token={app.state.security.launch_token}")
        capabilities = client.get("/api/v1/capabilities").json()
        assert capabilities["actions"]["machines"]["allowed"] is True
        body = client.get("/api/v1/machines").json()
    assert body["schema"] == "piceli.infra.status.v1" and body["state"] == "applied"
    server = body["servers"][0]
    assert server["ipv4"] == "192.0.2.10" and server["monthly_net"] == pytest.approx(
        3.79
    )
    assert server["cluster"] == {
        "name": "edge-1", "api": "https://edge-1.example.net:6443", "profile": "edge-1",
        "registered": False, "registered_at": None, "nodes": None,
    }  # fmt: skip
    assert body["estimate"]["monthly_net"] == pytest.approx(4.29)
    assert "state_dir" not in body and str(tmp_path) not in str(body)
    assert body["commands"]["plan"] == "piceli infra plan machines.py:infra"


def test_without_machines_the_view_is_unavailable(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>Piceli</title>")
    app = create_app(QueryService([]), static_dir=tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get(f"/?token={app.state.security.launch_token}")
        assert "machines" not in client.get("/api/v1/capabilities").json()["actions"]
        assert client.get("/api/v1/machines").status_code == 409
