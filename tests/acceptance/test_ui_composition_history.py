"""The in-cluster UI's deployment history, approvals and registry use (0.14.6).

Reproduces the 0.14.5 report: the installed UI had no deployment history
(``operations-not-configured``) although the controller kept run records.
Here the controller side publishes its status and run history as the real
controller does (``ConfigMapChannel`` into ``piceli-system`` of the public
fake Kubernetes API, documents built from real run journals); the UI reads
them through its own ``ConfigMapChannel``, as ``piceli ui forward-serve``
does. No cluster.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from piceli.gitops.install import connect
from piceli.gitops.state import HISTORY_CONFIGMAP, ConfigMapChannel
from piceli.infra import Cluster, Controller, Ui
from piceli.infra.ui_install import render_ui
from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.server.app import create_app
from piceli.services.composition_control import CompositionControl
from piceli.services.query import QueryService
from piceli.services.registration import Registration
from piceli.testing import FakeAPI, fake_cluster
from tests.gitops_history_fixture import (
    ASSETS,
    BRANCH,
    MEDIA,
    PLAN_HASH,
    PRODUCT,
    WEB,
    WORKER,
    sample_history,
)

ORIGIN = "http://127.0.0.1:8790"
API = "/api/v1"
NAMESPACE = "piceli-system"
#: A status as the composition controller publishes it (full ref names,
#: trigger, last action, verification, pending plan, registry use).
STATUS: dict[str, Any] = {
    "schema": "piceli.gitops-status.v1",
    "controller": {
        "state": "running",
        "last_poll": "2026-10-01T09:30:00Z",
        "poll_seconds": 60,
        "poll_failures": 0,
        "composition": "shop",
        "environments": [
            {"name": "main", "promote": False},
            {"name": "rc", "promote": True},
        ],
        "registry_usage": {
            "used_bytes": 553_889_792,
            "used_source": "du",
            "claim": "piceli-registry-storage",
            "measured_at": "2026-10-01T09:25:00Z",
        },
    },
    "sources": {
        "product": {
            "url": "https://git.example/shop/product.git",
            "refs": {
                "refs/heads/main": PRODUCT,
                "refs/heads/wp-login": BRANCH,
                "refs/tags/v1.4.0": PRODUCT,
            },
            "last_poll": "2026-10-01T09:30:00Z",
        },
        "assets": {
            "url": "https://git.example/shop/assets.git",
            "refs": {"refs/heads/main": ASSETS},
            "last_poll": "2026-10-01T09:30:00Z",
        },
    },
    "envs": {
        "main": {
            "branch": "main",
            "namespace": "piceli-test",
            "state": "deployed",
            "health": "degraded",
            "reason": "pipeline-checks-failed",
            "trigger": "checks-changed",
            "last_action": "verified",
            "revision": {"product": PRODUCT, "assets": ASSETS},
            "refs": {"product": "refs/heads/main", "assets": "refs/heads/main"},
            "last_sync": "2026-10-01T09:27:00Z",
            "verification": {
                "state": "failed",
                "trigger": "checks-changed",
                "checks_hash": "sha256:" + "f" * 64,
                "rolled": [],
                "at": "2026-10-01T09:27:00Z",
                "failed": [
                    {
                        "check": "deliberate-failure",
                        "code": "check-failed",
                        "detail": "sh exited 1, expected 0",
                    }
                ],
            },
            "components": {
                "web": {"source": "product", "commit": PRODUCT, "digest": WEB,
                        "state": "unchanged", "health": "healthy"},
                "worker": {"source": "product", "commit": PRODUCT, "digest": WORKER,
                           "state": "unchanged", "health": "healthy"},
                "media": {"source": "assets", "commit": ASSETS, "digest": MEDIA,
                          "state": "unchanged", "health": "healthy"},
            },
        },
        "rc": {
            "branch": "rc",
            "namespace": "piceli-test",
            "state": "approval-required",
            "health": "healthy",
            "trigger": "tag product/v1.4.0",
            "plan_hash": PLAN_HASH,
            "revision": {"product": PRODUCT, "assets": ASSETS},
            "pending_plan": {
                "plan_hash": PLAN_HASH,
                "combined_hash": "sha256:" + "9" * 64,
                "release": "shop-0123456789ab",
                "counts": {"update": 1, "no-op": 4},
                "changes": [{"operation": "update", "kind": "Deployment", "name": "web"}],
                "changes_total": 1,
                "create_namespace": False,
                "stop": [],
                "images": {"web": "registry.example:5000/shop/web@" + WEB},
            },
            "components": {
                "web": {"source": "product", "commit": PRODUCT, "digest": WEB,
                        "state": "rolling", "health": "healthy"},
            },
        },
        "wp-login": {
            "branch": "wp-login",
            "namespace": None,
            "state": "failed",
            "health": "unknown",
            "reason": "component-build-failed",
            "revision": {"product": BRANCH, "assets": ASSETS},
            "components": {
                "web": {"source": "product", "commit": BRANCH, "state": "failed",
                        "health": "unknown"},
            },
        },
    },
}  # fmt: skip


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
def ui(tmp_path: Path) -> Iterator[tuple[TestClient, Any, Path]]:
    history = sample_history(tmp_path / "controller-state")
    with fake_cluster(FakeAPI(namespace=NAMESPACE)) as cluster:
        config = cluster.kubeconfig(tmp_path / "config")
        # The controller's side: what CompositionController._publish writes.
        with connect(config, "fake", transport="loopback-http") as api:
            controller = ConfigMapChannel(api.client, NAMESPACE)
            controller.publish(STATUS)
            controller.publish_history(history)
        target = KubeconfigTarget(config, "fake", NAMESPACE, transport="loopback-http")
        query = QueryService([Registration("cluster", NAMESPACE, target)])

        @contextmanager
        def channel() -> Iterator[ConfigMapChannel]:
            with connect(config, "fake", transport="loopback-http") as api:
                yield ConfigMapChannel(api.client, NAMESPACE)

        static = tmp_path / "assets"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><html><body></body></html>")
        app = create_app(
            query,
            origin=ORIGIN,
            static_dir=static,
            composition_control=CompositionControl(query, "cluster", channel),
            launch_token="t" * 43,
        )
        client = TestClient(app, base_url=ORIGIN)
        assert client.get(f"{API}/composition/history").status_code == 403
        assert client.get("/?token=" + "t" * 43).status_code == 200
        yield client, cluster, tmp_path


def test_history_lists_each_environments_runs_newest_first(
    ui: tuple[TestClient, Any, Path],
) -> None:
    client, cluster, _ = ui
    before = len(cluster.api.requests)
    actions = client.get(f"{API}/capabilities").json()["actions"]
    assert actions["composition_history"]["allowed"] is True
    body = client.get(f"{API}/composition/history").json()
    assert body["configured"] is True and body["available"] is True
    assert body["generated_at"] == "2026-10-01T09:30:00Z"
    envs = {item["name"]: item["runs"] for item in body["environments"]}
    assert set(envs) == {"main", "rc", "wp-login"}
    main = envs["main"]
    assert [run["state"] for run in main] == ["degraded", "deployed", "deployed"]
    degraded, deployed, first = main
    # The failing check: what failed, how long it took, nothing rolled.
    assert degraded["trigger"] == "checks-changed" and degraded["rolled"] == []
    failing = [item for item in degraded["checks"]["results"] if not item["passed"]]
    assert failing == [
        {
            "name": "deliberate-failure",
            "type": "exec",
            "passed": False,
            "code": "check-failed",
            "detail": "sh exited 1, expected 0",
            "duration": 0.31,
        }
    ]
    assert degraded["failure"]["failed_checks"] == [
        {"name": "deliberate-failure", "code": "check-failed"}
    ]
    assert degraded["verification"]["state"] == "failed"
    # The deploy approved with the CLI: sources, plan, rolled and built.
    assert deployed["approved_by"] == {"via": "cli", "at": "2026-10-01T09:19:30Z"}
    assert deployed["trigger"] == "push product/main"
    assert deployed["plan_hash"] == PLAN_HASH
    assert deployed["combined_hash"] == "sha256:" + "e" * 63 + "2"
    assert {item["name"]: item["commit"] for item in deployed["sources"]} == {
        "assets": ASSETS,
        "product": PRODUCT,
    }
    assert deployed["rolled"] == ["web"] and deployed["built"] == ["web"]
    assert deployed["unchanged"] == ["media", "worker"]
    assert {
        item["name"]: (item["digest"], item["built"]) for item in deployed["components"]
    } == {
        "media": (MEDIA, False),
        "web": (WEB, True),
        "worker": (WORKER, False),
    }
    assert deployed["plan"]["changes"] == [
        {"operation": "update", "kind": "Deployment", "name": "web"}
    ]
    assert {stage["name"]: stage["state"] for stage in deployed["stages"]}[
        "apply"
    ] == "done"
    # A run journal from before the controller kept events, approved by policy.
    assert first["recorded_by"] == "run" and first["approved_by"]["via"] == "policy"
    assert first["plan"]["changes_total"] == 3
    # A failed build has no run, but its log tail.
    (failed,) = envs["wp-login"]
    assert failed["state"] == "failed" and failed["run_id"] is None
    assert failed["failure"]["reason"] == "component-build-failed"
    assert "missing script: build" in failed["failure"]["log_tail"]
    assert envs["rc"] == []
    one = client.get(f"{API}/composition/environments/main/history").json()
    assert [env["name"] for env in one["environments"]] == ["main"]
    assert one["environments"][0]["runs"] == main
    missing = client.get(f"{API}/composition/environments/gone/history")
    assert missing.status_code == 404
    assert missing.json()["code"] == "ui-sync-target-unknown"
    # Reading the history never writes.
    assert all(request["method"] == "GET" for request in cluster.api.requests[before:])


def test_without_a_published_history_the_ui_says_so(
    ui: tuple[TestClient, Any, Path],
) -> None:
    client, cluster, _ = ui
    del cluster.api.objects[("ConfigMap", HISTORY_CONFIGMAP)]
    body = client.get(f"{API}/composition/history").json()
    assert body["configured"] is True and body["available"] is False
    assert all(env["runs"] == [] for env in body["environments"])


def test_unknown_fields_and_secret_shaped_values_are_dropped(
    ui: tuple[TestClient, Any, Path],
) -> None:
    client, cluster, tmp_path = ui
    history = sample_history(tmp_path / "other-state")
    run = copy.deepcopy(history["envs"]["main"]["runs"][1])
    run["kubeconfig"] = "/home/someone/.kube/config"
    run["password"] = "hunter2"
    run["plan_hash"] = "not-a-hash"
    run["components"][0]["digest"] = "token=abc"
    history["envs"]["main"]["runs"] = [run]
    with connect(
        cluster.kubeconfig(tmp_path / "c2"), "fake", transport="loopback-http"
    ) as api:
        ConfigMapChannel(api.client, NAMESPACE).publish_history(history)
    text = client.get(f"{API}/composition/history").text
    assert "hunter2" not in text and ".kube" not in text and "token=abc" not in text
    (main,) = [
        env["runs"]
        for env in client.get(f"{API}/composition/history").json()["environments"]
        if env["name"] == "main"
    ]
    assert main[0]["plan_hash"] is None


def test_approve_reviews_the_pending_plan_and_records_the_ui(
    ui: tuple[TestClient, Any, Path],
) -> None:
    client, cluster, _ = ui
    rc = client.get(f"{API}/composition/environments/rc").json()["environment"]
    assert rc["pending_plan"]["changes"] == [
        {"operation": "update", "kind": "Deployment", "name": "web"}
    ]
    assert rc["trigger"] == "tag product/v1.4.0"
    options = client.get(f"{API}/composition/environments/rc/actions").json()
    assert options["approve"]["allowed"] is True and options["plan_hash"] == PLAN_HASH
    assert options["pending_plan"]["plan_hash"] == PLAN_HASH
    assert options["pending_plan"]["combined_hash"] == "sha256:" + "9" * 64
    assert options["pending_plan"]["counts"] == {"update": 1, "no-op": 4}
    assert options["promote"]["allowed"] is True
    assert {item["branch"] for item in options["refs"]} == {"main", "wp-login"}
    stale = client.post(
        f"{API}/composition/environments/rc/approvals",
        json={"plan_hash": "sha256:" + "0" * 64},
        headers=_headers(client),
    )
    assert stale.status_code == 409 and stale.json()["code"] == "ui-plan-stale"
    approved = client.post(
        f"{API}/composition/environments/rc/approvals",
        json={"plan_hash": PLAN_HASH},
        headers=_headers(client),
    )
    assert approved.status_code == 202
    (body,) = [
        value for value in _requests(cluster).values() if value["kind"] == "approve"
    ]
    assert body == {
        "schema": "piceli.gitops-request.v1",
        "kind": "approve",
        "env": "rc",
        "plan_hash": PLAN_HASH,
        "via": "ui",
    }
    # Main is not waiting: no approval, and its pending plan is never shown.
    main = client.get(f"{API}/composition/environments/main/actions").json()
    assert main["approve"]["allowed"] is False and main["pending_plan"] is None


def test_verification_state_and_registry_use_are_shown(
    ui: tuple[TestClient, Any, Path],
) -> None:
    client, _, _ = ui
    main = client.get(f"{API}/composition/environments/main").json()["environment"]
    assert main["health"] == "degraded" and main["last_action"] == "verified"
    assert main["verification"]["failed"] == [
        {"check": "deliberate-failure", "code": "check-failed"}
    ]
    sources = {
        item["name"]: item
        for item in client.get(f"{API}/composition").json()["sources"]
    }
    assert sources["product"]["refs"]["refs/heads/main"] == PRODUCT
    assert sources["product"]["last_poll"] == "2026-10-01T09:30:00Z"


def _requests(cluster: Any) -> dict[str, Any]:
    import json

    found = cluster.api.objects[("ConfigMap", "piceli-gitops-requests")]["data"]
    return {key: json.loads(value) for key, value in found.items()}


def test_the_installed_ui_may_read_the_history_configmap_only() -> None:
    cluster = Cluster(
        "my-cluster",
        api="https://192.0.2.1:6443",
        credentials="my-cluster",
        controller=Controller(on="node-a", image="r.example/p@sha256:" + "a" * 64),
        ui=Ui(),
    )
    (role,) = [
        item
        for item in render_ui(cluster)
        if item["kind"] == "Role" and item["metadata"]["namespace"] == NAMESPACE
    ]
    reads = [
        rule
        for rule in role["rules"]
        if rule["resources"] == ["configmaps"] and rule["verbs"] == ["get"]
    ]
    assert len(reads) == 1 and HISTORY_CONFIGMAP in reads[0]["resourceNames"]
    writes = [
        rule
        for rule in role["rules"]
        if rule["resources"] == ["configmaps"] and rule["verbs"] != ["get"]
    ]
    assert all(HISTORY_CONFIGMAP not in rule["resourceNames"] for rule in writes)
