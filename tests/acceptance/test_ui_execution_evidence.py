"""Real approved release execution exposes safe plan and journal evidence."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from piceli.server.app import create_app
from piceli.services.contracts import Operation, PlanRecord
from piceli.testing import fake_cluster
from tests.acceptance.test_ui_operations import (
    API,
    ORIGIN,
    launch,
    plan,
    post,
    service,
    wait,
)


def test_real_plan_and_execution_preserve_ordered_read_only_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with fake_cluster() as cluster:
        query, operations = service(
            tmp_path, cluster.kubeconfig(tmp_path / "kubeconfig"), cluster.namespace
        )
        with TestClient(
            create_app(query, operations=operations), base_url=ORIGIN
        ) as client:
            assert launch(client).status_code == 200
            reviewed = plan(client)
            assert [
                (
                    step["ordinal"],
                    step["level"],
                    step["resource"]["name"],
                    step["operation"],
                )
                for step in reviewed["steps"]
            ] == [(0, 0, "worker", "create")]
            assert reviewed["desired_resources_complete"] is True
            assert reviewed["desired_resources"][0]["resource"]["name"] == "worker"
            assert reviewed["desired_resources"][0]["manifest"]["kind"] == "Deployment"
            admitted = post(
                client,
                "/applications/shop/operations",
                {
                    "plan_id": reviewed["id"],
                    "approved_digest": reviewed["digest"],
                    "idempotency_key": "evidence",
                },
            )
            finished = wait(client, "/operations/" + admitted["id"])
            assert finished["state"] == "succeeded"
            journal = finished["journal"]
            assert journal["execution_id"] == finished["engine_execution_id"]
            assert journal["state"] == "ready"
            assert [
                (action["ordinal"], action["resource"]["name"], action["state"])
                for action in journal["actions"]
            ] == [(0, "worker", "ready")]
            assert [event["state"] for event in journal["events"]] == [
                "intent",
                "applied",
                "ready",
            ]
            assert journal["logs"] == []
            before = len(
                [
                    request
                    for request in cluster.api.requests
                    if request["method"] in {"POST", "PATCH", "DELETE"}
                ]
            )
            assert (
                client.get(API + "/operations/" + admitted["id"]).json()["journal"]
                == journal
            )
            assert (
                len(
                    [
                        request
                        for request in cluster.api.requests
                        if request["method"] in {"POST", "PATCH", "DELETE"}
                    ]
                )
                == before
            )

            # Existing durable records remain readable; absent snapshots never
            # turn into claims that no resources changed.
            older_plan = {
                key: value
                for key, value in reviewed.items()
                if key
                not in {"steps", "desired_resources", "desired_resources_complete"}
            }
            legacy = PlanRecord.model_validate(older_plan)
            assert legacy.steps == [] and legacy.desired_resources == []
            assert legacy.desired_resources_complete is False
            older_operation = {
                key: value for key, value in finished.items() if key != "journal"
            }
            assert Operation.model_validate(older_operation).journal is None

            journal_calls = []

            def unavailable(*args: object, **kwargs: object) -> None:
                journal_calls.append(True)
                raise OSError("private journal lookup detail")

            monkeypatch.setattr(operations.engine, "journal", unavailable)
            stored_before = operations.store.get("operation", admitted["id"])
            assert client.get(API + "/applications/shop/operations").status_code == 200
            assert journal_calls == []
            observed = client.get(API + "/operations/" + admitted["id"])
            assert observed.status_code == 200
            assert observed.json()["state"] == "succeeded"
            assert observed.json()["journal"] is None
            assert "private journal lookup detail" not in observed.text
            assert len(journal_calls) == 1
            assert operations.store.get("operation", admitted["id"]) == stored_before
