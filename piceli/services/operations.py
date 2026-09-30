"""Durable release admission and recovery over the existing execution journal."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from piceli.checks import PythonCheck
from piceli.k8s.release_spec import ReleaseSpec
from piceli.services.contracts import (
    CancelRequest,
    Capability,
    Evaluation,
    EvaluationPreview,
    EvaluationRequest,
    Operation,
    OperationPage,
    OperationRequest,
    PlanRecord,
    PlanRequest,
    Principal,
    RecoveryRequest,
    ReleasePage,
    Stage,
)
from piceli.services.engine import EngineAdapter, freeze_spec, thaw_spec, unexpired
from piceli.services.query import QueryError, QueryService
from piceli.services.registration import Registration
from piceli.services.store import Store, digest, now

if TYPE_CHECKING:
    from piceli.services.cluster_evaluation import KubernetesJobEvaluator
    from piceli.services.evaluation import DockerEvaluator, SourceSelection


# Evaluation refusals reported by their own code instead of the generic one.
_EVALUATION_REFUSALS = frozenset({"ui-prerollout-unsupported"})


class _CancelledBeforeDispatch(Exception):
    pass


def _failure_code(error: Exception) -> str:
    if isinstance(error, QueryError):
        return error.code
    if getattr(error, "code", None) in {
        "plan-not-found",
        "plan-expired",
        "stored-release-mismatch",
        "stored-evidence-mismatch",
        "execution-refused",
        "resume-refused",
        "plan-intent-mismatch",
        "plan-release-mismatch",
    }:
        return "ui-plan-stale"
    return "ui-execution-failed"


class OperationService:
    """One dispatcher; requests authorize admission, workers use stored grants."""

    def __init__(
        self,
        query: QueryService,
        store: Store,
        evaluator: DockerEvaluator | KubernetesJobEvaluator,
        sources: Mapping[str, SourceSelection],
        *,
        principal: Principal | None,
        engine: EngineAdapter | None = None,
    ) -> None:
        if query.scope_policy is None:
            if (
                principal is None
                or principal.kind != "local"
                or principal.id != "local"
            ):
                raise ValueError("local service requires its explicit local principal")
        elif principal is not None:
            raise ValueError("scoped operation dispatcher uses admitted request actors")
        self.query, self.store, self.evaluator = query, store, evaluator
        self.sources = dict(sources)
        self.principal = principal
        self.engine = engine or EngineAdapter()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._lock = threading.RLock()
        self._renderer_ready = False
        query.delivery_capabilities = self.capabilities

    def capabilities(self, registration: Registration) -> dict[str, Capability]:
        allowed = (
            registration.definition_kind == "release"
            and registration.ownership == "native"
            and registration.release_spec is not None
            and registration.id in self.sources
        )
        reason = "source-evaluation-not-configured"
        if allowed and not self._renderer_ready:
            allowed = False
            reason = "isolated-renderer-unavailable"
        if registration.release_spec is not None and any(
            isinstance(check, PythonCheck)
            for check in registration.release_spec.model.checks
        ):
            allowed = False
            reason = "isolated-python-check-unavailable"
        return {
            key: Capability(allowed=allowed, reason=None if allowed else reason)
            for key in ("evaluate", "plan", "deploy", "rollback")
        }

    def _registration(self, id: str, *, action: str = "evaluate") -> Registration:
        registration = self.query.registration(id, action=action)
        if not self.capabilities(registration)["evaluate"].allowed:
            raise QueryError("ui-operation-unavailable", 409)
        return registration

    def _worker_registration(self, id: str) -> Registration:
        """Trusted internal lookup after durable request-scoped admission."""
        try:
            return self.query.registrations[id]
        except KeyError:
            raise QueryError("ui-state-invalid", 409) from None

    def _worker_plan(
        self, id: str, *, application_id: str, approved_digest: str
    ) -> PlanRecord:
        raw, _ = self.store.get("plan", id)
        plan = PlanRecord.model_validate(raw)
        if plan.application_id != application_id or plan.digest != approved_digest:
            raise QueryError("ui-state-invalid", 409)
        return plan

    def _actor(self) -> str:
        return self.query._principal().id

    @staticmethod
    def _scope(registration: Registration, spec: ReleaseSpec) -> str:
        assert registration.release_spec is not None
        target = spec.kubeconfig_target()
        if not target.cluster_uid or not target.namespace_uid:
            raise QueryError("ui-state-invalid", 409)
        return digest(
            {
                "cluster_uid": target.cluster_uid,
                "namespace_uid": target.namespace_uid,
                "owner": spec.model.release.owner,
                "release": spec.model.release.name,
            }
        )

    def preview(self, application_id: str, request: PlanRequest) -> EvaluationPreview:
        if not self._renderer_ready:
            self._renderer_ready = self.evaluator.available()
        registration = self._registration(application_id)
        if request.intent == "rollback":
            self.query.registration(application_id, action="rollback")
        if (request.intent == "rollback") != (request.release is not None):
            raise QueryError("ui-invalid-request", 422)
        try:
            spec, inputs = self.engine.inputs(registration)
            self.query.registrations[application_id] = replace(
                registration, target=spec.kubeconfig_target()
            )
            preview = self.evaluator.preview(
                application_id,
                self.sources[application_id],
                inputs,
                intent=request.intent,
                release=request.release,
            )
            self.store.put(
                "preview",
                preview.model_dump(mode="json"),
                private={"spec": freeze_spec(spec)},
            )
            return preview
        except QueryError:
            raise
        except Exception:
            raise QueryError("ui-evaluation-failed", 409) from None

    def evaluate(self, application_id: str, request: EvaluationRequest) -> Evaluation:
        registration = self._registration(application_id)
        fingerprint = digest(
            {
                "action": "evaluate",
                "application": application_id,
                **request.model_dump(),
            }
        )
        raw, private = self.store.get("preview", request.preview_id)
        preview = EvaluationPreview.model_validate(raw)
        if preview.application_id != application_id:
            raise QueryError("ui-not-found")
        if preview.intent == "rollback":
            self.query.registration(application_id, action="rollback")
        duplicate = self.store.idempotent(
            request.idempotency_key,
            fingerprint,
            "evaluation",
            principal=self._actor(),
        )
        if duplicate is not None:
            return Evaluation.model_validate(duplicate)
        if preview.digest != request.approved_digest:
            raise QueryError("ui-approval-mismatch", 409)
        if not unexpired(preview.expires_at):
            raise QueryError("ui-plan-stale", 409)
        evaluation = Evaluation(
            id=uuid.uuid4().hex,
            application_id=application_id,
            state="queued",
            preview_id=preview.id,
            created_at=now(),
            updated_at=now(),
        )
        result = self.store.admit(
            "evaluation",
            evaluation.model_dump(mode="json"),
            private={"spec": private["spec"], "preview": raw},
            scope=self._scope(registration, thaw_spec(private["spec"])),
            key=request.idempotency_key,
            fingerprint=fingerprint,
            principal=self._actor(),
        )
        self._wake.set()
        return Evaluation.model_validate(result)

    def evaluation(self, id: str) -> Evaluation:
        raw, _ = self.store.get("evaluation", id)
        self.query.registration(raw["application_id"], action="evaluate")
        return Evaluation.model_validate(raw)

    def plan(self, id: str) -> PlanRecord:
        raw, _ = self.store.get("plan", id)
        self.query.registration(raw["application_id"], action="plan")
        return PlanRecord.model_validate(raw)

    def admit(self, application_id: str, request: OperationRequest) -> Operation:
        registration = self._registration(application_id, action="plan")
        raw, private = self.store.get("plan", request.plan_id)
        plan = PlanRecord.model_validate(raw)
        if plan.application_id != application_id:
            raise QueryError("ui-not-found")
        self.query.registration(application_id, action=plan.intent)
        fingerprint = digest(
            {
                "action": plan.intent,
                "application": application_id,
                **request.model_dump(),
            }
        )
        duplicate = self.store.idempotent(
            request.idempotency_key,
            fingerprint,
            "operation",
            principal=self._actor(),
        )
        if duplicate is not None:
            return self._public(Operation.model_validate(duplicate))
        if plan.digest != request.approved_digest:
            raise QueryError("ui-approval-mismatch", 409)
        self.engine.validate(plan, private)
        operation = Operation(
            id=uuid.uuid4().hex,
            application_id=application_id,
            plan_id=plan.id,
            approved_digest=plan.digest,
            actor=self._actor(),
            trigger="ui",
            state="queued",
            created_at=now(),
            updated_at=now(),
            stages=[
                Stage(name=name, state="pending")
                for name in ("apply", "readiness", "checks")
            ],
            engine_release=plan.release,
        )
        result = self.store.admit(
            "operation",
            operation.model_dump(mode="json"),
            private={"resume": False},
            scope=self._scope(registration, thaw_spec(private["frozen"]["spec"])),
            key=request.idempotency_key,
            fingerprint=fingerprint,
            principal=self._actor(),
        )
        self._wake.set()
        return self._public(Operation.model_validate(result))

    def _public(self, operation: Operation) -> Operation:
        resumable = False
        if (
            operation.state in {"failed", "interrupted"}
            and operation.engine_execution_id
            and operation.trigger != "cli"
        ):
            try:
                plan = self.plan(operation.plan_id)
                _, private = self.store.get("plan", plan.id)
                resumable = private["frozen"]["mode"] == "create" and unexpired(
                    plan.expires_at
                )
            except (QueryError, KeyError):
                pass
        resume_authorized = self.query._allowed(operation.application_id, "resume")
        cancel_authorized = self.query._allowed(operation.application_id, "cancel")
        cancellable = (
            operation.state in {"queued", "running", "cancelling"}
            and operation.trigger != "cli"
        )
        return operation.model_copy(
            update={
                "capabilities": {
                    "resume": Capability(
                        allowed=resumable and resume_authorized,
                        reason=None
                        if resumable and resume_authorized
                        else "not-authorized"
                        if not resume_authorized
                        else "review-new-plan-required",
                    ),
                    "cancel": Capability(
                        allowed=cancellable and cancel_authorized,
                        reason=None
                        if cancellable and cancel_authorized
                        else "not-authorized"
                        if not cancel_authorized
                        else "operation-ended",
                    ),
                }
            }
        )

    def operation(self, id: str) -> Operation:
        raw, _ = self.store.get("operation", id)
        self.query.registration(raw["application_id"], action="activity")
        return self._public(Operation.model_validate(raw))

    def resume(self, id: str, request: RecoveryRequest) -> Operation:
        # Resolve and authorize the original scope before even consulting an
        # idempotency key; a prior response is still private to that scope.
        original = self.operation(id)
        self.query.registration(original.application_id, action="resume")
        fingerprint = digest(
            {"action": "resume", "operation": id, **request.model_dump()}
        )
        duplicate = self.store.idempotent(
            request.idempotency_key,
            fingerprint,
            "operation",
            principal=self._actor(),
        )
        if duplicate is not None:
            return self._public(Operation.model_validate(duplicate))
        registration = self._registration(original.application_id, action="resume")
        if not original.capabilities["resume"].allowed:
            raise QueryError("ui-operation-unavailable", 409)
        plan = self.plan(original.plan_id)
        if request.approved_digest != plan.digest:
            raise QueryError("ui-approval-mismatch", 409)
        _, private = self.store.get("plan", plan.id)
        self.engine.validate(plan, private, pending=False)
        recovered = original.model_copy(
            update={
                "id": uuid.uuid4().hex,
                "state": "queued",
                "created_at": now(),
                "updated_at": now(),
                "attempt": original.attempt + 1,
                "recovery_of": original.id,
                "error_code": None,
                "capabilities": {},
                "stages": [
                    Stage(name=name, state="pending")
                    for name in ("apply", "readiness", "checks")
                ],
            }
        )
        result = self.store.admit(
            "operation",
            recovered.model_dump(mode="json"),
            private={"resume": True},
            scope=self._scope(registration, thaw_spec(private["frozen"]["spec"])),
            key=request.idempotency_key,
            fingerprint=fingerprint,
            principal=self._actor(),
        )
        self._wake.set()
        return self._public(Operation.model_validate(result))

    def cancel(self, id: str, request: CancelRequest) -> Operation:
        # Cancellation itself is repeatable and never resets terminal outcomes.
        with self._lock:
            operation = self.operation(id)
            self.query.registration(operation.application_id, action="cancel")
            if operation.trigger == "cli":
                raise QueryError("ui-operation-unavailable", 409)
            raw, private = self.store.cancel(
                id,
                key=request.idempotency_key,
                fingerprint=digest({"action": "cancel", "operation": id}),
                principal=self._actor(),
            )
            if raw["state"] == "cancelling":
                if operation.engine_execution_id:
                    _, plan_private = self.store.get("plan", operation.plan_id)
                    try:
                        self.engine.cancel(plan_private, operation.engine_execution_id)
                    except (ValueError, OSError):
                        pass  # Intent survives; worker retries after journal start.
                # A checks-failed rollback is a distinct execution linked to
                # its parent. A parent cancellation must reach that child too.
                for child in self.store.active("operation"):
                    if child.get("recovery_of") != id or child.get("state") not in {
                        "running",
                        "cancelling",
                    }:
                        continue
                    child_raw, _ = self.store.cancel(
                        child["id"],
                        key=request.idempotency_key + ":policy-child",
                        fingerprint=digest(
                            {"action": "cancel", "operation": child["id"]}
                        ),
                        principal=self._actor(),
                    )
                    if child_raw.get("engine_execution_id"):
                        _, child_plan_private = self.store.get(
                            "plan", child_raw["plan_id"]
                        )
                        try:
                            self.engine.cancel(
                                child_plan_private, child_raw["engine_execution_id"]
                            )
                        except (ValueError, OSError):
                            pass
            return self.operation(id)

    def operations(self, application_id: str) -> OperationPage:
        registration = self.query.registration(application_id, action="activity")
        cursor = self.store.cursor()
        self._import_history(registration)
        return OperationPage(
            items=[
                self._public(Operation.model_validate(raw))
                for raw in self.store.records("operation", application_id)
            ],
            cursor=cursor,
        )

    def releases(self, application_id: str) -> ReleasePage:
        registration = self.query.registration(application_id)
        cursor = self.store.cursor()
        try:
            items = self.engine.releases(registration)
        except Exception:
            raise QueryError("ui-observation-unavailable", 503) from None
        allowed = self.capabilities(registration)[
            "rollback"
        ].allowed and self.query._allowed(application_id, "rollback")
        return ReleasePage(
            items=[
                item.model_copy(
                    update={
                        "capabilities": {
                            "rollback": Capability(
                                allowed=allowed,
                                reason=None
                                if allowed
                                else "source-evaluation-not-configured",
                            )
                        }
                    }
                )
                for item in items
            ],
            cursor=cursor,
        )

    def _import_history(self, registration: Registration) -> None:
        if registration.release_spec is None:
            return
        try:
            report = self.engine.history(registration)
        except Exception:
            return
        known = {
            raw.get("engine_execution_id"): raw
            for raw in self.store.records("operation", registration.id)
        }
        latest = {entry["execution_id"]: entry for entry in report.get("history", [])}
        for entry in latest.values():
            execution_id = entry["execution_id"]
            existing = known.get(execution_id)
            if existing is not None and existing["trigger"] != "cli":
                continue
            state = entry["state"]
            imported = Operation(
                id="cli-"
                + digest({"app": registration.id, "execution": execution_id})[:32],
                application_id=registration.id,
                plan_id="",
                approved_digest=str(entry.get("plan_hash", "")),
                actor="unknown",
                trigger="cli",
                state="succeeded"
                if state == "ready"
                else "cancelled"
                if state == "cancelled"
                else "interrupted"
                if state == "running"
                else "failed",
                created_at=entry["at"],
                updated_at=entry["at"],
                engine_execution_id=execution_id,
                engine_release=entry["release"],
                deployment_outcome="succeeded"
                if state in {"ready", "checks-failed"}
                else "unknown",
                checks_outcome="failed"
                if state == "checks-failed"
                else "succeeded"
                if (entry.get("checks") or {}).get("passed")
                else "unknown",
            )
            try:
                value = imported.model_dump(mode="json")
                if existing is None:
                    self.store.put("operation", value, private={"imported": True})
                elif existing != value:
                    self.store.update("operation", value)
            except QueryError:
                pass
            known[execution_id] = imported.model_dump(mode="json")

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self.store.acquire_dispatcher()
            try:
                self._renderer_ready = self.evaluator.available()
                self.evaluator.recover_interrupted()
                self._recover()
            except BaseException:
                self.store.close()
                raise
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="piceli-operations", daemon=True
            )
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._cancel.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            # Shutdown is not user cancellation. Finish an in-flight execution
            # when possible; process death leaves its journal for reconciliation.
            thread.join(timeout=30)
            if thread.is_alive():
                return  # Never release the dispatcher lock while writes continue.
        self._thread = None
        self.store.close()

    def _recover(self) -> None:
        for raw in self.store.active("evaluation"):
            if raw["state"] == "running":
                raw.update(
                    state="interrupted",
                    error_code="ui-operation-interrupted",
                    updated_at=now(),
                )
                self.store.update("evaluation", raw)
        for raw in self.store.active("operation"):
            if raw["state"] not in {"running", "cancelling"}:
                continue
            operation = Operation.model_validate(raw)
            if operation.engine_execution_id:
                try:
                    plan = self._worker_plan(
                        operation.plan_id,
                        application_id=operation.application_id,
                        approved_digest=operation.approved_digest,
                    )
                    _, private = self.store.get("plan", plan.id)
                    evidence = self.engine.inspect(
                        plan, private, operation.engine_execution_id
                    )
                    if evidence.get("release_state") in {
                        "ready",
                        "checks-failed",
                        "failed",
                        "cancelled",
                        "blocked",
                        "rejected",
                    }:
                        self._finish(operation.id, evidence)
                        continue
                except Exception:
                    pass
            raw.update(
                state="interrupted",
                error_code="ui-operation-interrupted",
                updated_at=now(),
            )
            self.store.update("operation", raw)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                work = next(
                    (
                        raw
                        for raw in self.store.active("evaluation")
                        if raw["state"] == "queued"
                    ),
                    None,
                )
                if work is not None:
                    self._evaluate(work["id"])
                    continue
                work = next(
                    (
                        raw
                        for raw in self.store.active("operation")
                        if raw["state"] == "queued"
                    ),
                    None,
                )
                if work is not None:
                    self._execute(work["id"])
                    continue
            except Exception:
                # A control-store failure stops dispatch; restart reconciliation
                # handles persisted running records. Never guess a success.
                self._stop.set()
                return
            self._wake.wait(0.25)
            self._wake.clear()

    def _evaluate(self, id: str) -> None:
        raw, private = self.store.get("evaluation", id)
        raw.update(state="running", updated_at=now())
        self.store.update("evaluation", raw)
        self._cancel.clear()
        try:
            preview = EvaluationPreview.model_validate(private["preview"])
            rendered = self.evaluator.render(
                preview.id, preview.digest, cancel=self._cancel
            )
            plan, material = self.engine.plan(
                self._worker_registration(raw["application_id"]),
                thaw_spec(private["spec"]),
                rendered,
                preview,
                id,
            )
            self.store.put("plan", plan.model_dump(mode="json"), private=material)
            raw.update(state="succeeded", plan_id=plan.id, updated_at=now())
        except Exception as error:
            code = getattr(error, "code", None)
            raw.update(
                state="interrupted" if self._stop.is_set() else "failed",
                error_code=(
                    code if code in _EVALUATION_REFUSALS else "ui-evaluation-failed"
                ),
                updated_at=now(),
            )
        self.store.update("evaluation", raw)

    def _execute(self, id: str) -> None:
        with self._lock:
            raw, admission = self.store.get("operation", id)
            if raw["state"] != "queued":
                return
            raw.update(state="running", updated_at=now())
            self.store.update("operation", raw)
        operation = Operation.model_validate(raw)
        plan = self._worker_plan(
            operation.plan_id,
            application_id=operation.application_id,
            approved_digest=operation.approved_digest,
        )
        _, private = self.store.get("plan", plan.id)
        children: list[str] = []

        def started(entry: dict[str, Any]) -> None:
            with self._lock:
                current, intent = self.store.get("operation", id)
                if entry["plan_hash"] != plan.engine_digest:
                    if intent.get("cancel_requested"):
                        raise _CancelledBeforeDispatch()
                    child_plan, child_private = self.engine.policy_plan(
                        plan, private, entry
                    )
                    self.store.put(
                        "plan",
                        child_plan.model_dump(mode="json"),
                        private=child_private,
                    )
                    child = Operation(
                        id=uuid.uuid4().hex,
                        application_id=operation.application_id,
                        plan_id=child_plan.id,
                        approved_digest=child_plan.digest,
                        actor=operation.actor,
                        trigger="ui",
                        state="running",
                        created_at=now(),
                        updated_at=now(),
                        recovery_of=id,
                        engine_execution_id=entry["execution_id"],
                        engine_release=entry["release"],
                        stages=[
                            Stage(name="apply", state="running"),
                            Stage(name="readiness", state="pending"),
                            Stage(name="checks", state="pending"),
                        ],
                    )
                    self.store.put(
                        "operation",
                        child.model_dump(mode="json"),
                        private={"policy": True},
                    )
                    children.append(child.id)
                    return
                current.update(
                    engine_execution_id=entry["execution_id"],
                    updated_at=now(),
                    stages=[
                        Stage(name="apply", state="running").model_dump(),
                        Stage(name="readiness", state="pending").model_dump(),
                        Stage(name="checks", state="pending").model_dump(),
                    ],
                )
                self.store.update("operation", current)
                if intent.get("cancel_requested"):
                    raise _CancelledBeforeDispatch()

        def progress(_line: str) -> None:
            current, intent = self.store.get("operation", id)
            if _line == "checking release post-deploy checks":
                current.update(
                    updated_at=now(),
                    stages=[
                        Stage(name="apply", state="succeeded").model_dump(),
                        Stage(name="readiness", state="succeeded").model_dump(),
                        Stage(name="checks", state="running").model_dump(),
                    ],
                )
                self.store.update("operation", current)
            if intent.get("cancel_requested") and current.get("engine_execution_id"):
                self.engine.cancel(private, current["engine_execution_id"])

        try:
            outcome = self.engine.execute(
                plan,
                private,
                on_start=started,
                on_progress=progress,
                resume=admission.get("resume", False),
                execution_id=operation.engine_execution_id,
            )
            self._finish(id, outcome)
            if children:
                rollback = outcome.get("rollback") or {}
                self._finish(children[-1], rollback)
        except Exception as error:
            current, intent = self.store.get("operation", id)
            if isinstance(error, _CancelledBeforeDispatch) and current.get(
                "engine_execution_id"
            ):
                # The parent already deployed and failed its checks. Refusing
                # the standing-policy child must leave that original failure
                # intact, based on the release history and journal.
                try:
                    evidence = self.engine.inspect(
                        plan, private, current["engine_execution_id"]
                    )
                    if evidence.get("release_state") == "checks-failed":
                        self._finish(id, evidence)
                        continue_children = False
                    else:
                        continue_children = True
                except Exception:
                    continue_children = True
                if not continue_children:
                    return
            code = _failure_code(error)
            current.update(
                state="cancelled"
                if isinstance(error, _CancelledBeforeDispatch)
                else "interrupted"
                if self._stop.is_set()
                else "failed",
                error_code=code,
                updated_at=now(),
            )
            self.store.update("operation", current)
            for child_id in children:
                child, _ = self.store.get("operation", child_id)
                child.update(
                    state="interrupted",
                    error_code="ui-operation-interrupted",
                    updated_at=now(),
                )
                self.store.update("operation", child)

    def _finish(self, id: str, outcome: Mapping[str, Any]) -> None:
        current, _ = self.store.get("operation", id)
        execution = outcome.get("execution") or {}
        release_state = outcome.get("release_state", execution.get("state"))
        deployed = execution.get("state") == "ready"
        plan = self._worker_plan(
            current["plan_id"],
            application_id=current["application_id"],
            approved_digest=current["approved_digest"],
        )
        checks_declared = bool(plan.checks.get("names"))
        checks = outcome.get("checks") or {}
        checked = checks.get("passed") is True
        # History ready is written only after required checks pass; journal ready
        # alone is insufficient. During a crash this distinction is decisive.
        success = (
            release_state == "ready"
            and deployed
            and (
                not checks_declared
                or checked
                or outcome.get("release_state") == "ready"
            )
        )
        state = (
            "succeeded"
            if success
            else "cancelled"
            if release_state == "cancelled"
            else "failed"
        )
        category = str(execution.get("failure_category", ""))
        stale = "precondition" in category or category in {
            "authorization-expired",
            "execution-expired",
            "recreated-object",
        }
        current.update(
            state=state,
            updated_at=now(),
            error_code=None
            if success
            else "ui-plan-stale"
            if stale
            else "ui-execution-failed",
            deployment_outcome="succeeded" if deployed else "failed",
            checks_outcome="not_configured"
            if not checks_declared
            else "succeeded"
            if success
            else "failed"
            if release_state == "checks-failed"
            else "unknown",
            stages=[
                Stage(
                    name="apply", state="succeeded" if deployed else "failed"
                ).model_dump(),
                Stage(
                    name="readiness", state="succeeded" if deployed else "failed"
                ).model_dump(),
                Stage(
                    name="checks",
                    state="skipped"
                    if not checks_declared
                    else "succeeded"
                    if success
                    else "failed"
                    if release_state == "checks-failed"
                    else "pending",
                ).model_dump(),
            ],
            receipts=[
                {
                    "execution_id": execution.get("execution_id"),
                    "state": execution.get("state"),
                    "actions": execution.get("actions", {}),
                }
            ],
        )
        self.store.update("operation", current)
