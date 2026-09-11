"""Fsync-backed operation journal with process exclusion and private references."""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from piceli.k8s.ops.bounds import positive, strict_json
from piceli.k8s.ops.secret_versions import private_database


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
        """
        )
        if self.connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("journal integrity check failed")

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
        with self.connection:
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

    def record(
        self, execution: str, ordinal: int, state: str, payload: dict[str, Any]
    ) -> None:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        self._capacity(encoded)
        with self.connection:
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

    def set_state(self, execution: str, state: str) -> None:
        self._capacity()
        with self.connection:
            self.connection.execute(
                "UPDATE executions SET state=? WHERE id=?", (state, execution)
            )

    def cancel(self, execution: str) -> None:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE executions SET cancelled=1,state='cancelled' WHERE id=?",
                (execution,),
            )
            if cursor.rowcount != 1:
                raise ValueError("unknown execution")

    def resume(self, execution: str) -> None:
        with self.connection:
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
        return {
            "execution_id": execution,
            "state": row["state"],
            "cancelled": bool(row["cancelled"]),
            "actions": [
                {"ordinal": item["ordinal"], "state": item["state"]}
                for item in self.actions(execution)
            ],
        }

    def close(self) -> None:
        self.connection.close()
