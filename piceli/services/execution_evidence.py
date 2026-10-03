"""Bounded, read-only projection of durable journal evidence for the UI.

Never return action payloads or execution bindings. Those include private
references. No progress text is reconstructed: events are the persisted rows;
pod logs exist only when a redacted failure diagnosis recorded them.
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from piceli.k8s.ops.bounds import strict_json
from piceli.k8s.ops.diagnosis import LOG_LINES, SCHEMA, redact
from piceli.services.contracts import (
    ExecutionJournalRecord,
    JournalAction,
    JournalEvent,
    JournalLog,
    PlanStep,
)
from piceli.services.plan_evidence import plan_steps

_ACTION_STATES = frozenset(
    {"pending", "failed", "intent", "applied", "ready", "compensating", "compensated"}
)
_EXECUTION_STATES = frozenset(
    {"pending", "running", "ready", "failed", "blocked", "cancelled"}
)
_MAX_BINDING_BYTES = 8_000_000
_MAX_DIAGNOSIS_BYTES = 256_000


def _written_at(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _logs(diagnosis: Any, steps: list[PlanStep]) -> tuple[list[JournalLog], bool]:
    if not isinstance(diagnosis, dict) or diagnosis.get("schema") != SCHEMA:
        return [], False
    result: list[JournalLog] = []
    truncated = False
    workloads = diagnosis.get("workloads", [])
    if not isinstance(workloads, list):
        return [], True
    for workload in workloads:
        if not isinstance(workload, dict):
            truncated = True
            continue
        matches = [
            step.resource
            for step in steps
            if step.resource.kind == workload.get("kind")
            and step.resource.name == workload.get("name")
        ]
        # Diagnosis has no namespace field. Never guess when names are ambiguous.
        if len(matches) != 1:
            truncated = True
            continue
        causes = workload.get("causes", [])
        if not isinstance(causes, list):
            truncated = True
            continue
        for cause in causes:
            if not isinstance(cause, dict) or not isinstance(cause.get("logs"), list):
                continue
            lines = cause["logs"]
            if not lines:
                continue
            if len(result) >= 20:
                return result, True
            truncated = truncated or len(lines) > LOG_LINES
            result.append(
                JournalLog(
                    resource=matches[0],
                    pod=redact(str(cause.get("pod", ""))),
                    container=redact(str(cause.get("container", ""))),
                    lines=[
                        redact(line)
                        for line in lines[-LOG_LINES:]
                        if isinstance(line, str)
                    ],
                )
            )
    return result, truncated


def read_journal(
    path: Path,
    execution_id: str,
    engine_digest: str,
    target_id: str,
    *,
    max_actions: int = 1000,
    max_events: int = 2000,
) -> ExecutionJournalRecord | None:
    """Read one exact plan-bound execution, leaving the database unchanged.

    Unknown/corrupt/missing evidence is unavailable rather than guessed from
    resource names, aggregate counters, or the currently registered source.
    """
    if not engine_digest or path.is_symlink() or not path.is_file():
        return None
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5
        )
        connection.row_factory = sqlite3.Row
        deadline = time.monotonic() + 1
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT state,CASE WHEN length(binding)<=? THEN binding END AS binding FROM executions WHERE id=?",
            (_MAX_BINDING_BYTES, execution_id),
        ).fetchone()
        if (
            row is None
            or row["state"] not in _EXECUTION_STATES
            or row["binding"] is None
        ):
            return None
        binding = strict_json(row["binding"], _MAX_BINDING_BYTES)
        plan = binding["revision"]["desired_state"]
        if plan.get("plan_hash") != engine_digest:
            return None
        steps = plan_steps(plan, target_id)
        by_ordinal = {step.ordinal: step for step in steps}
        rows = connection.execute(
            "SELECT ordinal,state,substr(json_extract(payload,'$.written_at'),1,65) AS written_at FROM actions WHERE execution=? ORDER BY ordinal LIMIT ?",
            (execution_id, max_actions + 1),
        ).fetchall()
        truncated = len(rows) > max_actions or len(rows) != len(steps)
        actions: list[JournalAction] = []
        for item in rows[:max_actions]:
            step = by_ordinal.get(item["ordinal"])
            if step is None or item["state"] not in _ACTION_STATES:
                truncated = True
                continue
            actions.append(
                JournalAction(
                    ordinal=step.ordinal,
                    resource=step.resource,
                    operation=step.operation,
                    state=item["state"],
                    written_at=_written_at(item["written_at"]),
                )
            )
        rows = connection.execute(
            "SELECT sequence,ordinal,state FROM events WHERE execution=? ORDER BY sequence DESC LIMIT ?",
            (execution_id, max_events + 1),
        ).fetchall()
        truncated = truncated or len(rows) > max_events
        events: list[JournalEvent] = []
        for item in reversed(rows[:max_events]):
            state = item["state"]
            if (
                state not in _ACTION_STATES
                and state != "legacy-imported"
                and not re.fullmatch(r"error:[a-z][a-z0-9-]{0,79}", state)
            ) or (item["ordinal"] is not None and item["ordinal"] not in by_ordinal):
                truncated = True
                continue
            events.append(
                JournalEvent(
                    sequence=item["sequence"], ordinal=item["ordinal"], state=state
                )
            )
        diagnosis = connection.execute(
            "SELECT CASE WHEN length(payload)<=? THEN payload END AS payload FROM diagnoses WHERE execution=?",
            (_MAX_DIAGNOSIS_BYTES, execution_id),
        ).fetchone()
        logs: list[JournalLog] = []
        if diagnosis and row["state"] in {"failed", "blocked"}:
            if diagnosis["payload"] is None:
                truncated = True
            else:
                logs, logs_truncated = _logs(
                    strict_json(diagnosis["payload"], _MAX_DIAGNOSIS_BYTES), steps
                )
                truncated = truncated or logs_truncated
        return ExecutionJournalRecord(
            execution_id=execution_id,
            state=row["state"],
            actions=actions,
            events=events,
            logs=logs,
            truncated=truncated,
        )
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError):
        return None
    finally:
        if connection is not None:
            connection.close()
