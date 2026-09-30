"""The UI cannot approve a different controller plan or bypass scope grants."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from piceli.envs.model import EnvStatus
from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.pipeline import Target
from piceli.services.authority import ScopePolicy, request_principal
from piceli.services.contracts import Principal
from piceli.services.environment_control import EnvironmentControl
from piceli.services.query import QueryError, QueryService
from piceli.services.registration import Registration

HASH = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


class Channel:
    def __init__(self) -> None:
        self.document: dict[str, Any] = {
            "controller": {"state": "running", "repo": "secret-in-url"},
            "envs": {
                "wp-feature": {
                    "state": "approval-required",
                    "namespace": "shop-wp-feature",
                    "commit": "a" * 40,
                    "plan_hash": HASH,
                    "private": "never-expose",
                }
            },
        }
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def read_status(self) -> dict[str, Any]:
        return self.document

    def add_request(self, key: str, body: dict[str, Any]) -> None:
        self.requests.append((key, body))


def test_controller_requests_recheck_grant_and_pending_hash(tmp_path: Path) -> None:
    channel = Channel()

    @contextmanager
    def open_channel() -> Any:
        yield channel

    policy = ScopePolicy({"operator": {"shop": frozenset({"inspect"})}})
    query = QueryService(
        [
            Registration(
                "shop",
                "Shop",
                KubeconfigTarget(tmp_path / "unused", "kind-shop", "shop"),
            )
        ],
        scope_policy=policy,
    )
    service = EnvironmentControl(query, "shop", channel_factory=open_channel)
    with request_principal(Principal(id="operator", name="Operator", kind="oidc")):
        status = service.gitops_status()
        assert "secret-in-url" not in str(status)
        assert "never-expose" not in str(status)
        with pytest.raises(QueryError) as forbidden:
            service.approve_gitops("wp-feature", HASH)
        assert forbidden.value.status == 404
        policy.replace({"operator": {"shop": frozenset({"inspect", "deploy"})}})
        with pytest.raises(QueryError) as stale:
            service.approve_gitops("wp-feature", OTHER)
        assert stale.value.code == "ui-plan-stale"
        assert not channel.requests
        assert service.approve_gitops("wp-feature", HASH)["state"] == "requested"
        assert channel.requests[-1][1]["plan_hash"] == HASH
        with pytest.raises(QueryError):
            service.promote("wp-feature", "b" * 40)
        assert service.promote("wp-feature", "a" * 40)["state"] == "requested"
        policy.replace({"operator": {"shop": frozenset({"inspect"})}})
        with pytest.raises(QueryError):
            service.promote("wp-feature", "a" * 40)
        assert len(channel.requests) == 2


def test_local_branch_gets_existing_resource_log_and_access_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import piceli.services.environment_control as module

    query = QueryService(
        [Registration("shop", "Shop", KubeconfigTarget(tmp_path / "unused", "kind-shop", "shop"))]
    )
    pipeline = SimpleNamespace(
        name="shop",
        target=Target.kubeconfig(
            tmp_path / "unused", context="kind-shop", namespace="shop"
        ),
    )
    statuses = [
        EnvStatus("main", "shop", True, "running", "healthy"),
        EnvStatus("wp-feature", "shop-wp-feature", False, "running", "healthy"),
    ]
    monkeypatch.setattr(module, "list_envs", lambda _pipeline: statuses)
    control = EnvironmentControl(query, "shop", pipeline=pipeline)
    rows = control.environments()["items"]
    branch_id = rows[1]["application_id"]
    assert rows[0]["application_id"] == "shop"
    assert query.registration(branch_id, action="logs").target.namespace == "shop-wp-feature"
    assert query.registration(branch_id, action="access").target.namespace == "shop-wp-feature"
    statuses.pop()
    control.environments()
    with pytest.raises(QueryError):
        query.registration(branch_id)
