"""Browser Pipeline approval uses the existing runner and explicit fake target."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from piceli import App, Pipeline, Target
from piceli.app.render import load_target
from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.server.app import create_app
from piceli.services.pipeline_control import PipelineControl
from piceli.services.query import QueryService
from piceli.services.registration import Registration
from piceli.services.store import Store
from piceli.testing import fake_cluster
from tests.acceptance.fake_api import TARGET

IMAGE = "registry.example/shop@sha256:" + "a" * 64


def _headers(client: TestClient) -> dict[str, str]:
    return {
        "Origin": "http://127.0.0.1:8000",
        "X-Piceli-CSRF": next(
            value
            for name, value in client.cookies.items()
            if name.startswith("piceli_csrf_")
        ),
    }


def test_materialized_pipeline_replans_exact_hash_before_apply(tmp_path: Path) -> None:
    with fake_cluster() as cluster:
        kubeconfig = cluster.kubeconfig(tmp_path / "target.kubeconfig")
        app = App("shop")
        web = app.deployment("web", image=IMAGE, ports=[8080])
        app.pre_rollout(web, ["web", "check-config"])
        cluster.api.job_result("web-cfg", exit_code=0)
        pipeline = Pipeline(
            app,
            Target.kubeconfig(
                kubeconfig,
                context="fake",
                namespace=TARGET.namespace,
                transport="loopback-http",
            ),
            state_dir=tmp_path / "pipeline-state",
            execution={"max_seconds": 30, "readiness_seconds": 1, "poll_seconds": 0.05},
        )
        query = QueryService(
            [
                Registration(
                    "shop",
                    "Shop",
                    KubeconfigTarget(
                        kubeconfig, "fake", TARGET.namespace, transport="loopback-http"
                    ),
                    definition_kind="pipeline",
                    ownership="native",
                )
            ]
        )
        control = PipelineControl(
            query, "shop", pipeline, "shop.py:pipeline", tmp_path / "control"
        )
        server = create_app(query, pipeline_control=control)
        with TestClient(server, base_url="http://127.0.0.1:8000") as client:
            client.get(f"/?token={server.state.security.launch_token}")
            planned = client.post("/api/v1/pipeline/plans", headers=_headers(client))
            assert planned.status_code == 200, planned.text
            plan = planned.json()
            assert plan["materialized"] is True
            assert any(
                stage["name"] == "prerollout" and stage["checks"]
                for stage in plan["stages"]
            )
            assert not any(
                key[0] == "Deployment" and key[1] == "web"
                for key in cluster.api.objects
            )
            wrong = client.post(
                "/api/v1/pipeline/operations",
                headers=_headers(client),
                json={
                    "plan_id": plan["id"],
                    "approved_digest": "0" * 64,
                    "idempotency_key": "wrong",
                },
            )
            assert wrong.status_code == 409
            assert wrong.json()["code"] == "ui-approval-mismatch"
            admitted = client.post(
                "/api/v1/pipeline/operations",
                headers=_headers(client),
                json={
                    "plan_id": plan["id"],
                    "approved_digest": plan["digest"],
                    "idempotency_key": "approved",
                },
            )
            assert admitted.status_code == 202, admitted.text
            run_id = admitted.json()["id"]
            for _ in range(100):
                state = client.get(f"/api/v1/pipeline/operations/{run_id}").json()
                if state["state"] not in {"queued", "running"}:
                    break
                time.sleep(0.05)
            assert state["state"] == "succeeded", state
            assert state["stages"]["prerollout"] == "done"
            assert (
                cluster.api.objects[("Deployment", "web")]["spec"]["template"]["spec"][
                    "containers"
                ][0]["image"]
                == IMAGE
            )
            stale_plan = client.post(
                "/api/v1/pipeline/plans", headers=_headers(client)
            ).json()
            app.deployment("later", image=IMAGE, ports=[8081])
            stale_admission = client.post(
                "/api/v1/pipeline/operations",
                headers=_headers(client),
                json={
                    "plan_id": stale_plan["id"],
                    "approved_digest": stale_plan["digest"],
                    "idempotency_key": "stale",
                },
            )
            assert stale_admission.status_code == 202
            for _ in range(100):
                stale = client.get(
                    f"/api/v1/pipeline/operations/{stale_admission.json()['id']}"
                ).json()
                if stale["state"] not in {"queued", "running"}:
                    break
                time.sleep(0.05)
            assert (stale["state"], stale["error_code"]) == (
                "failed",
                "ui-plan-stale",
            )
            assert ("Deployment", "later") not in cluster.api.objects


def test_built_pipeline_stops_for_second_materialized_approval(
    shop: tuple[object, Path],
) -> None:
    api, directory = shop
    from tests.acceptance.test_deploy_pipeline import FakeBackend

    pipeline = load_target("app.py:pipeline", directory)
    assert isinstance(pipeline, Pipeline)
    kubeconfig = directory / "kubeconfig"
    query = QueryService(
        [
            Registration(
                "shop",
                "Shop",
                KubeconfigTarget(
                    kubeconfig, "fake", TARGET.namespace, transport="loopback-http"
                ),
                definition_kind="pipeline",
                ownership="native",
            )
        ]
    )
    control = PipelineControl(
        query, "shop", pipeline, "app.py:pipeline", directory / "ui-control"
    )
    server = create_app(query, pipeline_control=control)
    with TestClient(server, base_url="http://127.0.0.1:8000") as client:
        client.get(f"/?token={server.state.security.launch_token}")
        first = client.post("/api/v1/pipeline/plans", headers=_headers(client)).json()
        assert first["phase"] == "preliminary"
        assert first["materialized"] is False
        assert ("Deployment", "web") not in api.objects
        admitted = client.post(
            "/api/v1/pipeline/operations",
            headers=_headers(client),
            json={
                "plan_id": first["id"],
                "approved_digest": first["digest"],
                "idempotency_key": "preliminary",
            },
        )
        assert admitted.status_code == 202, admitted.text
        identity = admitted.json()["id"]
        for _ in range(200):
            operation = client.get(f"/api/v1/pipeline/operations/{identity}").json()
            if operation["state"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        assert operation["state"] == "awaiting-review", operation
        assert FakeBackend.calls.count("build") == 1
        assert ("Deployment", "web") not in api.objects
        second = client.get(
            f"/api/v1/pipeline/plans/{operation['next_plan_id']}"
        ).json()
        assert second["phase"] == "final" and second["materialized"] is True
        assert second["digest"] != first["digest"]
        mismatch = client.post(
            f"/api/v1/pipeline/operations/{identity}/approve",
            headers=_headers(client),
            json={
                "plan_id": second["id"],
                "approved_digest": first["digest"],
                "idempotency_key": "wrong-second",
            },
        )
        assert mismatch.status_code == 409
        approved = client.post(
            f"/api/v1/pipeline/operations/{identity}/approve",
            headers=_headers(client),
            json={
                "plan_id": second["id"],
                "approved_digest": second["digest"],
                "idempotency_key": "second",
            },
        )
        assert approved.status_code == 202, approved.text
        for _ in range(200):
            operation = client.get(f"/api/v1/pipeline/operations/{identity}").json()
            if operation["state"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        assert operation["state"] == "succeeded", operation
        assert ("Deployment", "web") in api.objects


def test_pipeline_dispatcher_restart_interrupts_unfinished_write(
    shop: tuple[object, Path],
) -> None:
    _api, directory = shop
    pipeline = load_target("app.py:pipeline", directory)
    assert isinstance(pipeline, Pipeline)
    query = QueryService(
        [
            Registration(
                "shop",
                "Shop",
                KubeconfigTarget(
                    directory / "kubeconfig",
                    "fake",
                    TARGET.namespace,
                    transport="loopback-http",
                ),
                definition_kind="pipeline",
                ownership="native",
            )
        ]
    )
    control_dir = directory / "ui-control"
    store = Store(control_dir / "pipeline-control.sqlite3")
    store.acquire_dispatcher()
    for identity, state in (("a" * 32, "running"), ("b" * 32, "awaiting-review")):
        store.put(
            "pipeline_operation",
            {
                "id": identity,
                "application_id": "shop",
                "state": state,
                "plan_id": "c" * 32,
                "stages": {},
            },
        )
    store.close()
    control = PipelineControl(query, "shop", pipeline, "app.py:pipeline", control_dir)
    control.start()
    try:
        assert control.operation("a" * 32)["state"] == "interrupted"
        assert control.operation("b" * 32)["state"] == "awaiting-review"
    finally:
        control.close()
        query.close()
