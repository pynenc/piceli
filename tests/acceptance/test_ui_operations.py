"""Independent HTTP approval/recovery tests using the real release engine.

Only source rendering is a deterministic trusted test fixture here. Docker
isolation and actual source execution have separate opt-in acceptance tests.
Kubernetes discovery, planning, writes, journal and state are real.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from piceli.artifacts.process import ToolPin
from piceli.k8s.release_spec import ReleaseSpec
from piceli.server.app import create_app
from piceli.services.contracts import CancelRequest, Principal
from piceli.services.engine import EngineAdapter
from piceli.services.evaluation import (
    DockerEvaluator,
    RenderedComposition,
    RendererConfig,
    SourceSelection,
)
from piceli.services.operations import OperationService
from piceli.services.query import QueryService
from piceli.services.registration import Registration
from piceli.services.store import Store
from piceli.state.errors import StateError
from piceli.testing import fake_cluster, manifest

ORIGIN = "http://127.0.0.1:8000"
API = "/api/v1"


class FixtureRenderer(DockerEvaluator):
    """Return known IR, never import consumer source into this test process."""

    def available(self) -> bool:
        return True

    def render(
        self, preview_id: str, approved_digest: str, *, cancel: Any = None
    ) -> RenderedComposition:
        stage = self.store / preview_id
        record = json.loads((stage / "record.json").read_text())
        assert record["preview"]["digest"] == approved_digest
        inputs = json.loads((stage / "request.json").read_text())["inputs"]
        resource = manifest("Deployment", "worker")
        resource["metadata"] = {"name": "worker", "namespace": inputs["namespace"]}
        resource.pop("status", None)
        resource["spec"]["template"]["spec"]["containers"][0]["image"] = inputs[
            "images"
        ]["api"]["ref"]
        return RenderedComposition.from_dict(
            {
                "preview_id": preview_id,
                "digest": approved_digest,
                "source": record["preview"]["source"],
                "inputs": inputs,
                "components": [
                    {
                        "name": "worker",
                        "dependencies": [],
                        "resources": [
                            {"manifest": resource, "dependencies": [], "bindings": []}
                        ],
                    }
                ],
            }
        )


def service(
    directory: Path, kubeconfig: Path, namespace: str, *, readiness: int = 1
) -> tuple[QueryService, OperationService]:
    source = directory / "definition.py"
    source.write_text("raise AssertionError('source must never execute in the API')\n")
    spec = ReleaseSpec.from_dict(
        {
            "target": {
                "kubeconfig": str(kubeconfig),
                "context": "fake",
                "namespace": namespace,
                "transport": "loopback-http",
            },
            "release": {
                "name": "shop",
                "owner": "shop-ui",
                "field_manager": "shop-ui",
                "composition": "definition.py:build",
                "state_dir": "state",
            },
            "images": {"api": "registry.example/api@sha256:" + "1" * 64},
            "execution": {
                "max_seconds": readiness + 5,
                "readiness_seconds": readiness,
                "poll_seconds": 0.05,
            },
        },
        directory,
    )
    query = QueryService([Registration.from_release("shop", "Shop", spec)])
    renderer = FixtureRenderer(
        directory / "evaluations",
        RendererConfig(
            image_id="sha256:" + "a" * 64,
            platform="linux/arm64",
            docker=ToolPin.capture(Path(sys.executable)),
            socket=directory / "unused.sock",
        ),
    )
    operations = OperationService(
        query,
        Store(directory / "control/operations.sqlite3"),
        renderer,
        {"shop": SourceSelection(directory, ("definition.py",), "definition.py:build")},
        principal=Principal(id="local", name="Local user"),
    )
    return query, operations


def headers(client: TestClient) -> dict[str, str]:
    return {
        "Origin": str(client.base_url).rstrip("/"),
        "X-Piceli-CSRF": client.cookies["piceli_csrf"],
    }


def post(
    client: TestClient, path: str, body: dict[str, Any], status: int = 202
) -> dict[str, Any]:
    response = client.post(API + path, json=body, headers=headers(client))
    assert response.status_code == status, response.text
    return response.json()


def wait(client: TestClient, path: str) -> dict[str, Any]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(API + path)
        assert response.status_code == 200, response.text
        record = response.json()
        if record["state"] not in {"queued", "running", "cancelling"}:
            return record
        time.sleep(0.02)
    raise AssertionError("operation did not reach a terminal state")


def plan(client: TestClient, key: str = "evaluate") -> dict[str, Any]:
    preview = post(
        client, "/applications/shop/evaluation-preview", {"intent": "deploy"}, 200
    )
    evaluation = post(
        client,
        "/applications/shop/evaluations",
        {
            "preview_id": preview["id"],
            "approved_digest": preview["digest"],
            "idempotency_key": key,
        },
    )
    result = wait(client, "/evaluations/" + evaluation["id"])
    assert result["state"] == "succeeded", result
    response = client.get(API + "/plans/" + result["plan_id"])
    assert response.status_code == 200, response.text
    return response.json()


def test_exact_approval_duplicate_admission_and_restart_do_not_repeat_writes(
    tmp_path: Path,
) -> None:
    with fake_cluster() as cluster:
        config = cluster.kubeconfig(tmp_path / "kubeconfig")
        query, operations = service(tmp_path, config, cluster.namespace)
        with TestClient(
            create_app(query, operations=operations), base_url=ORIGIN
        ) as client:
            assert client.get("/").status_code == 200
            reviewed = plan(client)
            assert ("Deployment", "worker") not in cluster.api.objects
            body = {
                "plan_id": reviewed["id"],
                "approved_digest": reviewed["digest"],
                "idempotency_key": "deploy-once",
            }
            rejected = post(
                client,
                "/applications/shop/operations",
                {**body, "approved_digest": "0" * 64},
                409,
            )
            assert rejected["code"] == "ui-approval-mismatch"
            admitted = post(client, "/applications/shop/operations", body)
            assert (
                post(client, "/applications/shop/operations", body)["id"]
                == admitted["id"]
            )
            finished = wait(client, "/operations/" + admitted["id"])
            assert finished["state"] == "succeeded", finished
            assert finished["engine_execution_id"]
            assert cluster.api.objects[("Deployment", "worker")]["spec"]["template"][
                "spec"
            ]["containers"][0]["image"].endswith("1" * 64)
        writes = [
            r
            for r in cluster.api.requests
            if r["method"] in {"PATCH", "POST", "DELETE"}
        ]
        query, restarted = service(tmp_path, config, cluster.namespace)
        with TestClient(
            create_app(query, operations=restarted), base_url=ORIGIN
        ) as client:
            client.get("/")
            recovered = client.get(API + "/operations/" + admitted["id"]).json()
            assert recovered["state"] == "succeeded"
            assert (
                post(client, "/applications/shop/operations", body)["id"]
                == admitted["id"]
            )
            conflict = post(
                client,
                "/applications/shop/operations",
                {**body, "approved_digest": "f" * 64},
                409,
            )
            assert conflict["code"] == "ui-idempotency-conflict"
        assert [
            r
            for r in cluster.api.requests
            if r["method"] in {"PATCH", "POST", "DELETE"}
        ] == writes


def test_live_resource_appearing_after_review_is_never_overwritten(
    tmp_path: Path,
) -> None:
    with fake_cluster() as cluster:
        query, operations = service(
            tmp_path, cluster.kubeconfig(tmp_path / "kubeconfig"), cluster.namespace
        )
        with TestClient(
            create_app(query, operations=operations), base_url=ORIGIN
        ) as client:
            client.get("/")
            reviewed = plan(client)
            foreign = manifest("Deployment", "worker")
            foreign["spec"]["replicas"] = 7
            cluster.api.put(foreign, owned=False)
            admitted = post(
                client,
                "/applications/shop/operations",
                {
                    "plan_id": reviewed["id"],
                    "approved_digest": reviewed["digest"],
                    "idempotency_key": "stale",
                },
            )
            finished = wait(client, "/operations/" + admitted["id"])
            assert finished["state"] in {"failed", "interrupted"}, finished
            assert (
                cluster.api.objects[("Deployment", "worker")]["spec"]["replicas"] == 7
            )


def _child_server(
    directory: str, kubeconfig: str, namespace: str, descriptor: int
) -> None:
    """A real API/dispatcher process for abrupt-death acceptance."""
    import uvicorn

    listener = socket.socket(fileno=descriptor)
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    query, operations = service(
        Path(directory), Path(kubeconfig), namespace, readiness=20
    )
    app = create_app(query, operations=operations, origin=origin)
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[listener])


@contextmanager
def child_server(
    directory: Path, kubeconfig: Path, namespace: str
) -> Iterator[tuple[httpx.Client, subprocess.Popen[bytes]]]:
    with socket.socket() as listener, tempfile.TemporaryFile() as output:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        code = (
            "import sys; from tests.acceptance.test_ui_operations import _child_server; "
            "_child_server(sys.argv[1],sys.argv[2],sys.argv[3],int(sys.argv[4]))"
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                code,
                str(directory),
                str(kubeconfig),
                namespace,
                str(listener.fileno()),
            ],
            pass_fds=(listener.fileno(),),
            stdout=output,
            stderr=output,
        )
        try:
            with httpx.Client(base_url=origin, timeout=2) as client:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    assert process.poll() is None, "API process exited during startup"
                    try:
                        if client.get("/").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.02)
                else:
                    raise AssertionError("API process did not start")
                yield client, process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            process.wait(timeout=5)


def test_api_process_death_after_write_requires_explicit_same_execution_recovery(
    tmp_path: Path,
) -> None:
    with fake_cluster() as cluster:
        config = cluster.kubeconfig(tmp_path / "kubeconfig")
        cluster.api.ready = False
        with child_server(tmp_path, config, cluster.namespace) as (client, process):
            reviewed = plan(client)
            admitted = post(
                client,
                "/applications/shop/operations",
                {
                    "plan_id": reviewed["id"],
                    "approved_digest": reviewed["digest"],
                    "idempotency_key": "crash-write",
                },
            )
            deadline = time.monotonic() + 10
            while (
                "Deployment",
                "worker",
            ) not in cluster.api.objects and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ("Deployment", "worker") in cluster.api.objects
            process.kill()
            process.wait(timeout=5)
        before = len(
            [
                r
                for r in cluster.api.requests
                if r["method"] == "PATCH" and not r.get("dry_run")
            ]
        )
        with child_server(tmp_path, config, cluster.namespace) as (client, _process):
            original = client.get(API + "/operations/" + admitted["id"]).json()
            assert original["state"] == "interrupted", original
            assert original["engine_execution_id"]
            time.sleep(0.1)
            assert (
                len(
                    [
                        r
                        for r in cluster.api.requests
                        if r["method"] == "PATCH" and not r.get("dry_run")
                    ]
                )
                == before
            )
            cluster.api.ready = True
            recovered = post(
                client,
                "/operations/" + original["id"] + "/resume",
                {
                    "approved_digest": reviewed["digest"],
                    "idempotency_key": "explicit-recovery",
                },
            )
            assert recovered["id"] != original["id"]
            assert recovered["recovery_of"] == original["id"]
            finished = wait(client, "/operations/" + recovered["id"])
            assert finished["state"] == "succeeded", finished
            assert finished["engine_execution_id"] == original["engine_execution_id"]
            assert (
                client.get(API + "/operations/" + original["id"]).json()["state"]
                == "interrupted"
            )


def test_event_stream_replays_only_its_scope_and_resets_expired_cursor(
    tmp_path: Path,
) -> None:
    def first_event(client: httpx.Client, after: str) -> dict[str, Any]:
        with client.stream(
            "GET", API + f"/events?application_id=shop&after={after}"
        ) as response:
            assert response.status_code == 200, response.text
            assert response.headers["content-type"].startswith("text/event-stream")
            for line in response.iter_lines():
                if line.startswith("data: "):
                    return json.loads(line[6:])
        raise AssertionError("event stream ended without a frame")

    with fake_cluster() as cluster:
        config = cluster.kubeconfig(tmp_path / "kubeconfig")
        with child_server(tmp_path, config, cluster.namespace) as (client, _process):
            plan(client)
            event = first_event(client, "0")
            assert event["scope"] == "shop"
            assert event["kind"] == "upsert" and event["subject_id"]
            assert client.get(API + "/events?application_id=other").status_code == 404
            assert (
                client.get(API + "/events?application_id=shop&after=bad").status_code
                == 422
            )
            store = Store(tmp_path / "control/operations.sqlite3")
            with store.transaction() as connection:
                connection.execute(
                    "DELETE FROM outbox WHERE sequence < (SELECT MAX(sequence) FROM outbox)"
                )
            reset = first_event(client, "0")
            assert reset["kind"] == "reset"
            assert reset["scope"] == "shop"
            assert reset["subject_id"] is None


def test_cancelling_failed_parent_fences_standing_policy_rollback_before_write(
    tmp_path: Path,
) -> None:
    class ControlledEngine(EngineAdapter):
        service: OperationService
        rollback_writes = 0

        def execute(
            self,
            reviewed: Any,
            _private: Any,
            *,
            on_start: Any,
            on_progress: Any,
            **_kwargs: Any,
        ) -> Any:
            on_start(
                {
                    "plan_hash": reviewed.engine_digest,
                    "execution_id": "original",
                    "release": reviewed.release,
                }
            )
            parent = self.service.store.active("operation")[0]
            self.service.cancel(
                parent["id"], CancelRequest(idempotency_key="stop-after-checks")
            )
            # Existing release engine calls the hook before any rollback write.
            on_start(
                {
                    "plan_hash": "f" * 64,
                    "execution_id": "rollback",
                    "release": "earlier",
                    "trigger": "checks-failed",
                }
            )
            self.rollback_writes += 1
            return {"release_state": "ready"}

        def inspect(
            self, _plan: Any, _private: Any, _execution_id: str
        ) -> dict[str, Any]:
            return {
                "release_state": "checks-failed",
                "execution": {"state": "ready", "execution_id": "original"},
                "checks": {"passed": False},
            }

        def policy_plan(self, *_args: Any) -> Any:
            raise AssertionError("cancelled policy rollback must never prepare a child")

    with fake_cluster() as cluster:
        query, operations = service(
            tmp_path, cluster.kubeconfig(tmp_path / "kubeconfig"), cluster.namespace
        )
        engine = ControlledEngine()
        engine.service = operations
        operations.engine = engine
        with TestClient(
            create_app(query, operations=operations), base_url=ORIGIN
        ) as client:
            client.get("/")
            reviewed = plan(client)
            admitted = post(
                client,
                "/applications/shop/operations",
                {
                    "plan_id": reviewed["id"],
                    "approved_digest": reviewed["digest"],
                    "idempotency_key": "policy-fence",
                },
            )
            parent = wait(client, "/operations/" + admitted["id"])
            assert parent["state"] == "failed", parent
            assert engine.rollback_writes == 0
            assert not any(
                item["recovery_of"] == parent["id"]
                for item in client.get(API + "/applications/shop/operations").json()[
                    "items"
                ]
            )


def test_lease_loss_after_execution_start_never_invents_success(
    tmp_path: Path,
) -> None:
    class LostLease(EngineAdapter):
        dispatches = 0

        def execute(
            self,
            reviewed: Any,
            _private: Any,
            *,
            on_start: Any,
            on_progress: Any,
            **_kwargs: Any,
        ) -> Any:
            self.dispatches += 1
            on_start(
                {
                    "plan_hash": reviewed.engine_digest,
                    "execution_id": "lease-lost",
                    "release": reviewed.release,
                }
            )
            raise StateError("state-lease-lost", "fenced before the next write")

    with fake_cluster() as cluster:
        config = cluster.kubeconfig(tmp_path / "kubeconfig")
        query, operations = service(tmp_path, config, cluster.namespace)
        engine = LostLease()
        operations.engine = engine
        with TestClient(
            create_app(query, operations=operations), base_url=ORIGIN
        ) as client:
            client.get("/")
            reviewed = plan(client)
            admitted = post(
                client,
                "/applications/shop/operations",
                {
                    "plan_id": reviewed["id"],
                    "approved_digest": reviewed["digest"],
                    "idempotency_key": "lease-loss",
                },
            )
            finished = wait(client, "/operations/" + admitted["id"])
            assert finished["state"] == "failed", finished
            assert finished["engine_execution_id"] == "lease-lost"
            assert finished["deployment_outcome"] != "succeeded"
            assert finished["error_code"] == "ui-execution-failed"
            assert engine.dispatches == 1
            assert ("Deployment", "worker") not in cluster.api.objects
        query, restarted = service(tmp_path, config, cluster.namespace)
        restarted.engine = engine
        with TestClient(
            create_app(query, operations=restarted), base_url=ORIGIN
        ) as client:
            client.get("/")
            assert (
                client.get(API + "/operations/" + admitted["id"]).json()["state"]
                == "failed"
            )
            assert engine.dispatches == 1


def test_failed_standing_policy_rollback_is_a_separate_failed_operation(
    tmp_path: Path,
) -> None:
    class FailedRollback(EngineAdapter):
        def execute(
            self,
            reviewed: Any,
            _private: Any,
            *,
            on_start: Any,
            on_progress: Any,
            **_kwargs: Any,
        ) -> Any:
            on_start(
                {
                    "plan_hash": reviewed.engine_digest,
                    "execution_id": "failed-checks",
                    "release": reviewed.release,
                }
            )
            on_start(
                {
                    "plan_hash": "e" * 64,
                    "execution_id": "failed-rollback",
                    "release": "prior",
                    "trigger": "checks-failed",
                }
            )
            return {
                "release_state": "checks-failed",
                "execution": {"state": "ready", "execution_id": "failed-checks"},
                "checks": {"passed": False},
                "rollback": {
                    "release_state": "failed",
                    "execution": {"state": "failed", "execution_id": "failed-rollback"},
                },
            }

        def policy_plan(self, parent: Any, private: Any, entry: Any) -> tuple[Any, Any]:
            return (
                parent.model_copy(
                    update={
                        "id": "e" * 64,
                        "digest": "e" * 64,
                        "engine_digest": entry["plan_hash"],
                        "intent": "rollback",
                        "release": entry["release"],
                        "authorization": "policy",
                        "policy_digest": parent.digest,
                    }
                ),
                private,
            )

    with fake_cluster() as cluster:
        query, operations = service(
            tmp_path, cluster.kubeconfig(tmp_path / "kubeconfig"), cluster.namespace
        )
        operations.engine = FailedRollback()
        with TestClient(
            create_app(query, operations=operations), base_url=ORIGIN
        ) as client:
            client.get("/")
            reviewed = plan(client)
            admitted = post(
                client,
                "/applications/shop/operations",
                {
                    "plan_id": reviewed["id"],
                    "approved_digest": reviewed["digest"],
                    "idempotency_key": "failed-policy-rollback",
                },
            )
            parent = wait(client, "/operations/" + admitted["id"])
            assert parent["state"] == "failed", parent
            children = [
                item
                for item in client.get(API + "/applications/shop/operations").json()[
                    "items"
                ]
                if item["recovery_of"] == parent["id"]
            ]
            assert len(children) == 1
            assert children[0]["state"] == "failed"
            assert children[0]["engine_execution_id"] == "failed-rollback"
            assert children[0]["deployment_outcome"] == "failed"
