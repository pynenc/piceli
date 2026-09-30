"""A live cluster observation stream closes when its principal loses scope."""

from __future__ import annotations

import hashlib
import socket
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.server.app import create_app
from piceli.server.cluster_security import (
    ClusterSecurity,
    ClusterSecurityConfig,
    _Session,
)
from piceli.services.authority import ScopePolicy
from piceli.services.contracts import Principal
from piceli.services.query import QueryService
from piceli.services.registration import Registration


def test_live_observation_stream_closes_on_scope_revocation(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        principal = Principal(id="alice", name="Alice", kind="oidc")
        policy = ScopePolicy({"alice": {"shop": frozenset({"inspect"})}})
        query = QueryService(
            [
                Registration(
                    "shop",
                    "Shop",
                    KubeconfigTarget(tmp_path / "kc", "explicit", "shop"),
                )
            ],
            scope_policy=policy,
        )
        security = ClusterSecurity(
            ClusterSecurityConfig(
                origin=origin,
                issuer=origin,
                metadata_url=origin + "/.well-known/openid-configuration",
                client_id="piceli-test",
                allow_insecure_loopback_test=True,
            )
        )
        alice_cookie = "alice-session"
        bob_cookie = "bob-session"
        for cookie, actor in (
            (alice_cookie, principal),
            (bob_cookie, Principal(id="bob", name="Bob", kind="oidc")),
        ):
            security._sessions[hashlib.sha256(cookie.encode()).hexdigest()] = _Session(
                actor, "csrf", time.time() + 60
            )
        app = create_app(
            query, origin=origin, static_dir=tmp_path, cluster_security=security
        )
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while (
                not server.started and thread.is_alive() and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert server.started
            with httpx.Client(base_url=origin, timeout=5, trust_env=False) as client:
                params = {"application_id": "shop", "after": "0"}
                client.cookies.set(security.cookie_name, bob_cookie)
                assert (
                    client.get(
                        "/api/v1/observation-events",
                        params=params,
                    ).status_code
                    == 404
                )
                with query.observation._lock:
                    query.observation._emit("shop", "upsert", "resource-1")
                client.cookies.set(security.cookie_name, alice_cookie)
                with client.stream(
                    "GET",
                    "/api/v1/observation-events",
                    params=params,
                ) as response:
                    assert response.status_code == 200
                    lines = iter(response.iter_lines())
                    assert next(lines) == "id: 1"
                    assert next(lines) == "event: upsert"
                    assert '"scope":"shop"' in next(lines)
                    assert next(lines) == ""
                    policy.replace({})
                    assert list(lines) == []
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()
