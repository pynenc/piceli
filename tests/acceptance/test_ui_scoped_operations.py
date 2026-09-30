"""A scoped request admits work that a trusted dispatcher can finish later."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.server.app import create_app
from piceli.server.cluster_security import ClusterSecurity, ClusterSecurityConfig
from piceli.services.authority import ScopePolicy, request_principal
from piceli.services.cluster_evaluation import (
    KubernetesJobEvaluator,
    KubernetesRendererConfig,
)
from piceli.services.contracts import (
    EvaluationRequest,
    OperationRequest,
    PlanRequest,
    Principal,
)
from piceli.services.operations import OperationService
from piceli.services.query import QueryError, QueryService
from piceli.services.registration import Registration
from piceli.testing import fake_cluster
from tests.acceptance.test_ui_cluster_auth import complete_login, issuer
from tests.acceptance.test_ui_operations import (
    FixtureRenderer,
    headers,
    plan,
    post,
    service,
    wait,
)


def test_worker_uses_admitted_actor_after_request_scope_ends(tmp_path: Path) -> None:
    with fake_cluster() as cluster:
        local_query, local_operations = service(
            tmp_path,
            cluster.kubeconfig(tmp_path / "explicit-kubeconfig"),
            cluster.namespace,
        )
        policy = ScopePolicy(
            {
                "operator": {
                    "shop": frozenset(
                        {"inspect", "evaluate", "plan", "deploy", "activity"}
                    )
                }
            }
        )
        query = QueryService(
            list(local_query.registrations.values()), scope_policy=policy
        )
        operations = OperationService(
            query,
            local_operations.store,
            local_operations.evaluator,
            local_operations.sources,
            principal=None,
        )
        actor = Principal(id="operator", name="Operator", kind="oidc")
        with request_principal(actor):
            preview = operations.preview("shop", PlanRequest(intent="deploy"))
            evaluation = operations.evaluate(
                "shop",
                EvaluationRequest(
                    preview_id=preview.id,
                    approved_digest=preview.digest,
                    idempotency_key="evaluate-once",
                ),
            )
        # The dispatcher has no HTTP request identity. It relies on the
        # already admitted, scoped private record and configured registration.
        operations._evaluate(evaluation.id)
        completed, _ = operations.store.get("evaluation", evaluation.id)
        assert completed["state"] == "succeeded"
        with request_principal(actor):
            reviewed = operations.plan(completed["plan_id"])
            with pytest.raises(QueryError, match="ui-not-found"):
                operations.preview(
                    "shop", PlanRequest(intent="rollback", release="prior")
                )
            preview_raw, preview_private = operations.store.get("preview", preview.id)
            rollback_preview = {**preview_raw, "id": "f" * 32, "intent": "rollback"}
            operations.store.put("preview", rollback_preview, private=preview_private)
            with pytest.raises(QueryError, match="ui-not-found"):
                operations.evaluate(
                    "shop",
                    EvaluationRequest(
                        preview_id="f" * 32,
                        approved_digest=preview.digest,
                        idempotency_key="rollback-evaluation",
                    ),
                )
            plan_raw, plan_private = operations.store.get("plan", reviewed.id)
            rollback_plan = {**plan_raw, "id": "e" * 32, "intent": "rollback"}
            operations.store.put("plan", rollback_plan, private=plan_private)
            with pytest.raises(QueryError, match="ui-not-found"):
                operations.admit(
                    "shop",
                    OperationRequest(
                        plan_id="e" * 32,
                        approved_digest=reviewed.digest,
                        idempotency_key="rollback-operation",
                    ),
                )
            admitted = operations.admit(
                "shop",
                OperationRequest(
                    plan_id=reviewed.id,
                    approved_digest=reviewed.digest,
                    idempotency_key="deploy-once",
                ),
            )
        assert admitted.actor == "operator"
        policy.replace({})
        try:
            operations.plan(reviewed.id)
        except QueryError:
            pass
        else:
            raise AssertionError("revoked principal read an approved plan")
        operations._execute(admitted.id)
        finished, _ = operations.store.get("operation", admitted.id)
        assert finished["state"] == "succeeded"
        assert finished["actor"] == "operator"
        assert ("Deployment", "worker") in cluster.api.objects
        policy.replace(
            {
                "operator": {
                    "shop": frozenset(
                        {"inspect", "evaluate", "plan", "rollback", "activity"}
                    )
                }
            }
        )
        with request_principal(actor):
            with pytest.raises(QueryError, match="ui-not-found"):
                operations.admit(
                    "shop",
                    OperationRequest(
                        plan_id=reviewed.id,
                        approved_digest=reviewed.digest,
                        idempotency_key="deploy-denied-after-grant-change",
                    ),
                )
            # The synthetic rollback plan shares the reviewed private engine
            # fixture; isolate this assertion to admission's action choice.
            operations.engine.validate = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
            rollback_admitted = operations.admit(
                "shop",
                OperationRequest(
                    plan_id="e" * 32,
                    approved_digest=reviewed.digest,
                    idempotency_key="rollback-only-operation",
                ),
            )
            assert rollback_admitted.actor == "operator"


class _FixtureClusterRenderer(KubernetesJobEvaluator):
    """Only Job execution is faked; HTTP, OIDC, planner and engine are real."""

    def available(self) -> bool:
        return True

    def render(
        self, preview_id: str, approved_digest: str, *, cancel: Any = None
    ) -> Any:
        return FixtureRenderer.render(self, preview_id, approved_digest, cancel=cancel)  # type: ignore[arg-type]


def test_oidc_manual_deploy_and_revoked_scope(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>Piceli</title>")
    with fake_cluster() as cluster, issuer() as (issuer_url, _flags):
        config = cluster.kubeconfig(tmp_path / "explicit-kubeconfig")
        local_query, local_operations = service(tmp_path, config, cluster.namespace)
        principal_id = hashlib.sha256(
            (issuer_url + "\0operator-1").encode()
        ).hexdigest()
        grants = frozenset(
            {"inspect", "evaluate", "plan", "deploy", "activity", "resume", "cancel"}
        )
        policy = ScopePolicy({principal_id: {"shop": grants}})
        query = QueryService(
            [
                *local_query.registrations.values(),
                Registration(
                    "other",
                    "Other",
                    KubeconfigTarget(
                        config, "fake", cluster.namespace, transport="loopback-http"
                    ),
                ),
            ],
            scope_policy=policy,
        )
        renderer = _FixtureClusterRenderer(
            tmp_path / "cluster-evaluations",
            KubernetesRendererConfig(
                image_id="example.invalid/renderer@sha256:" + "a" * 64,
                platform="linux/arm64",
                target=KubeconfigTarget(
                    config,
                    "fake",
                    cluster.namespace,
                    cluster_uid="cluster-uid",
                    namespace_uid="namespace-uid",
                    transport="loopback-http",
                ),
            ),
            client_factory=lambda: None,
        )
        operations = OperationService(
            query,
            local_operations.store,
            renderer,
            local_operations.sources,
            principal=None,
        )
        security = ClusterSecurity(
            ClusterSecurityConfig(
                origin="http://127.0.0.1:8000",
                issuer=issuer_url,
                metadata_url=issuer_url + "/.well-known/openid-configuration",
                client_id="piceli-test",
                allow_insecure_loopback_test=True,
            )
        )
        app = create_app(
            query,
            static_dir=tmp_path,
            operations=operations,
            cluster_security=security,
        )
        with TestClient(app, base_url="http://127.0.0.1:8000") as browser:
            assert complete_login(browser).status_code == 200
            assert browser.get("/api/v1/applications/other").status_code == 404
            reviewed = plan(browser)
            admitted = post(
                browser,
                "/applications/shop/operations",
                {
                    "plan_id": reviewed["id"],
                    "approved_digest": reviewed["digest"],
                    "idempotency_key": "oidc-deploy",
                },
            )
            finished = wait(browser, "/operations/" + admitted["id"])
            assert finished["state"] == "succeeded", finished
            assert finished["actor"] == principal_id
            assert ("Deployment", "worker") in cluster.api.objects
            policy.replace({})
            assert browser.get("/api/v1/plans/" + reviewed["id"]).status_code == 404
            assert (
                browser.get("/api/v1/operations/" + admitted["id"]).status_code == 404
            )
            assert (
                browser.get(
                    "/api/v1/observation-events?application_id=shop"
                ).status_code
                == 404
            )
            assert (
                browser.post(
                    "/api/v1/applications/shop/evaluation-preview",
                    json={"intent": "deploy"},
                    headers=headers(browser),
                ).status_code
                == 404
            )
