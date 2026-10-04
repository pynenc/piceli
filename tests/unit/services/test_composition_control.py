"""Composition views keep only the status schema's fields; Sync writes one request."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from piceli.gitops import GitOpsError
from piceli.gitops.state import REQUEST_SCHEMA, DirectoryChannel
from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.services.authority import ScopePolicy, request_principal
from piceli.services.composition_control import CompositionControl, sync_request
from piceli.services.contracts import Principal
from piceli.services.query import QueryError, QueryService
from piceli.services.registration import Registration

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "sha256:" + "c" * 64


def status() -> dict[str, Any]:
    return {
        "schema": "piceli.gitops-status.v1",
        "controller": {
            "state": "running",
            "last_poll": "2026-10-01T09:00:00Z",
            "repo": "x",
        },
        "sources": {
            "product": {
                "url": "https://user:s3cret-token@git.example/shop/product.git",
                "refs": {"main": SHA_A, "v1.2.0": SHA_B, "bad": "not-a-sha"},
                "last_poll": "2026-10-01T09:00:00Z",
                "private": "never-expose",
            },
            "assets": {
                "url": "git@git.example:shop/assets.git",
                "refs": {"main": SHA_B},
            },
        },
        "envs": {
            "main": {
                "namespace": "shop",
                "state": "deployed",
                "health": "healthy",
                "revision": {"product": SHA_A, "assets": SHA_B},
                "last_sync": "2026-10-01T09:01:00Z",
                "plan_hash": "not-a-hash",
                "components": {
                    "api": {
                        "source": "product",
                        "commit": SHA_A,
                        "digest": DIGEST,
                        "state": "synced",
                        "health": "healthy",
                        "updated_at": "2026-10-01T09:01:00Z",
                        "env": {"PASSWORD": "never-expose"},
                    },
                    "worker": {"source": "product", "state": "invented-state"},
                },
            },
            "wp-login": {"namespace": "shop-wp-login", "state": "pending"},
        },
    }


class Channel:
    def __init__(self, document: dict[str, Any] | None) -> None:
        self.document = document
        self.requests: dict[str, dict[str, Any]] = {}
        self.fail = False

    def read_status(self) -> dict[str, Any] | None:
        if self.fail:
            raise GitOpsError("gitops-cluster-failed", "unreachable")
        return self.document

    def add_request(self, key: str, body: dict[str, Any]) -> None:
        if self.fail:
            raise GitOpsError("gitops-cluster-failed", "unreachable")
        self.requests[key] = dict(body)


def _service(
    tmp_path: Path, channel: Any, policy: ScopePolicy | None = None
) -> tuple[CompositionControl, QueryService]:
    query = QueryService(
        [
            Registration(
                "cluster",
                "piceli-system",
                KubeconfigTarget(
                    tmp_path / "config", "piceli-incluster", "piceli-system"
                ),
            )
        ],
        scope_policy=policy,
    )

    @contextmanager
    def open_channel() -> Iterator[Any]:
        yield channel

    return CompositionControl(query, "cluster", open_channel), query


def test_overview_keeps_documented_fields_and_drops_credentials(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, Channel(status()))
    view = service.overview()
    text = str(view)
    assert "s3cret" not in text and "never-expose" not in text and "user:" not in text
    assert "repo" not in view["controller"]
    (product,) = [item for item in view["sources"] if item["name"] == "product"]
    assert product["url"] == "https://git.example/shop/product.git"
    assert product["refs"] == {"main": SHA_A, "v1.2.0": SHA_B}
    (assets,) = [item for item in view["sources"] if item["name"] == "assets"]
    assert assets["url"] == "git@git.example:shop/assets.git"
    main = view["environments"][0]
    assert main["name"] == "main"
    assert main["revision"] == {"assets": SHA_B, "product": SHA_A}
    assert main["last_sync"] == "2026-10-01T09:01:00Z"
    assert main["plan_hash"] is None
    api, worker = main["components"]
    assert api == {
        "name": "api",
        "source": "product",
        "commit": SHA_A,
        "digest": DIGEST,
        "state": "synced",
        "health": "healthy",
        "updated_at": "2026-10-01T09:01:00Z",
        "reason": None,
    }
    assert worker["state"] == "unknown" and worker["health"] == "unknown"


def test_scp_style_url_with_a_credential_keeps_only_the_host() -> None:
    from piceli.services.composition_control import _url

    assert _url("user:token@git.example:shop/a.git") == "git.example:shop/a.git"
    assert _url("ssh://git:pw@git.example:2222/shop/a.git?x=1") == (
        "ssh://git.example:2222/shop/a.git"
    )


def test_environment_scopes_open_workloads_and_follow_the_status(
    tmp_path: Path,
) -> None:
    channel = Channel(status())
    service, query = _service(tmp_path, channel)
    view = service.overview()
    ids = {env["name"]: env["application_id"] for env in view["environments"]}
    assert set(ids) == {"main", "wp-login"}
    registration = query.registrations[ids["main"]]
    assert registration.target.namespace == "shop"
    assert registration.target.kubeconfig == tmp_path / "config"
    kinds = {kind for _, kind in registration.kinds}
    assert "Secret" not in kinds and "ConfigMap" not in kinds
    # An environment that leaves the status loses its scope.
    assert channel.document is not None
    del channel.document["envs"]["wp-login"]
    service.overview()
    assert ids["wp-login"] not in query.registrations
    assert ids["main"] in query.registrations


def test_environment_detail_and_unknown_environment(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, Channel(status()))
    detail = service.environment("main")
    assert detail["environment"]["name"] == "main"
    assert [item["name"] for item in detail["environment"]["components"]] == [
        "api",
        "worker",
    ]
    with pytest.raises(QueryError) as missing:
        service.environment("nope")
    assert missing.value.code == "ui-sync-target-unknown"
    with pytest.raises(QueryError) as invalid:
        service.environment("../x")
    assert invalid.value.code == "ui-invalid-request"


def test_absent_controller_is_unconfigured(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, Channel(None))
    assert service.overview() == {
        "configured": False,
        "controller": None,
        "sources": [],
        "environments": [],
    }
    with pytest.raises(QueryError) as absent:
        service.environment("main")
    assert absent.value.code == "ui-controller-absent"
    with pytest.raises(QueryError) as sync:
        service.sync("main")
    assert sync.value.code == "ui-controller-absent"


def test_sync_writes_the_gitops_sync_request(tmp_path: Path) -> None:
    channel = Channel(status())
    service, _ = _service(tmp_path, channel)
    whole = service.sync("main")
    one = service.sync("main", "api")
    assert whole["state"] == one["state"] == "requested"
    assert channel.requests == {
        whole["request"]: {"schema": REQUEST_SCHEMA, "kind": "sync", "env": "main"},
        one["request"]: {
            "schema": REQUEST_SCHEMA,
            "kind": "sync",
            "env": "main",
            "component": "api",
        },
    }
    # The same Sync twice replaces the pending request.
    service.sync("main")
    assert len(channel.requests) == 2
    assert whole["request"].startswith("sync.")


@pytest.mark.parametrize(
    ("env", "component", "code"),
    [
        ("absent", None, "ui-sync-target-unknown"),
        ("main", "absent", "ui-sync-target-unknown"),
        ("Main!", None, "ui-invalid-request"),
        ("main", "bad name", "ui-invalid-request"),
    ],
)
def test_sync_refusals(
    tmp_path: Path, env: str, component: str | None, code: str
) -> None:
    channel = Channel(status())
    service, _ = _service(tmp_path, channel)
    with pytest.raises(QueryError) as refused:
        service.sync(env, component)
    assert refused.value.code == code
    assert channel.requests == {}


def test_unreachable_controller(tmp_path: Path) -> None:
    channel = Channel(status())
    channel.fail = True
    service, _ = _service(tmp_path, channel)
    with pytest.raises(QueryError) as read:
        service.overview()
    assert read.value.code == "ui-observation-unavailable"
    with pytest.raises(QueryError) as write:
        service.sync("main")
    assert write.value.code == "ui-sync-unavailable"


def test_scoped_sessions_need_grants_and_never_register_scopes(tmp_path: Path) -> None:
    policy = ScopePolicy({"viewer": {"cluster": frozenset({"inspect"})}})
    channel = Channel(status())
    service, query = _service(tmp_path, channel, policy)
    with request_principal(Principal(id="viewer", name="Viewer", kind="oidc")):
        view = service.overview()
        assert all(env["application_id"] is None for env in view["environments"])
        with pytest.raises(QueryError):
            service.sync("main")
    assert list(query.registrations) == ["cluster"]
    assert channel.requests == {}


def test_sync_request_matches_directory_channel_round_trip(tmp_path: Path) -> None:
    key, body = sync_request("main", "api")
    channel = DirectoryChannel(tmp_path)
    channel.add_request(key, body)
    assert channel.requests() == {key: body}
    with pytest.raises(GitOpsError):
        sync_request("Not A Label")


def test_environment_reports_last_action_and_verification_without_check_detail(
    tmp_path: Path,
) -> None:
    document = status()
    document["envs"]["main"].update(
        {
            "health": "degraded",
            "last_action": "verified",
            "verification": {
                "state": "failed",
                "trigger": "checks-changed",
                "checks_hash": "d" * 64,
                "rolled": [],
                "failed": [
                    {
                        "check": "http-ready",
                        "code": "check-failed",
                        "detail": "Authorization: Bearer never-expose",
                    },
                    "not-a-check",
                ],
                "at": "2026-10-01T09:02:00Z",
                "private": "never-expose",
            },
        }
    )
    document["envs"]["wp-login"]["verification"] = "not-a-mapping"
    service, _ = _service(tmp_path, Channel(document))
    environments = {item["name"]: item for item in service.overview()["environments"]}
    main = environments["main"]
    assert main["health"] == "degraded"
    assert main["last_action"] == "verified"
    assert main["verification"] == {
        "state": "failed",
        "trigger": "checks-changed",
        "checks_hash": "d" * 64,
        "at": "2026-10-01T09:02:00Z",
        "failed": [{"check": "http-ready", "code": "check-failed"}],
    }
    assert "never-expose" not in repr(service.environment("main"))
    assert environments["wp-login"]["last_action"] is None
    assert environments["wp-login"]["verification"] is None


def test_environment_on_several_clusters_lists_each_without_credentials(
    tmp_path: Path,
) -> None:
    """0.15: ``clusters`` per environment; absent for one cluster."""
    document = status()
    document["envs"]["main"]["clusters"] = {
        "edge-a": {
            "state": "unreachable",
            "health": "healthy",
            "reason": "cluster-unreachable",
            "revision": {"product": SHA_A},
            "last_contact": "2026-10-01T08:00:00Z",
            "namespace": "shop-edge",
            "api": "https://user:never-expose@100.64.0.10:6443",
            "home": False,
            "kubeconfig": "never-expose",
        },
        "Bad Name": {"state": "deployed"},
    }
    service, _ = _service(tmp_path, Channel(document))
    environments = {item["name"]: item for item in service.overview()["environments"]}
    clusters = environments["main"]["clusters"]
    assert [item["name"] for item in clusters] == ["edge-a"]
    assert clusters[0]["state"] == "unreachable"
    assert clusters[0]["last_contact"] == "2026-10-01T08:00:00Z"
    assert clusters[0]["api"] == "https://100.64.0.10:6443"
    assert "never-expose" not in repr(service.environment("main"))
    assert "clusters" not in environments["wp-login"]
