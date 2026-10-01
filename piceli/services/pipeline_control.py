"""A durable local browser admission layer over ``piceli deploy``'s runner.

Only the trusted operator supplies a Pipeline. The browser approves a
preliminary build/delivery plan when images are pending, then explicitly
approves the materialized release plan before any rollout change.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from piceli.pipeline import PipelineError
from piceli.pipeline.planfile import (
    load_plan_document,
    observed_target,
    plan_document,
    write_plan_document,
)
from piceli.pipeline.runner import PipelineRunner
from piceli.services.query import QueryError, QueryService
from piceli.services.store import Store, digest, now

if TYPE_CHECKING:
    from piceli.pipeline import Pipeline


def _future(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


def _unexpired(value: str) -> bool:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) > datetime.now(UTC)
    except ValueError:
        return False


def _public_stages(stages: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    shown: list[dict[str, Any]] = []
    # These are the same public descriptions emitted by ``piceli deploy
    # --plan --json``. Private receipts and file paths are never projected.
    for name, stage in stages.items():
        row: dict[str, Any] = {"name": name}
        row.update(
            {
                key: stage[key]
                for key in (
                    "state",
                    "action",
                    "why",
                    "summary",
                    "changes",
                    "checks",
                    "claims",
                    "writers",
                    "release",
                    "rollback_on_failed_checks",
                )
                if key in stage
            }
        )
        shown.append(row)
    return shown


class PipelineControl:
    """Single local dispatcher for reviewed, already-materialized pipelines."""

    def __init__(
        self,
        query: QueryService,
        application_id: str,
        pipeline: Pipeline,
        entry: str,
        control_dir: Path,
    ) -> None:
        if query.scope_policy is not None:
            raise ValueError("cluster pipelines need an isolated source adapter")
        if application_id not in query.registrations:
            raise ValueError("pipeline control must name a configured scope")
        if not entry or not control_dir.is_absolute():
            raise ValueError(
                "pipeline control requires trusted entry and private state"
            )
        self.query = query
        self.application_id = application_id
        self.pipeline = pipeline
        self.entry = entry
        self.directory = control_dir
        self.store = Store(control_dir / "pipeline-control.sqlite3")
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.store.acquire_dispatcher()
        # A previous process may have died mid-run. Never silently resume an
        # approval after restart: the owner must inspect its runner journal.
        for record in self.store.active("pipeline_operation"):
            self.store.update(
                "pipeline_operation",
                {
                    **record,
                    "state": "interrupted",
                    "error_code": "ui-operation-interrupted",
                    "updated_at": now(),
                },
            )
        self._thread = threading.Thread(target=self._dispatch, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join()
        self.store.close()

    def _plan_file(self, identity: str) -> Path:
        if len(identity) != 32 or any(
            char not in "0123456789abcdef" for char in identity
        ):
            raise QueryError("ui-not-found")
        return self.directory / "plans" / f"{identity}.json"

    def _save_plan(
        self, runner: PipelineRunner, combined: Any, phase: str
    ) -> dict[str, Any]:
        identity = uuid.uuid4().hex
        document = plan_document(
            runner,
            combined,
            entry=self.entry,
            reapply=False,
            observed=observed_target(self.pipeline),
        )
        record = {
            "id": identity,
            "application_id": self.application_id,
            "state": "planned",
            "digest": combined.combined_hash,
            "target": self.query.registration(self.application_id)
            .public_target()
            .model_dump(),
            "stages": _public_stages(combined.stages),
            "materialized": phase == "final",
            "phase": phase,
            "expires_at": _future(10),
            "created_at": now(),
        }
        path = self._plan_file(identity)
        try:
            write_plan_document(path, document)
            self.store.put("pipeline_plan", record)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return record

    def plan(self) -> dict[str, Any]:
        self.query.registration(self.application_id, action="plan")
        runner = PipelineRunner(self.pipeline, say=lambda _line: None)
        try:
            with runner.locked(), runner.sources():
                combined = runner.plan("checks")
                if combined.stages.get("plan", {}).get("state") == "pending":
                    combined = runner.plan("deliver")
                    phase = "preliminary"
                else:
                    phase = "final"
                return self._save_plan(runner, combined, phase)
        except (PipelineError, OSError, ValueError):
            raise QueryError("ui-evaluation-failed", 409) from None

    def get_plan(self, identity: str) -> dict[str, Any]:
        record, _ = self.store.get("pipeline_plan", identity)
        self.query.registration(record["application_id"], action="plan")
        return record

    def admit(
        self, identity: str, approved_digest: str, idempotency_key: str
    ) -> dict[str, Any]:
        self.query.registration(self.application_id, action="deploy")
        fingerprint = digest(
            {"plan": identity, "approved": approved_digest, "action": "pipeline-deploy"}
        )
        existing = self.store.idempotent(
            idempotency_key, fingerprint, "pipeline_operation"
        )
        if existing is not None:
            return existing
        plan = self.get_plan(identity)
        if plan["application_id"] != self.application_id:
            raise QueryError("ui-not-found")
        if plan["digest"] != approved_digest:
            raise QueryError("ui-approval-mismatch", 409)
        if not _unexpired(plan["expires_at"]):
            raise QueryError("ui-plan-stale", 409)
        operation = {
            "id": uuid.uuid4().hex,
            "application_id": self.application_id,
            "state": "queued",
            "plan_id": identity,
            "approved_digest": approved_digest,
            "phase": plan["phase"],
            "stages": {item["name"]: "pending" for item in plan["stages"]},
            "created_at": now(),
            "updated_at": now(),
        }
        admitted = self.store.admit(
            "pipeline_operation",
            operation,
            private={},
            scope=digest(
                {
                    "pipeline": self.pipeline.name,
                    "target": self.pipeline.target.identity(),
                }
            ),
            key=idempotency_key,
            fingerprint=fingerprint,
        )
        self._wake.set()
        return admitted

    def approve_second(
        self, operation_id: str, plan_id: str, approved_digest: str
    ) -> dict[str, Any]:
        self.query.registration(self.application_id, action="deploy")
        operation, _ = self.store.get("pipeline_operation", operation_id)
        if operation["application_id"] != self.application_id:
            raise QueryError("ui-not-found")
        if operation.get("next_plan_id") != plan_id:
            raise QueryError("ui-approval-mismatch", 409)
        plan = self.get_plan(plan_id)
        if plan["phase"] != "final" or plan["digest"] != approved_digest:
            raise QueryError("ui-approval-mismatch", 409)
        if not _unexpired(plan["expires_at"]):
            raise QueryError("ui-plan-stale", 409)
        if operation["state"] != "awaiting-review":
            if operation.get("approved_digest") == approved_digest:
                return operation
            raise QueryError("ui-operation-conflict", 409)
        updated = {
            **operation,
            "state": "queued",
            "phase": "final",
            "plan_id": plan_id,
            "approved_digest": approved_digest,
            "updated_at": now(),
        }
        self.store.update("pipeline_operation", updated)
        self._wake.set()
        return updated

    def operation(self, identity: str) -> dict[str, Any]:
        record, _ = self.store.get("pipeline_operation", identity)
        self.query.registration(record["application_id"], action="activity")
        return record

    def operations(self) -> dict[str, Any]:
        self.query.registration(self.application_id, action="activity")
        return {
            "items": self.store.records("pipeline_operation", self.application_id)[:50]
        }

    def _dispatch(self) -> None:
        while not self._stop.is_set():
            for operation in self.store.active("pipeline_operation"):
                if operation["state"] == "queued":
                    self._run(operation)
            self._wake.wait(0.5)
            self._wake.clear()

    def _run(self, operation: dict[str, Any]) -> None:
        operation = {**operation, "state": "running", "updated_at": now()}
        self.store.update("pipeline_operation", operation)
        runner = PipelineRunner(self.pipeline, say=lambda _line: None)
        try:
            document = load_plan_document(self._plan_file(operation["plan_id"]))
            with runner.locked(), runner.sources():
                runner.adopt_plan_file(document)
                combined = runner.plan(str(document["until"]))
                if (
                    operation["phase"] == "final"
                    and combined.stages.get("plan", {}).get("state") == "pending"
                ):
                    raise QueryError("ui-plan-stale", 409)
                if combined.combined_hash != operation["approved_digest"]:
                    raise QueryError("ui-plan-stale", 409)
                result = runner.execute(combined, operation["approved_digest"])
                if operation["phase"] == "preliminary":
                    if result["state"] != "stopped":
                        raise QueryError("ui-execution-failed", 409)
                    second = runner.plan("checks")
                    if second.stages.get("plan", {}).get("state") == "pending":
                        raise QueryError("ui-plan-stale", 409)
                    next_plan = self._save_plan(runner, second, "final")
                    operation = {
                        **operation,
                        "state": "awaiting-review",
                        "next_plan_id": next_plan["id"],
                        "runner_id": result.get("run_id"),
                        "stages": result["stages"],
                        "updated_at": now(),
                    }
                else:
                    operation = {
                        **operation,
                        "state": "succeeded"
                        if result["state"] == "ready"
                        else result["state"],
                        "runner_id": result.get("run_id"),
                        "stages": result["stages"],
                        "updated_at": now(),
                    }
        except (PipelineError, QueryError) as error:
            operation = {
                **operation,
                "state": "failed",
                "error_code": "ui-plan-stale"
                if getattr(error, "code", "")
                in {
                    "ui-plan-stale",
                    "pipeline-plan-changed",
                    "pipeline-preview-changed",
                    "deploy-plan-file-mismatch",
                    "deploy-plan-target-mismatch",
                }
                else "ui-execution-failed",
                "stages": {
                    name: runner.run.stage(name).get("state", "pending")
                    for name in runner.stages
                }
                if runner.run is not None
                else operation["stages"],
                "updated_at": now(),
            }
        except Exception:
            operation = {
                **operation,
                "state": "failed",
                "error_code": "ui-execution-failed",
                "updated_at": now(),
            }
        self.store.update("pipeline_operation", operation)
