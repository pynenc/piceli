"""One-time remote tickets never impersonate a laptop forward on the server."""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from piceli.k8s.cli.ui import app as ui_app
from piceli.k8s.cli.ui_remote import RemoteAccessClient
from piceli.k8s.ops.provider_factory import ClusterIdentity, KubeconfigTarget
from piceli.server.app import create_app
from piceli.server.cluster_security import (
    ClusterSecurity,
    ClusterSecurityConfig,
    _Session,
)
from piceli.services.authority import ScopePolicy, request_principal
from piceli.services.contracts import (
    Principal,
    RemoteAccessClaimRequest,
    RemoteAccessStartRequest,
)
from piceli.services.query import QueryError, QueryService
from piceli.services.registration import Registration
from piceli.services.remote_access import RemoteAccessService


class Reader:
    identity = ClusterIdentity("cluster-uid", "namespace-uid")
    resource_uid = "service-uid"

    def __init__(self, _registration: Registration) -> None:
        pass

    def list(self, api_version: str, kind: str, namespace: str) -> list[dict[str, Any]]:
        if (api_version, kind, namespace) != ("v1", "Service", "shop"):
            return []
        return [
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": "api",
                    "namespace": "shop",
                    "uid": self.resource_uid,
                },
                "spec": {"ports": [{"port": 8080}]},
            }
        ]

    def close(self) -> None:
        pass

    def logs(
        self,
        pod: str,
        namespace: str,
        container: str,
        *,
        previous: bool,
        tail_lines: int,
    ) -> str:
        raise AssertionError("logs are outside this access fixture")


def setup(tmp_path: Path) -> tuple[QueryService, ScopePolicy, RemoteAccessService]:
    query = QueryService(
        [
            Registration(
                "shop",
                "Shop",
                KubeconfigTarget(tmp_path / "kc", "explicit", "shop"),
                kinds=(("v1", "Service"),),
            )
        ],
        reader_factory=Reader,
        scope_policy=ScopePolicy(
            {
                "alice": {"shop": frozenset({"inspect", "access"})},
                "bob": {"shop": frozenset({"inspect", "access"})},
            }
        ),
    )
    assert query.scope_policy is not None
    return query, query.scope_policy, RemoteAccessService(query)


def _selected(query: QueryService) -> tuple[str, str]:
    with request_principal(Principal(id="alice", name="Alice", kind="oidc")):
        resource = query.resources("shop").items[0]
    return resource.id, resource.identity.uid or ""


def test_remote_ticket_claim_heartbeat_scope_and_expiry(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>Piceli</title>")
    query, policy, remote = setup(tmp_path)
    resource_id, uid = _selected(query)
    security = ClusterSecurity(
        ClusterSecurityConfig(
            origin="http://127.0.0.1:8000",
            issuer="http://127.0.0.1:9000",
            metadata_url="http://127.0.0.1:9000/.well-known/openid-configuration",
            client_id="piceli-test",
            allow_insecure_loopback_test=True,
        )
    )
    app = create_app(
        query, static_dir=tmp_path, cluster_security=security, remote_access=remote
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as alice:
        for name in ("alice", "bob"):
            token = f"{name}-session"
            security._sessions[hashlib.sha256(token.encode()).hexdigest()] = _Session(
                Principal(id=name, name=name, kind="oidc"), "csrf", time.time() + 60
            )
        alice.cookies.set(security.cookie_name, "alice-session")
        browser_headers = {
            "Origin": "http://127.0.0.1:8000",
            "X-Piceli-CSRF": "csrf",
        }
        path = "/api/v1/applications/shop/remote-access"
        body = {
            "resource_id": resource_id,
            "resource_uid": uid,
            "remote_port": 8080,
            "duration_seconds": 60,
        }
        assert alice.post(path, json=body).status_code == 403
        issued = alice.post(path, json=body, headers=browser_headers)
        assert issued.status_code == 201
        ticket = issued.json()
        session = ticket["session"]
        ticket_id = session["id"]
        assert session["state"] == "pending"
        assert session["binding_location"] == "local_client"
        assert session["endpoint"] is None
        assert session["local_port"] is None
        assert ticket["pairing_secret"] not in str(session)
        alice.cookies.set(security.cookie_name, "bob-session")
        assert alice.get(path).json()["items"] == []
        assert alice.get(f"{path}/{ticket_id}").status_code == 404
        alice.cookies.set(security.cookie_name, "alice-session")
        claim_path = f"/api/v1/remote-access/{ticket_id}/claim"
        alice.cookies.clear()
        assert (
            alice.post(
                claim_path + "/extra", json={"pairing_secret": ticket["pairing_secret"]}
            ).status_code
            == 403
        )
        assert (
            alice.post(claim_path, json={"pairing_secret": "x" * 44}).status_code == 404
        )
        assert (
            alice.post(
                claim_path,
                json={"pairing_secret": ticket["pairing_secret"]},
                headers={"Origin": "https://foreign.example"},
            ).status_code
            == 403
        )
        assert (
            alice.post(
                claim_path,
                json={"pairing_secret": ticket["pairing_secret"]},
                headers={"Sec-Fetch-Site": "cross-site"},
            ).status_code
            == 403
        )
        claim = alice.post(
            claim_path, json={"pairing_secret": ticket["pairing_secret"]}
        )
        assert claim.status_code == 200
        lease = claim.json()
        assert lease["target"]["cluster_uid"] == "cluster-uid"
        assert lease["target"]["namespace_uid"] == "namespace-uid"
        assert (
            alice.post(
                claim_path, json={"pairing_secret": ticket["pairing_secret"]}
            ).status_code
            == 404
        )
        alice.cookies.set(security.cookie_name, "alice-session")
        assert alice.get(f"{path}/{ticket_id}").json()["endpoint"] is None
        heartbeat = f"/api/v1/remote-access/{ticket_id}/heartbeat"
        assert (
            alice.post(
                heartbeat,
                json={"lease_secret": lease["lease_secret"], "state": "ready"},
            ).status_code
            == 422
        )
        ready = alice.post(
            heartbeat,
            json={
                "lease_secret": lease["lease_secret"],
                "state": "ready",
                "local_port": 49152,
            },
        )
        assert ready.status_code == 200
        assert ready.json()["endpoint"] == "127.0.0.1:49152"
        assert ready.json()["reason"] is None
        Reader.resource_uid = "replacement-uid"
        try:
            assert (
                alice.post(
                    heartbeat,
                    json={
                        "lease_secret": lease["lease_secret"],
                        "state": "ready",
                        "local_port": 49152,
                    },
                ).status_code
                == 409
            )
        finally:
            Reader.resource_uid = uid
        policy.replace({"alice": {"shop": frozenset({"inspect"})}})
        assert (
            alice.post(
                heartbeat,
                json={"lease_secret": lease["lease_secret"], "state": "connecting"},
            ).status_code
            == 404
        )
        assert alice.get(path).status_code == 404


def test_remote_client_reports_only_probed_local_port_and_closes(
    tmp_path: Path,
) -> None:
    query, _policy, remote = setup(tmp_path)
    resource_id, uid = _selected(query)
    with request_principal(Principal(id="alice", name="Alice", kind="oidc")):
        issued = remote.issue(
            "shop",
            RemoteAccessStartRequest(
                resource_id=resource_id, resource_uid=uid, remote_port=8080
            ),
        )
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("test fixture; verifier is injected")
    executable = tmp_path / "kubectl"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)

    class Response:
        status_code = 200

        def __init__(self, body: dict[str, Any]) -> None:
            self.body = body

        def json(self) -> dict[str, Any]:
            return self.body

    class Http:
        def post(self, path: str, *, json: dict[str, Any]) -> Response:
            ticket_id = issued.session.id
            if path.endswith("/claim"):
                from piceli.services.contracts import RemoteAccessClaimRequest

                return Response(
                    remote.claim(
                        ticket_id, RemoteAccessClaimRequest(**json)
                    ).model_dump()
                )
            if path.endswith("/heartbeat"):
                from piceli.services.contracts import RemoteAccessHeartbeatRequest

                return Response(
                    remote.heartbeat(
                        ticket_id, RemoteAccessHeartbeatRequest(**json)
                    ).model_dump()
                )
            from piceli.services.contracts import RemoteAccessReleaseRequest

            return Response(
                remote.release(
                    ticket_id, RemoteAccessReleaseRequest(**json)
                ).model_dump()
            )

    class Status:
        state = "running"
        reachable = True

    class Supervisor:
        closed = False

        def quick_start(self, _id: str, _namespace: str) -> None:
            pass

        def statuses(self) -> tuple[Status]:
            return (Status(),)

        def close(self) -> None:
            self.closed = True

    supervisor = Supervisor()
    verified = []
    stop = threading.Event()
    seen = []

    def report(session: Any) -> None:
        seen.append(session)
        stop.set()

    client = RemoteAccessClient(
        server="http://127.0.0.1:8000",
        kubeconfig=kubeconfig,
        context="explicit",
        local_port=49152,
        kubectl=executable,
        allow_insecure_loopback_test=True,
        http_client=Http(),
        verify_resource=lambda resource, target, port: verified.append(
            (resource.uid, target.cluster_uid, target.namespace_uid, port)
        ),
        supervisor_factory=lambda **_kwargs: supervisor,  # type: ignore[arg-type]
        probe=lambda port: port == 49152,
        poll_seconds=0.01,
    )
    try:
        client.run(
            issued.session.id,
            issued.pairing_secret,
            stop=stop,
            on_state=report,
        )
    finally:
        client.close()
    assert seen[0].state == "ready"
    assert seen[0].endpoint == "127.0.0.1:49152"
    assert verified == [
        (uid, "cluster-uid", "namespace-uid", 8080),
        (uid, "cluster-uid", "namespace-uid", 8080),
    ]
    assert supervisor.closed
    with request_principal(Principal(id="alice", name="Alice", kind="oidc")):
        assert remote.get("shop", issued.session.id).state == "stopped"


def test_pending_ticket_expiry_and_browser_stop_revoke_pairing(tmp_path: Path) -> None:
    query, _policy, remote = setup(tmp_path)
    resource_id, uid = _selected(query)
    alice = Principal(id="alice", name="Alice", kind="oidc")
    request = RemoteAccessStartRequest(
        resource_id=resource_id, resource_uid=uid, remote_port=8080
    )
    with request_principal(alice):
        expired = remote.issue("shop", request)
        remote._tickets[expired.session.id].claim_deadline = time.monotonic() - 1
        assert remote.get("shop", expired.session.id).state == "expired"
    with pytest.raises(QueryError):
        remote.claim(
            expired.session.id,
            RemoteAccessClaimRequest(pairing_secret=expired.pairing_secret),
        )
    with request_principal(alice):
        stopped = remote.issue("shop", request)
        assert remote.stop("shop", stopped.session.id).state == "stopped"
    with pytest.raises(QueryError):
        remote.claim(
            stopped.session.id,
            RemoteAccessClaimRequest(pairing_secret=stopped.pairing_secret),
        )


def test_cluster_access_subject_must_also_have_inspection_grant(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        ui_app,
        [
            "cluster-observe",
            "--api-server",
            "https://kubernetes.example.test",
            "--ca-file",
            str(tmp_path / "ca"),
            "--token-file",
            str(tmp_path / "token"),
            "--namespace",
            "shop",
            "--control-dir",
            str(tmp_path / "state"),
            "--origin",
            "https://piceli.example.test",
            "--oidc-issuer",
            "https://id.example.test",
            "--oidc-metadata-url",
            "https://id.example.test/.well-known/openid-configuration",
            "--oidc-client-id",
            "piceli-test",
            "--authorized-sub",
            "alice",
            "--authorized-access-sub",
            "bob",
        ],
    )
    assert result.exit_code == 2
    assert '"reason": "ui-invalid-request"' in result.stdout
