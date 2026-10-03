"""Disposable history exercises real read APIs without enabling release writes."""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.server.app import create_app
from piceli.services.query import QueryService
from piceli.services.registration import Registration
from piceli.testing import fake_cluster
from tests.browser.showcase_delivery import showcase_delivery
from tests.browser.showcase_resources import seed_resources

ORIGIN = "http://127.0.0.1:8000"
API = "/api/v1"


def test_showcase_plan_and_history_are_readable_without_enabling_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_process(*args: object, **kwargs: object) -> None:
        pytest.fail("The read-only preview must not launch Docker or source processes")

    monkeypatch.setattr(subprocess, "Popen", no_process)
    with fake_cluster() as cluster:
        seed_resources(cluster.api)
        before = copy.deepcopy(cluster.api.objects)
        query = QueryService(
            [
                Registration(
                    "shop",
                    "Shop",
                    KubeconfigTarget(
                        cluster.kubeconfig(tmp_path / "kubeconfig"),
                        "fake",
                        cluster.namespace,
                        transport="loopback-http",
                    ),
                    definition_kind="pipeline",
                    ownership="native",
                )
            ]
        )
        operations = showcase_delivery(query, tmp_path / "delivery")
        server = create_app(query, operations=operations)
        with TestClient(server, base_url=ORIGIN) as client:
            client.get(f"/?token={server.state.security.launch_token}")
            headers = {
                "Origin": ORIGIN,
                "X-Piceli-CSRF": next(
                    value
                    for name, value in client.cookies.items()
                    if name.startswith("piceli_csrf_")
                ),
            }
            app = client.get(API + "/applications/shop").json()
            assert app["definition_kind"] == "pipeline"
            assert app["capabilities"]["activity"]["allowed"] is True
            for name in ("evaluate", "plan", "deploy", "rollback"):
                assert app["capabilities"][name]["allowed"] is False

            response = client.get(API + "/applications/shop/operations")
            assert response.status_code == 200
            history = response.json()["items"]
            assert {record["state"] for record in history} == {
                "succeeded",
                "failed",
                "cancelled",
            }
            assert all("Preview fixture" in record["actor"] for record in history)
            assert {record["plan_id"] for record in history} == {
                "showcase-plan",
                "showcase-plan-previous",
            }
            archived = client.get(API + "/applications/shop/plans")
            assert archived.status_code == 200
            archive = {item["id"]: item for item in archived.json()["items"]}
            assert set(archive) == {
                "showcase-plan",
                "showcase-plan-previous",
                "showcase-plan-unused",
            }
            assert (
                archive["showcase-plan-unused"]["expires_at"] == "2026-09-29T10:00:00Z"
            )
            assert all(
                record["plan_id"] != "showcase-plan-unused" for record in history
            )
            unused = client.get(API + "/plans/showcase-plan-unused")
            assert unused.status_code == 200
            assert "never executed" in " ".join(unused.json()["warnings"]).lower()
            for record in history:
                assert not any(
                    value["allowed"] for value in record["capabilities"].values()
                )
                assert (
                    client.get(API + "/operations/" + record["id"]).status_code == 200
                )
            plan_response = client.get(API + "/plans/showcase-plan")
            assert plan_response.status_code == 200
            plan = plan_response.json()
            assert plan["application_id"] == "shop"
            assert plan["warnings"] and "fixture" in plan["warnings"][0].lower()
            assert len(plan["diffs"]) == 3
            assert {diff["operation"] for diff in plan["diffs"]} == {"update", "create"}
            assert any(
                change["path"] == "/spec/replicas"
                for diff in plan["diffs"]
                for change in diff["changes"]
            )
            assert plan["desired_resources_complete"] is True
            assert [step["level"] for step in plan["steps"]] == [0, 0, 1]
            assert [step["resource"]["kind"] for step in plan["steps"]] == [
                "ConfigMap",
                "Service",
                "Deployment",
            ]
            assert plan["steps"][2]["dependencies"] == [plan["steps"][0]["resource"]]
            desired = {
                item["resource"]["kind"]: item["manifest"]
                for item in plan["desired_resources"]
            }
            assert desired["Deployment"]["spec"]["replicas"] == 3
            assert desired["Service"]["spec"]["ports"][0]["port"] == 80
            assert desired["ConfigMap"]["data"]["LOG_LEVEL"] == "info"

            previous = client.get(API + "/plans/showcase-plan-previous").json()
            assert previous["desired_resources_complete"] is True
            assert previous["source"]["revision"] != plan["source"]["revision"]
            assert previous["source"]["kind"] == plan["source"]["kind"] == "git"
            assert len(previous["desired_resources"]) == 2
            old_deployment = next(
                item["manifest"]
                for item in previous["desired_resources"]
                if item["resource"]["kind"] == "Deployment"
            )
            assert old_deployment["spec"]["replicas"] == 2

            failure = client.get(API + "/operations/showcase-failed").json()
            journal = failure["journal"]
            assert journal["state"] == "failed"
            assert [action["ordinal"] for action in journal["actions"]] == [0, 1, 2]
            assert journal["actions"][2]["state"] == "applied"
            assert journal["events"][-1]["state"] == "failed"
            assert journal["logs"][0]["pod"] == "api-preview"
            assert "Preview fixture" in journal["logs"][0]["lines"][0]

            for suffix, body in (
                ("evaluation-preview", {"intent": "deploy"}),
                (
                    "operations",
                    {
                        "plan_id": plan["id"],
                        "approved_digest": plan["digest"],
                        "idempotency_key": "fixture-write",
                    },
                ),
            ):
                refused = client.post(
                    API + "/applications/shop/" + suffix, headers=headers, json=body
                )
                assert refused.status_code == 409
                assert refused.json()["code"] == "ui-operation-unavailable"
            refused = client.post(
                API + "/operations/showcase-failed/resume",
                headers=headers,
                json={
                    "approved_digest": plan["digest"],
                    "idempotency_key": "fixture-resume",
                },
            )
            assert refused.status_code == 409
            assert (
                len(client.get(API + "/applications/shop/operations").json()["items"])
                == 4
            )

            resources = client.get(API + "/applications/shop/resources").json()["items"]
            assert len(resources) == 6
            assert all(
                resource["capabilities"]["manifest"]["allowed"] is False
                for resource in resources
            )
            assert cluster.api.objects == before
            assert not any(
                request["method"] in {"POST", "PATCH", "DELETE"}
                for request in cluster.api.requests
            )
        assert operations._thread is None
        assert operations.store._dispatcher is None
