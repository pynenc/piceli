"""Private control records and transactional outbox; engine journals own writes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from piceli.services.contracts import Event
from piceli.services.query import QueryError

SCHEMA_VERSION = 1


def now() -> str:
    return datetime.now(UTC).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Store:
    """Single dispatcher, durable admission, immutable plans and bounded events.

    Every method opens its own SQLite connection; no connection crosses threads.
    The lifetime dispatcher lock prevents a second server claiming live work.
    Construction alone does not create files.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._guard = threading.RLock()
        self._dispatcher: IO[str] | None = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._guard:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            if self.path.is_symlink():
                raise QueryError("ui-state-invalid", 409)
            connection = sqlite3.connect(self.path, timeout=10)
            os.chmod(self.path, 0o600)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in {0, SCHEMA_VERSION}:
                    raise QueryError("ui-state-invalid", 409)
                if version == 0:
                    if connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchone():
                        raise QueryError("ui-state-invalid", 409)
                    schema = """
                        CREATE TABLE records (
                            kind TEXT NOT NULL, id TEXT NOT NULL, app TEXT NOT NULL,
                            state TEXT NOT NULL, data TEXT NOT NULL, private TEXT NOT NULL,
                            scope TEXT, created TEXT NOT NULL,
                            PRIMARY KEY(kind,id));
                        CREATE UNIQUE INDEX active_scope ON records(scope)
                            WHERE scope IS NOT NULL AND state IN ('queued','running','cancelling');
                        CREATE TABLE idempotency (
                            principal TEXT NOT NULL, key TEXT NOT NULL,
                            fingerprint TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                            PRIMARY KEY(principal,key));
                        CREATE TABLE outbox (
                            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                            app TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                            state TEXT NOT NULL, at TEXT NOT NULL);
                        PRAGMA user_version=1;
                    """
                    for statement in schema.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                try:
                    yield connection
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            finally:
                connection.close()

    def acquire_dispatcher(self) -> None:
        with self._guard:
            if self._dispatcher is not None:
                return
            with self.transaction():
                pass
            descriptor = os.open(
                str(self.path) + ".runner.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            handle = os.fdopen(descriptor, "a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                raise QueryError("ui-operation-conflict", 409) from None
            self._dispatcher = handle

    def close(self) -> None:
        with self._guard:
            if self._dispatcher is not None:
                self._dispatcher.close()
                self._dispatcher = None

    @staticmethod
    def _event(
        connection: sqlite3.Connection, kind: str, value: Mapping[str, Any]
    ) -> None:
        connection.execute(
            "INSERT INTO outbox(app,kind,id,state,at) VALUES(?,?,?,?,?)",
            (
                value["application_id"],
                kind,
                value["id"],
                value.get("state", "planned"),
                now(),
            ),
        )
        connection.execute(
            "DELETE FROM outbox WHERE sequence <= (SELECT COALESCE(MAX(sequence),0)-10000 FROM outbox)"
        )

    @staticmethod
    def _insert(
        connection: sqlite3.Connection,
        kind: str,
        value: Mapping[str, Any],
        private: Mapping[str, Any],
        scope: str | None,
    ) -> None:
        try:
            connection.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?)",
                (
                    kind,
                    value["id"],
                    value["application_id"],
                    value.get("state", "planned"),
                    canonical(value),
                    canonical(private),
                    scope,
                    value.get("created_at") or now(),
                ),
            )
        except sqlite3.IntegrityError:
            raise QueryError("ui-operation-conflict", 409) from None
        Store._event(connection, kind, value)

    def put(
        self,
        kind: str,
        value: Mapping[str, Any],
        *,
        private: Mapping[str, Any] | None = None,
    ) -> None:
        with self.transaction() as connection:
            self._insert(connection, kind, value, private or {}, None)

    def admit(
        self,
        kind: str,
        value: Mapping[str, Any],
        *,
        private: Mapping[str, Any],
        scope: str,
        key: str,
        fingerprint: str,
        principal: str = "local",
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            found = connection.execute(
                "SELECT * FROM idempotency WHERE principal=? AND key=?",
                (principal, key),
            ).fetchone()
            if found:
                if found["fingerprint"] != fingerprint or found["kind"] != kind:
                    raise QueryError("ui-idempotency-conflict", 409)
                row = connection.execute(
                    "SELECT data FROM records WHERE kind=? AND id=?",
                    (kind, found["id"]),
                ).fetchone()
                if row is None:
                    raise QueryError("ui-state-invalid", 409)
                return dict(json.loads(row["data"]))
            self._insert(connection, kind, value, private, scope)
            connection.execute(
                "INSERT INTO idempotency VALUES(?,?,?,?,?)",
                (principal, key, fingerprint, kind, value["id"]),
            )
            return dict(value)

    def idempotent(
        self, key: str, fingerprint: str, kind: str, *, principal: str = "local"
    ) -> dict[str, Any] | None:
        """Find retries before mutable preconditions are checked again."""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM idempotency WHERE principal=? AND key=?",
                (principal, key),
            ).fetchone()
            if row is None:
                return None
            if row["fingerprint"] != fingerprint or row["kind"] != kind:
                raise QueryError("ui-idempotency-conflict", 409)
            value = connection.execute(
                "SELECT data FROM records WHERE kind=? AND id=?", (kind, row["id"])
            ).fetchone()
            return dict(json.loads(value["data"]))

    def get(self, kind: str, id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT data,private FROM records WHERE kind=? AND id=?", (kind, id)
            ).fetchone()
            if row is None:
                raise QueryError("ui-not-found")
            return dict(json.loads(row["data"])), dict(json.loads(row["private"]))

    def update(
        self,
        kind: str,
        value: Mapping[str, Any],
        *,
        private: Mapping[str, Any] | None = None,
    ) -> None:
        if kind in {"plan", "preview"}:
            raise QueryError("ui-state-invalid", 409)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT private FROM records WHERE kind=? AND id=?", (kind, value["id"])
            ).fetchone()
            if row is None:
                raise QueryError("ui-not-found")
            connection.execute(
                "UPDATE records SET state=?,data=?,private=? WHERE kind=? AND id=?",
                (
                    value.get("state", "planned"),
                    canonical(value),
                    canonical(private) if private is not None else row["private"],
                    kind,
                    value["id"],
                ),
            )
            self._event(connection, kind, value)

    def records(
        self, kind: str, application_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT data FROM records WHERE kind=? AND (? IS NULL OR app=?) ORDER BY created DESC",
                (kind, application_id, application_id),
            ).fetchall()
            return [dict(json.loads(row["data"])) for row in rows]

    def active(self, kind: str) -> list[dict[str, Any]]:
        """All nonterminal admissions, independent of history/list retention."""
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT data FROM records WHERE kind=? AND state IN ('queued','running','cancelling') ORDER BY created",
                (kind,),
            ).fetchall()
            return [dict(json.loads(row["data"])) for row in rows]

    def cancel(
        self, id: str, *, key: str, fingerprint: str, principal: str = "local"
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Durably request cancellation with the same global retry-key contract."""
        with self.transaction() as connection:
            retry = connection.execute(
                "SELECT * FROM idempotency WHERE principal=? AND key=?",
                (principal, key),
            ).fetchone()
            if retry is not None and (
                retry["fingerprint"] != fingerprint or retry["id"] != id
            ):
                raise QueryError("ui-idempotency-conflict", 409)
            row = connection.execute(
                "SELECT data,private FROM records WHERE kind='operation' AND id=?",
                (id,),
            ).fetchone()
            if row is None:
                raise QueryError("ui-not-found")
            value, private = json.loads(row["data"]), json.loads(row["private"])
            if retry is None:
                connection.execute(
                    "INSERT INTO idempotency VALUES(?,?,?,?,?)",
                    (principal, key, fingerprint, "operation", id),
                )
                if value["state"] in {"queued", "running", "cancelling"}:
                    value.update(
                        state="cancelled"
                        if value["state"] == "queued"
                        else "cancelling",
                        updated_at=now(),
                    )
                    private["cancel_requested"] = True
                    connection.execute(
                        "UPDATE records SET state=?,data=?,private=? WHERE kind='operation' AND id=?",
                        (value["state"], canonical(value), canonical(private), id),
                    )
                    self._event(connection, "operation", value)
            return dict(value), dict(private)

    def cursor(self) -> str:
        with self.transaction() as connection:
            return str(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM outbox"
                ).fetchone()[0]
            )

    def events(
        self, application_id: str, after: str | None, *, limit: int = 100
    ) -> tuple[list[Event], str, bool]:
        """Read one bounded replay window from the transactional outbox.

        Cursors are opaque to clients. An expired cursor or a client lagging
        beyond one window gets a reset instead of silently missing changes.
        The application filter is applied before any event leaves the store.
        """
        if not 1 <= limit <= 1000:
            raise ValueError("invalid event window")
        if after is not None and (
            len(after) > 20 or not after.isascii() or not after.isdecimal()
        ):
            raise QueryError("ui-invalid-request", 422)
        with self.transaction() as connection:
            oldest, latest = connection.execute(
                "SELECT COALESCE(MIN(sequence),0),COALESCE(MAX(sequence),0) FROM outbox"
            ).fetchone()
            cursor = latest if after is None else int(after)
            if cursor > latest:
                raise QueryError("ui-invalid-request", 422)
            if oldest and cursor < oldest - 1:
                return [], str(latest), True
            rows = connection.execute(
                "SELECT sequence,kind,id FROM outbox WHERE app=? AND sequence>? "
                "ORDER BY sequence LIMIT ?",
                (application_id, cursor, limit + 1),
            ).fetchall()
            if len(rows) > limit:
                return [], str(latest), True
            events = [
                Event(
                    cursor=str(row["sequence"]),
                    scope=application_id,
                    kind="operation" if row["kind"] == "operation" else "upsert",
                    subject_id=row["id"],
                )
                for row in rows
            ]
            return events, events[-1].cursor if events else str(latest), False
