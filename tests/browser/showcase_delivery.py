"""Read-only delivery evidence for the explicitly disposable UI showcase.

The real service reads typed fixture records from its ordinary SQLite store.
These are illustrative records, not deployments or incidents that occurred.
No source is registered; no renderer or release write capability is enabled.
"""

from __future__ import annotations

import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from piceli.artifacts.process import ToolPin
from piceli.services.contracts import (
    DesiredResource,
    FieldChange,
    Operation,
    PlanRecord,
    PlanStep,
    Principal,
    ResourceDiff,
    ResourceIdentity,
    SourceRevision,
    Stage,
)
from piceli.services.engine import digest
from piceli.services.evaluation import DockerEvaluator, RendererConfig
from piceli.services.operations import OperationService
from piceli.services.query import QueryService
from piceli.services.store import Store


class _UnavailableRenderer(DockerEvaluator):
    """Skip all Docker discovery; this preview has no executable sources."""

    def available(self) -> bool:
        return False


PREVIOUS_REVISION = "2a8cfe419e5038c114d0b4ac42efda840de81f39"
CURRENT_REVISION = "d74e632dc981290bca241689fc0f6adeb114305a"
PREVIEW_ENTRYPOINT = "Illustrative preview; no source is registered or executed"


def _journal(
    plan: PlanRecord, execution: str, *, failed: bool = False
) -> dict[str, Any]:
    """Illustrative safe journal projection; never a running execution."""
    actions = []
    events = [{"sequence": 1, "ordinal": None, "state": "running"}]
    for step in plan.steps:
        state = "applied" if failed and step.resource.kind == "Deployment" else "ready"
        actions.append(
            {
                "ordinal": step.ordinal,
                "resource": step.resource.model_dump(mode="json"),
                "operation": step.operation,
                "state": state,
                "written_at": f"2026-10-01T08:30:{step.ordinal + 1:02d}Z"
                if failed
                else f"2026-10-01T09:15:{step.ordinal + 1:02d}Z",
            }
        )
        for transition in (
            "intent",
            "applied",
            *(["ready"] if state == "ready" else []),
        ):
            events.append(
                {
                    "sequence": len(events) + 1,
                    "ordinal": step.ordinal,
                    "state": transition,
                }
            )
    events.append(
        {
            "sequence": len(events) + 1,
            "ordinal": None,
            "state": "failed" if failed else "ready",
        }
    )
    deployment = next(
        step.resource for step in plan.steps if step.resource.kind == "Deployment"
    )
    return {
        "execution_id": execution,
        "state": "failed" if failed else "ready",
        "actions": actions,
        "events": events,
        "logs": [
            {
                "resource": deployment.model_dump(mode="json"),
                "pod": "api-preview",
                "container": "api",
                "lines": [
                    "Preview fixture: application startup completed",
                    "Preview fixture: readiness probe did not succeed before the deadline",
                ],
            }
        ]
        if failed
        else [],
        "truncated": False,
    }


def showcase_delivery(query: QueryService, directory: Path) -> OperationService:
    """Attach illustrative terminal history while preserving Shop's registration."""
    target = query.registration("shop").public_target()

    def resource(kind: str, name: str) -> ResourceIdentity:
        return ResourceIdentity(
            target_id=target.id,
            api_version="apps/v1" if kind == "Deployment" else "v1",
            kind=kind,
            name=name,
            namespace=target.namespace,
        )

    def desired(kind: str, name: str, **body: Any) -> dict[str, Any]:
        identity = resource(kind, name)
        return {
            "resource": identity.model_dump(mode="json"),
            "manifest": {
                "apiVersion": identity.api_version,
                "kind": kind,
                "metadata": {"name": name, "namespace": target.namespace},
                **body,
            },
            "not_compared": ["/status", "/metadata/uid", "/metadata/resourceVersion"],
        }

    previous_desired = [
        desired(
            "Deployment",
            "api",
            spec={
                "replicas": 2,
                "selector": {"matchLabels": {"app": "api"}},
                "template": {
                    "metadata": {"labels": {"app": "api"}},
                    "spec": {
                        "containers": [
                            {
                                "name": "api",
                                "image": "registry.example/shop/api:v1.4.0",
                                "ports": [{"containerPort": 8080}],
                            }
                        ]
                    },
                },
            },
        ),
        desired(
            "Service",
            "api",
            spec={
                "selector": {"app": "api"},
                "ports": [{"port": 8080, "targetPort": 8080}],
            },
        ),
    ]
    current_desired = deepcopy(previous_desired)
    deployment_spec = current_desired[0]["manifest"]["spec"]
    deployment_spec["replicas"] = 3
    container = deployment_spec["template"]["spec"]["containers"][0]
    container["image"] = "registry.example/shop/api:v1.5.0"
    container["envFrom"] = [{"configMapRef": {"name": "public-settings"}}]
    current_desired[1]["manifest"]["spec"]["ports"][0]["port"] = 80
    current_desired.append(
        desired(
            "ConfigMap",
            "public-settings",
            data={"LOG_LEVEL": "info", "PAGE_SIZE": "25"},
        )
    )
    steps = [
        {
            "ordinal": 0,
            "level": 0,
            "resource": resource("ConfigMap", "public-settings"),
            "operation": "create",
            "dependencies": [],
        },
        {
            "ordinal": 1,
            "level": 0,
            "resource": resource("Service", "api"),
            "operation": "update",
            "dependencies": [],
        },
        {
            "ordinal": 2,
            "level": 1,
            "resource": resource("Deployment", "api"),
            "operation": "update",
            "dependencies": [resource("ConfigMap", "public-settings")],
        },
    ]

    diffs = [
        ResourceDiff(
            resource=resource("Deployment", "api"),
            operation="update",
            basis="client",
            changes=[
                FieldChange(path="/spec/replicas", op="replace", before=2, after=3),
                FieldChange(
                    path="/spec/template/spec/containers/0/image",
                    op="replace",
                    before="registry.example/shop/api:v1.4.0",
                    after="registry.example/shop/api:v1.5.0",
                ),
                FieldChange(
                    path="/spec/template/spec/containers/0/envFrom",
                    op="add",
                    after=[{"configMapRef": {"name": "public-settings"}}],
                ),
            ],
            unified="--- preview before\n+++ preview after\n- replicas: 2\n+ replicas: 3\n- image: registry.example/shop/api:v1.4.0\n+ image: registry.example/shop/api:v1.5.0\n+ envFrom: [{configMapRef: {name: public-settings}}]\n",
            not_compared=["status", "server-generated metadata"],
        ),
        ResourceDiff(
            resource=resource("Service", "api"),
            operation="update",
            basis="client",
            changes=[
                FieldChange(
                    path="/spec/ports/0/port", op="replace", before=8080, after=80
                )
            ],
            unified="--- preview before\n+++ preview after\n- port: 8080\n+ port: 80\n",
        ),
        ResourceDiff(
            resource=resource("ConfigMap", "public-settings"),
            operation="create",
            basis="client",
            changes=[
                FieldChange(path="/data/LOG_LEVEL", op="add", after="info"),
                FieldChange(path="/data/PAGE_SIZE", op="add", after="25"),
            ],
            unified="+++ preview after\n+ LOG_LEVEL: info\n+ PAGE_SIZE: '25'\n",
        ),
    ]
    plan = PlanRecord(
        id="showcase-plan",
        application_id="shop",
        digest=digest({"revision": CURRENT_REVISION, "desired": current_desired}),
        target=target,
        source=SourceRevision(
            kind="git",
            revision=CURRENT_REVISION,
            entrypoint=PREVIEW_ENTRYPOINT,
        ),
        intent="deploy",
        release="preview-shop-v1.5.0",
        expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        summary={"update": 2, "create": 1},
        diffs=diffs,
        steps=[PlanStep.model_validate(step) for step in steps],
        desired_resources=[
            DesiredResource.model_validate(item) for item in current_desired
        ],
        desired_resources_complete=True,
        warnings=[
            "Disposable preview fixture: these changes and history are illustrative, "
            "not observed deployments or incidents. Source evaluation and deployment "
            "are disabled for this review."
        ],
    )
    store = Store(directory / "operations.sqlite3")
    store.put("plan", plan.model_dump(mode="json"), private={"fixture": True})
    previous = PlanRecord.model_validate(
        {
            **plan.model_dump(mode="json"),
            "id": "showcase-plan-previous",
            "digest": digest(
                {"revision": PREVIOUS_REVISION, "desired": previous_desired}
            ),
            "source": SourceRevision(
                kind="git",
                revision=PREVIOUS_REVISION,
                entrypoint=PREVIEW_ENTRYPOINT,
            ),
            "release": "preview-shop-v1.4.0",
            "summary": {"update": 1, "no-op": 1},
            "diffs": [
                ResourceDiff(
                    resource=resource("Deployment", "api"),
                    operation="update",
                    basis="client",
                    changes=[
                        FieldChange(
                            path="/spec/replicas", op="replace", before=1, after=2
                        )
                    ],
                    unified="--- preview before\n+++ preview after\n- replicas: 1\n+ replicas: 2\n",
                )
            ],
            "steps": [
                {
                    "ordinal": 0,
                    "level": 0,
                    "resource": resource("Service", "api"),
                    "operation": "no-op",
                    "dependencies": [],
                },
                {
                    "ordinal": 1,
                    "level": 0,
                    "resource": resource("Deployment", "api"),
                    "operation": "update",
                    "dependencies": [],
                },
            ],
            "desired_resources": previous_desired,
        }
    )
    store.put("plan", previous.model_dump(mode="json"), private={"fixture": True})
    unused_desired = deepcopy(current_desired)
    unused_desired[0]["manifest"]["spec"]["replicas"] = 4
    unused = PlanRecord.model_validate(
        {
            **plan.model_dump(mode="json"),
            "id": "showcase-plan-unused",
            "digest": digest({"preview": "unused-scale", "desired": unused_desired}),
            "release": "preview-shop-unapplied-scale",
            "expires_at": "2026-09-29T10:00:00Z",
            "summary": {"update": 1, "no-op": 2},
            "diffs": [
                ResourceDiff(
                    resource=resource("Deployment", "api"),
                    operation="update",
                    basis="client",
                    changes=[
                        FieldChange(
                            path="/spec/replicas", op="replace", before=3, after=4
                        )
                    ],
                    unified="--- preview before\n+++ preview after\n- replicas: 3\n+ replicas: 4\n",
                )
            ],
            "steps": [
                {
                    **step,
                    "operation": "update"
                    if step["resource"].kind == "Deployment"
                    else "no-op",
                }
                for step in steps
            ],
            "desired_resources": unused_desired,
            "warnings": [
                *plan.warnings,
                "Preview fixture: prepared for review, never executed; its original approval has expired.",
            ],
        }
    )
    store.put("plan", unused.model_dump(mode="json"), private={"fixture": True})
    # Fixture creation metadata matches its illustrative chronology. These
    # immutable plan bodies have no creation field of their own.
    with store.transaction() as connection:
        connection.executemany(
            "UPDATE records SET created=? WHERE kind='plan' AND id=?",
            [
                ("2026-10-01T08:29:00Z", plan.id),
                ("2026-09-30T08:59:00Z", previous.id),
                ("2026-09-29T09:00:00Z", unused.id),
            ],
        )
    base = {
        "application_id": "shop",
        "plan_id": plan.id,
        "approved_digest": plan.digest,
        "actor": "Preview fixture (disposable)",
        "trigger": "ui",
        "engine_release": plan.release,
    }
    histories = [
        Operation(
            **base,
            id="showcase-succeeded",
            state="succeeded",
            created_at="2026-10-01T09:15:00Z",
            updated_at="2026-10-01T09:17:00Z",
            stages=[
                Stage(name=name, state="succeeded") for name in ("apply", "readiness")
            ]
            + [
                Stage(
                    name="checks",
                    state="skipped",
                    reason="Fixture: no checks configured",
                )
            ],
            deployment_outcome="succeeded",
            checks_outcome="not_configured",
        ),
        Operation(
            **base,
            id="showcase-failed",
            state="failed",
            created_at="2026-10-01T08:30:00Z",
            updated_at="2026-10-01T08:32:00Z",
            stages=[
                Stage(name="apply", state="succeeded"),
                Stage(
                    name="readiness",
                    state="failed",
                    reason="Fixture: readiness deadline reached",
                ),
                Stage(name="checks", state="skipped"),
            ],
            deployment_outcome="failed",
            checks_outcome="not_configured",
        ),
        Operation(
            **base,
            id="showcase-cancelled",
            state="cancelled",
            created_at="2026-10-01T08:00:00Z",
            updated_at="2026-10-01T08:00:15Z",
            stages=[
                Stage(
                    name=name,
                    state="skipped",
                    reason="Fixture: cancelled before execution",
                )
                for name in ("apply", "readiness", "checks")
            ],
        ),
    ]
    histories[0] = Operation.model_validate(
        {
            **histories[0].model_dump(mode="json"),
            "journal": _journal(plan, "preview-execution-ready"),
            "engine_execution_id": "preview-execution-ready",
        }
    )
    histories[1] = Operation.model_validate(
        {
            **histories[1].model_dump(mode="json"),
            "journal": _journal(plan, "preview-execution-failed", failed=True),
            "engine_execution_id": "preview-execution-failed",
        }
    )
    histories.append(
        Operation(
            id="showcase-previous",
            application_id="shop",
            plan_id=previous.id,
            approved_digest=previous.digest,
            actor="Preview fixture (disposable)",
            trigger="ui",
            state="succeeded",
            created_at="2026-09-30T09:00:00Z",
            updated_at="2026-09-30T09:02:00Z",
            engine_release=previous.release,
            stages=[
                Stage(name=name, state="succeeded") for name in ("apply", "readiness")
            ],
            deployment_outcome="succeeded",
            checks_outcome="not_configured",
        )
    )
    for operation in histories:
        store.put(
            "operation", operation.model_dump(mode="json"), private={"fixture": True}
        )
    # The pin satisfies the existing renderer constructor; available() above
    # never invokes it. No source selection exists to reach render().
    renderer = _UnavailableRenderer(
        directory / "evaluations",
        RendererConfig(
            image_id="sha256:" + "0" * 64,
            platform="linux/arm64",
            docker=ToolPin.capture(Path(sys.executable)),
            socket=directory / "unused.sock",
        ),
    )
    return OperationService(
        query,
        store,
        renderer,
        {},
        principal=Principal(id="local", name="Local preview"),
    )
