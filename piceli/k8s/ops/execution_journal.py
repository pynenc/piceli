"""Fsync-backed operation journal with process exclusion and private references."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Collection, Iterator
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
    The one exception is a failed execution's *diagnosis*
    (:mod:`piceli.k8s.ops.diagnosis`): bounded pod causes whose log lines and
    messages were redacted before they were recorded.
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
            CREATE TABLE IF NOT EXISTS diagnoses (
                execution TEXT PRIMARY KEY REFERENCES executions(id),
                payload TEXT NOT NULL);
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

    def used_bytes(self) -> int:
        """Bytes in use: the file's pages minus the free ones (0.47.0), so
        what :meth:`compact` deletes counts at once, before a ``VACUUM``."""
        pages, free, size = (
            self.connection.execute(f"PRAGMA {name}").fetchone()[0]
            for name in ("page_count", "freelist_count", "page_size")
        )
        return int((pages - free) * size)

    def _capacity(self, encoded: str = "") -> None:
        # Reserve several SQLite pages for a complete intent/receipt transaction.
        if self.used_bytes() + len(encoded.encode()) + 32768 > self.max_bytes:
            raise ValueError("journal byte budget exhausted")

    def compact(self, keep: set[str], *, retain: int = 20) -> dict[str, int]:
        """Forget finished executions nothing needs any more (0.47.0).

        An execution goes, with its actions, events and diagnosis, when it is
        finished (not ``pending``), not in ``keep`` (the caller's live ones:
        the latest of each release), not among the newest ``retain``, and no
        migration lineage names it. Deployment sessions stay: a catalogued
        release reopens from its session. The file is rewritten
        (``VACUUM``) when more than a quarter of it is free afterwards."""
        used_before = self.used_bytes()
        doomed = self.doomed(keep, retain=retain)
        if doomed:
            with self._transaction():
                for table, column in (
                    ("events", "execution"),
                    ("actions", "execution"),
                    ("diagnoses", "execution"),
                    ("executions", "id"),
                ):
                    self.connection.executemany(
                        f"DELETE FROM {table} WHERE {column}=?",
                        [(execution,) for execution in doomed],
                    )
        size = self.path.stat().st_size
        vacuumed = 0
        if size and size - self.used_bytes() > size // 4:
            self.connection.execute("VACUUM")
            vacuumed = 1
            if self.on_commit is not None:
                self.on_commit()
        return {
            "removed": len(doomed),
            "vacuumed": vacuumed,
            "bytes_before": size,
            "bytes_after": self.path.stat().st_size,
            "used_before": used_before,
            "used_after": self.used_bytes(),
        }

    def doomed(self, keep: set[str], *, retain: int = 20) -> list[str]:
        """The executions :meth:`compact` would remove (changes nothing)."""
        protected = set(keep)
        for row in self.connection.execute(
            "SELECT execution, source_execution FROM migration_lineage"
        ):
            protected.update((row[0], row[1]))
        newest = {
            row[0]
            for row in self.connection.execute(
                "SELECT id FROM executions ORDER BY rowid DESC LIMIT ?", (retain,)
            )
        }
        return [
            row[0]
            for row in self.connection.execute(
                "SELECT id FROM executions WHERE state != 'pending'"
            )
            if row[0] not in protected and row[0] not in newest
        ]

    def execution_ids(self) -> set[str]:
        return {row[0] for row in self.connection.execute("SELECT id FROM executions")}

    def session_ids(self) -> set[str]:
        return {
            row[0]
            for row in self.connection.execute("SELECT id FROM deployment_sessions")
        }

    def forget_sessions(self, session_ids: set[str]) -> int:
        """Delete the archives of retired releases' sessions (0.49.0)."""
        ids = sorted(session_ids & self.session_ids())
        if not ids:
            return 0
        with self._transaction():
            self.connection.executemany(
                "DELETE FROM deployment_sessions WHERE id=?", [(item,) for item in ids]
            )
        return len(ids)

    def references_text(self, *, skip: Collection[str] = ()) -> Iterator[str]:
        """Every JSON text that may name private versions: session archives,
        execution bindings and action payloads (of executions not in
        ``skip``; session ids in ``skip`` are skipped too)."""
        for row in self.connection.execute(
            "SELECT id, archive FROM deployment_sessions"
        ):
            if row[0] not in skip:
                yield row[1]
        for row in self.connection.execute("SELECT id, binding FROM executions"):
            if row[0] not in skip:
                yield row[1]
        for row in self.connection.execute("SELECT execution, payload FROM actions"):
            if row[0] not in skip:
                yield row[1]

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

    def record_diagnosis(self, execution: str, diagnosis: dict[str, Any]) -> None:
        """Keep the (already redacted, bounded) diagnosis of a failed execution."""
        encoded = json.dumps(
            diagnosis, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if len(encoded) > 256_000:
            raise ValueError("diagnosis too large")
        self._capacity(encoded)
        with self._transaction():
            self.connection.execute(
                "INSERT OR REPLACE INTO diagnoses(execution,payload) VALUES (?,?)",
                (execution, encoded),
            )

    def clear_diagnosis(self, execution: str) -> None:
        """Forget an earlier attempt's diagnosis (a resumed execution)."""
        if self.diagnosis(execution) is None:
            return
        with self._transaction():
            self.connection.execute(
                "DELETE FROM diagnoses WHERE execution=?", (execution,)
            )

    def diagnosis(self, execution: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload FROM diagnoses WHERE execution=?", (execution,)
        ).fetchone()
        if row is None:
            return None
        value = strict_json(row[0])
        return value if isinstance(value, dict) else None

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
            diagnosis = self.diagnosis(execution)
            if diagnosis is not None:
                result["diagnosis"] = diagnosis
        return result

    def close(self) -> None:
        self.connection.close()
