"""0.14 UI adapters keep status public and write only reviewed controller requests."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from piceli.gitops.state import DirectoryChannel
from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.services.cluster_status import ClusterStatusControl
from piceli.services.composition_control import CompositionControl
from piceli.services.named_environment_actions import NamedEnvironmentActions
from piceli.services.query import QueryError, QueryService
from piceli.services.registration import Registration

SHA = "a" * 40
OTHER = "b" * 40
HASH = "sha256:" + "c" * 64


def _query(tmp_path: Path) -> QueryService:
    return QueryService(
        [
            Registration(
                "cluster",
                "Cluster",
                KubeconfigTarget(tmp_path / "config", "fake", "piceli-system"),
            )
        ]
    )


def _actions(
    tmp_path: Path, status: dict[str, Any]
) -> tuple[NamedEnvironmentActions, DirectoryChannel]:
    channel = DirectoryChannel(tmp_path / "channel")
    channel.publish(status)

    @contextmanager
    def factory():
        yield channel

    return NamedEnvironmentActions(
        CompositionControl(_query(tmp_path), "cluster", factory)
    ), channel


def _status() -> dict[str, Any]:
    return {
        "schema": "piceli.gitops-status.v1",
        "controller": {"environments": [{"name": "preview", "promote": True}]},
        "sources": {"shop": {"refs": {"wp-feature": SHA, "main": OTHER}}},
        "envs": {
            "preview": {
                "state": "approval-required",
                "namespace": "shop-preview",
                "plan_hash": HASH,
                "revision": {"shop": SHA},
                "components": {"web": {"state": "building"}},
                "private": "never-expose",
            }
        },
    }


def test_named_actions_review_exact_published_ref_and_plan(tmp_path: Path) -> None:
    status = _status()
    status["sources"]["shop"]["refs"] = {
        "refs/heads/wp-feature": SHA,
        "refs/tags/v1": OTHER,
    }
    actions, channel = _actions(tmp_path, status)
    options = actions.options("preview")
    assert options["promote"]["allowed"] and options["approve"]["allowed"]
    assert options["refs"] == [
        {"source": "shop", "branch": "wp-feature", "commit": SHA}
    ]
    assert options["summary"] == {
        "namespace": "shop-preview",
        "revision": {"shop": SHA},
        "components": ["web"],
    }
    assert "never-expose" not in str(options)
    assert actions.promote("preview", "wp-feature", SHA)["state"] == "requested"
    assert actions.approve("preview", HASH)["state"] == "requested"
    assert {item["kind"] for item in channel.requests().values()} == {
        "promote",
        "approve",
    }
    assert any(
        item.get("env") == "preview" and item.get("commit") == SHA
        for item in channel.requests().values()
    )
    with pytest.raises(QueryError, match="ui-plan-stale"):
        actions.promote("preview", "wp-feature", OTHER)
    with pytest.raises(QueryError, match="ui-plan-stale"):
        actions.approve("preview", "sha256:" + "d" * 64)


def test_promotion_policy_and_idle_wake(tmp_path: Path) -> None:
    status = _status()
    status["controller"]["environments"][0]["promote"] = False
    entry = status["envs"]["preview"]
    entry.update(state="stopped", reason="idle-stop", stopped_at="2026-10-01T10:00:00Z")
    actions, channel = _actions(tmp_path, status)
    options = actions.options("preview")
    assert options["promote"] == {
        "allowed": False,
        "reason": "gitops-promote-not-allowed",
    }
    assert (
        options["wake"]["allowed"]
        and options["stopped_since"] == "2026-10-01T10:00:00Z"
    )
    with pytest.raises(QueryError, match="gitops-promote-not-allowed"):
        actions.promote("preview", "wp-feature", SHA)
    assert actions.wake("preview")["state"] == "requested"
    assert [item["kind"] for item in channel.requests().values()] == ["sync"]
    entry["state"] = "deployed"
    channel.publish(status)
    with pytest.raises(QueryError, match="ui-plan-stale"):
        actions.wake("preview")


def test_composition_status_without_promotion_policy_keeps_action_disabled(
    tmp_path: Path,
) -> None:
    status = _status()
    status["controller"]["environments"] = ["preview"]
    actions, _channel = _actions(tmp_path, status)
    assert actions.options("preview")["promote"] == {
        "allowed": False,
        "reason": "ui-observation-unavailable",
    }
    with pytest.raises(QueryError, match="ui-observation-unavailable"):
        actions.promote("preview", "wp-feature", SHA)


def test_cluster_projection_whitelists_status_and_shows_k3s_restart(
    tmp_path: Path,
) -> None:
    raw = {
        "state": "degraded",
        "cluster": "my-cluster",
        "credentials": "never-expose",
        "nodes": [
            {
                "name": "worker-1",
                "arch": "arm64",
                "roles": ["builder"],
                "ready": True,
                "mirror": {
                    "kind": "k3s",
                    "state": "needs-restart",
                    "private": "never-expose",
                },
            }
        ],
        "registry": {
            "state": "ready",
            "host": "piceli-registry.piceli-system.svc:5000",
            "registry": {
                "ready": True,
                "pods": [
                    {
                        "name": "registry-1",
                        "node": "worker-1",
                        "phase": "Running",
                        "ready": True,
                    }
                ],
            },
            "storage": {
                "claim": "registry-data",
                "phase": "Bound",
                "capacity": "20Gi",
                "used_bytes": 1024,
            },
        },
        "controller": {
            "health": "degraded",
            "last_poll": "2026-10-01T09:00:00Z",
            "poll_failures": 2,
        },
        "ui": {"health": "healthy", "token": "never-expose"},
    }
    view = ClusterStatusControl(_query(tmp_path), "cluster", lambda: raw).status()
    assert view["nodes"][0]["mirror"] == {"kind": "k3s", "state": "needs-restart"}
    assert view["registry"]["storage"]["used_bytes"] == 1024
    assert view["controller"]["poll_failures"] == 2
    assert "never-expose" not in str(view)


def test_live_cluster_status_projects_declaration_and_k3s_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.gitops import GitOpsError

    node = {
        "metadata": {"name": "worker-1", "labels": {"piceli.io/role-builder": "true"}},
        "status": {
            "nodeInfo": {"architecture": "arm64"},
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }
    pod = {
        "metadata": {
            "name": "mirror-1",
            "labels": {"app.kubernetes.io/component": "mirror"},
        },
        "spec": {"nodeName": "worker-1"},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    objects = {
        "/api/v1/namespaces/piceli-system/configmaps/piceli-cluster": {
            "data": {
                "cluster.json": (
                    '{"name":"demo","credentials":"private-profile",'
                    '"nodes":[{"name":"worker-1","arch":"arm64","roles":["builder"]}],'
                    '"registry":{"node":"worker-1","namespace":"piceli-system",'
                    '"name":"piceli-registry","storage":"20Gi"}}'
                )
            }
        },
        "/api/v1/nodes": {"items": [node]},
        "/api/v1/namespaces/piceli-system/pods": {"items": [pod]},
        "/api/v1/namespaces/piceli-system/configmaps/piceli-gitops-status": {
            "data": {
                "status.json": '{"controller":{"last_poll":"2026-10-01T09:00:00Z","poll_failures":0}}'
            }
        },
    }

    class Api:
        def call(self, path: str, _method: str):
            if "/proxy/stats/summary" in path:
                raise GitOpsError("gitops-cluster-failed", "stats not granted")
            if path in objects:
                return objects[path]
            if "/deployments/" in path:
                return {"status": {"readyReplicas": 1}}
            if "/services/" in path:
                return {"spec": {"clusterIP": "10.0.0.3", "ports": [{"port": 5000}]}}
            if "/persistentvolumeclaims/" in path:
                return {"status": {"phase": "Bound", "capacity": {"storage": "20Gi"}}}
            raise AssertionError(path)

    @contextmanager
    def connect(*_args, **_kwargs):
        yield Api()

    monkeypatch.setattr("piceli.gitops.install.connect", connect)
    monkeypatch.setattr(
        "piceli.k8s.cli.registry.read_reports",
        lambda *_args: {
            "worker-1": {"runtime": "k3s", "mirror": "ready", "restart": "needed"}
        },
    )
    result = ClusterStatusControl(_query(tmp_path), "cluster").status()
    assert result["state"] == "degraded"
    assert result["nodes"][0] == {
        "name": "worker-1",
        "arch": "arm64",
        "roles": ["builder"],
        "ready": True,
        "mirror": {"kind": "k3s", "state": "needs-restart"},
    }
    assert result["registry"]["storage"]["used_bytes"] is None
    assert "private-profile" not in str(result)
