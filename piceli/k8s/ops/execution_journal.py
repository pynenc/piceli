"""Fsync-backed operation journal with process exclusion and private references."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from piceli.k8s.ops.bounds import positive, strict_json
from piceli.k8s.ops.secret_versions import private_database

_MIGRATABLE_ACTION_STATES = frozenset({"pending", "failed", "applied", "ready"})
_MIGRATABLE_EXECUTION_STATES = frozenset({"pending", "failed", "ready"})


class ExecutionJournal:
    """Durable intent precedes IO. One process may advance a journal at a time.

    No manifests, secret values, private value digests or server errors belong in
    this journal. Payloads contain identity, random private refs and allowlisted
    operation states. Cancellation has a separate transaction and survives death.
    """

    def __init__(self, path: Path, *, max_bytes: int = 64_000_000) -> None:
        self.path = path
        self.max_bytes = positive(max_bytes, "journal bytes", 1_000_000_000)
        self.connection = private_database(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS executions (
                id TEXT PRIMARY KEY, binding TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS actions (
                execution TEXT NOT NULL REFERENCES executions(id), ordinal INTEGER NOT NULL,
                state TEXT NOT NULL, operation_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(execution, ordinal));
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                execution TEXT NOT NULL REFERENCES executions(id), ordinal INTEGER,
                state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS migration_lineage (
                execution TEXT PRIMARY KEY REFERENCES executions(id),
                source_execution TEXT NOT NULL,
                source_binding_sha256 TEXT NOT NULL,
                archive_sha256 TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS deployment_sessions (
                id TEXT PRIMARY KEY, archive TEXT NOT NULL);
        """
        )
        if self.connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("journal integrity check failed")

    #: Called after every committed transaction, before the caller's IO
    #: (shared state writes the journal through; see :mod:`piceli.state`).
    on_commit: Callable[[], None] | None = None

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self.connection:
            yield
        if self.on_commit is not None:
            self.on_commit()

    def _capacity(self, encoded: str = "") -> None:
        # Reserve several SQLite pages for a complete intent/receipt transaction.
        if self.path.stat().st_size + len(encoded.encode()) + 32768 > self.max_bytes:
            raise ValueError("journal byte budget exhausted")

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        descriptor = os.open(
            str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("journal is already executing") from None
            yield
        finally:
            os.close(descriptor)

    def start(
        self, execution: str, binding: dict[str, Any], operation_ids: list[str]
    ) -> None:
        encoded = json.dumps(
            binding, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        current = self.connection.execute(
            "SELECT binding FROM executions WHERE id=?", (execution,)
        ).fetchone()
        if current is not None:
            if current[0] != encoded:
                raise ValueError(
                    "execution binding changed; new authorization and execution required"
                )
            rows = self.actions(execution)
            if len(rows) != len(operation_ids) or any(
                row["ordinal"] != index
                or row["state"]
                not in {
                    "pending",
                    "failed",
                    "intent",
                    "applied",
                    "ready",
                    "compensating",
                    "compensated",
                }
                or not re.fullmatch(r"[0-9a-f]{32}", row["operation_id"])
                for index, row in enumerate(rows)
            ):
                raise ValueError("journal action inventory is incomplete or invalid")
            return
        self._capacity(encoded)
        with self._transaction():
            self.connection.execute(
                "INSERT INTO executions(id,binding) VALUES (?,?)", (execution, encoded)
            )
            self.connection.executemany(
                "INSERT INTO actions VALUES (?,?,'pending',?,'{}')",
                [
                    (execution, index, operation_id)
                    for index, operation_id in enumerate(operation_ids)
                ],
            )

    def actions(self, execution: str) -> list[dict[str, Any]]:
        return [
            dict(row) | {"payload": strict_json(row["payload"])}
            for row in self.connection.execute(
                "SELECT * FROM actions WHERE execution=? ORDER BY ordinal", (execution,)
            )
        ]

    def export_execution(self, execution: str) -> dict[str, Any]:
        """Read a legacy record without changing it or resolving private references."""
        row = self.connection.execute(
            "SELECT binding,state,cancelled FROM executions WHERE id=?", (execution,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown execution")
        return {
            "execution_id": execution,
            "binding": strict_json(row["binding"]),
            "state": row["state"],
            "cancelled": bool(row["cancelled"]),
            "actions": self.actions(execution),
        }

    @staticmethod
    def _legacy_payload(payload: Any) -> None:
        if not isinstance(payload, dict) or set(payload) - {
            "before",
            "after",
            "uid",
            "resource_version",
            "retained_reconciled",
            "same_owner_update",
            "written_at",
        }:
            raise ValueError("legacy receipt is not secret-safe")
        if "written_at" in payload and not isinstance(payload["written_at"], str):
            raise ValueError("legacy receipt has invalid write time")
        for key in ("before", "after"):
            if key not in payload:
                continue
            reference = payload[key]
            if (
                not isinstance(reference, dict)
                or set(reference) != {"store_id", "version"}
                or any(
                    not isinstance(value, str)
                    or not re.fullmatch(r"[0-9a-f]{32}", value)
                    for value in reference.values()
                )
            ):
                raise ValueError("legacy receipt has invalid private reference")
        for key in ("uid", "resource_version"):
            if key in payload and (
                not isinstance(payload[key], str) or not payload[key]
            ):
                raise ValueError("legacy receipt has invalid identity")
        if "retained_reconciled" in payload and not isinstance(
            payload["retained_reconciled"], bool
        ):
            raise ValueError("legacy receipt has invalid retention state")
        if "same_owner_update" in payload and not isinstance(
            payload["same_owner_update"], bool
        ):
            raise ValueError("legacy receipt has invalid update state")

    def import_legacy_execution(
        self,
        *,
        execution: str,
        binding: dict[str, Any],
        source_execution: str,
        source_binding: dict[str, Any],
        archive: dict[str, Any],
        state: str,
        cancelled: bool,
        actions: list[dict[str, Any]],
    ) -> None:
        """Copy verified legacy state into a new journal with immutable lineage.

        The caller must validate plan/archive semantics first. This method only
        admits unambiguous, secret-safe receipts and never touches the source
        journal.
        """
        if state not in _MIGRATABLE_EXECUTION_STATES or cancelled:
            raise ValueError("legacy execution state is ambiguous")
        if not execution or execution != source_execution:
            raise ValueError("legacy execution identity mismatch")
        if (
            self.connection.execute(
                "SELECT 1 FROM executions WHERE id=?", (execution,)
            ).fetchone()
            is not None
        ):
            raise ValueError("destination execution already exists")
        encoded_binding = json.dumps(
            binding, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        encoded_source = json.dumps(
            source_binding, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        encoded_archive = json.dumps(
            archive, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if not isinstance(actions, list) or not actions:
            raise ValueError("legacy action inventory is missing")
        prepared: list[tuple[int, str, str, str]] = []
        for ordinal, action in enumerate(actions):
            if (
                not isinstance(action, dict)
                or action.get("ordinal") != ordinal
                or action.get("state") not in _MIGRATABLE_ACTION_STATES
                or not isinstance(action.get("operation_id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", action["operation_id"])
                or not isinstance(action.get("payload"), dict)
            ):
                raise ValueError("legacy action state is ambiguous")
            self._legacy_payload(action["payload"])
            payload = json.dumps(
                action["payload"],
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            prepared.append((ordinal, action["state"], action["operation_id"], payload))
        if state == "ready" and any(item[1] != "ready" for item in prepared):
            raise ValueError("ready legacy execution has incomplete receipts")
        self._capacity(encoded_binding + encoded_archive)
        with self._transaction():
            self.connection.execute(
                "INSERT INTO executions(id,binding,state) VALUES (?,?,?)",
                (execution, encoded_binding, state),
            )
            self.connection.executemany(
                "INSERT INTO actions VALUES (?,?,?,?,?)",
                [(execution, *item) for item in prepared],
            )
            self.connection.execute(
                "INSERT INTO migration_lineage VALUES (?,?,?,?)",
                (
                    execution,
                    source_execution,
                    hashlib.sha256(encoded_source.encode()).hexdigest(),
                    hashlib.sha256(encoded_archive.encode()).hexdigest(),
                ),
            )
            self.connection.execute(
                "INSERT INTO events(execution,ordinal,state) VALUES (?,NULL,?)",
                (execution, "legacy-imported"),
            )

    def migration_lineage(self, execution: str) -> dict[str, str] | None:
        row = self.connection.execute(
            "SELECT source_execution,source_binding_sha256,archive_sha256 "
            "FROM migration_lineage WHERE execution=?",
            (execution,),
        ).fetchone()
        return None if row is None else dict(row)

    def store_session(self, session_id: str, archive: dict[str, Any]) -> None:
        """Persist one canonical session archive without changing an existing one."""
        if not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise ValueError("invalid deployment session identity")
        encoded = json.dumps(
            archive, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        current = self.connection.execute(
            "SELECT archive FROM deployment_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if current is not None:
            if current[0] != encoded:
                raise ValueError("deployment session archive changed")
            return
        self._capacity(encoded)
        with self._transaction():
            self.connection.execute(
                "INSERT INTO deployment_sessions(id,archive) VALUES (?,?)",
                (session_id, encoded),
            )

    def session(self, session_id: str) -> dict[str, Any]:
        """Load canonical session evidence; malformed durable state is rejected."""
        row = self.connection.execute(
            "SELECT archive FROM deployment_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise ValueError("deployment session missing")
        value = strict_json(row[0])
        if (
            not isinstance(value, dict)
            or json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
            != row[0]
        ):
            raise ValueError("deployment session archive is corrupt")
        return value

    def record(
        self, execution: str, ordinal: int, state: str, payload: dict[str, Any]
    ) -> None:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        self._capacity(encoded)
        with self._transaction():
            cursor = self.connection.execute(
                "UPDATE actions SET state=?,payload=? WHERE execution=? AND ordinal=?",
                (state, encoded, execution, ordinal),
            )
            if cursor.rowcount != 1:
                raise ValueError("unknown journal action")
            self.connection.execute(
                "INSERT INTO events(execution,ordinal,state) VALUES (?,?,?)",
                (execution, ordinal, state),
            )

    def record_failure(self, execution: str, category: str) -> None:
        """Persist one bounded provider failure category without exception details."""
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", category):
            raise ValueError("invalid provider failure category")
        with self._transaction():
            self.connection.execute(
                "INSERT INTO events(execution,ordinal,state) VALUES (?,NULL,?)",
                (execution, f"error:{category}"),
            )

    def set_state(self, execution: str, state: str) -> None:
        self._capacity()
        with self._transaction():
            self.connection.execute(
                "UPDATE executions SET state=? WHERE id=?", (state, execution)
            )

    def cancel(self, execution: str) -> None:
        with self._transaction():
            cursor = self.connection.execute(
                "UPDATE executions SET cancelled=1,state='cancelled' WHERE id=?",
                (execution,),
            )
            if cursor.rowcount != 1:
                raise ValueError("unknown execution")

    def resume(self, execution: str) -> None:
        with self._transaction():
            self.connection.execute(
                "UPDATE executions SET cancelled=0,state='pending' WHERE id=?",
                (execution,),
            )

    def cancelled(self, execution: str) -> bool:
        row = self.connection.execute(
            "SELECT cancelled FROM executions WHERE id=?", (execution,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown execution")
        return bool(row[0])

    def summary(self, execution: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT state,cancelled FROM executions WHERE id=?", (execution,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown execution")
        result = {
            "execution_id": execution,
            "state": row["state"],
            "cancelled": bool(row["cancelled"]),
            "actions": [
                {"ordinal": item["ordinal"], "state": item["state"]}
                for item in self.actions(execution)
            ],
        }
        failure = self.connection.execute(
            "SELECT state FROM events WHERE execution=? AND state LIKE 'error:%' "
            "ORDER BY sequence DESC LIMIT 1",
            (execution,),
        ).fetchone()
        if failure is not None and row["state"] in {"blocked", "failed"}:
            result["failure_category"] = failure["state"].removeprefix("error:")
        return result

    def close(self) -> None:
        self.connection.close()
