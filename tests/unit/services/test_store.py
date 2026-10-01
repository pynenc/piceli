"""Admission/outbox atomicity, process exclusion and durable retry semantics."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from piceli.services.query import QueryError
from piceli.services.store import Store


def record(id: str, state: str = "queued") -> dict:
    return {"id": id, "application_id": "shop", "state": state, "created_at": id}


def test_concurrent_duplicate_admission_is_one_record_and_one_event(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "control" / "operations.sqlite3")

    def admit(index: int) -> dict:
        return store.admit(
            "operation",
            record(str(index)),
            private={},
            scope="scope",
            key="same-request",
            fingerprint="same-body",
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(admit, range(8)))
    assert len({value["id"] for value in responses}) == 1
    assert len(store.records("operation")) == 1
    assert store.cursor() == "1"
    restarted = Store(store.path)
    assert (
        restarted.idempotent("same-request", "same-body", "operation") == responses[0]
    )
    with pytest.raises(QueryError, match="ui-idempotency-conflict"):
        restarted.idempotent("same-request", "different", "operation")
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_active_scope_refusal_rolls_back_idempotency_and_event(tmp_path: Path) -> None:
    store = Store(tmp_path / "control" / "operations.sqlite3")
    store.admit(
        "operation",
        record("one"),
        private={},
        scope="scope",
        key="one",
        fingerprint="one",
    )
    with pytest.raises(QueryError, match="ui-operation-conflict"):
        store.admit(
            "operation",
            record("two"),
            private={},
            scope="scope",
            key="two",
            fingerprint="two",
        )
    assert store.idempotent("two", "two", "operation") is None
    assert store.cursor() == "1"
    store.update("operation", record("one", "failed"))
    store.admit(
        "operation",
        record("two"),
        private={},
        scope="scope",
        key="two",
        fingerprint="two",
    )
    assert [item["id"] for item in store.active("operation")] == ["two"]


def test_second_approval_conflicting_with_an_active_run_is_a_public_refusal(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "operations.sqlite3")
    waiting = store.admit(
        "pipeline_operation",
        record("waiting", "queued"),
        private={},
        scope="same-release",
        key="first",
        fingerprint="first",
    )
    store.update("pipeline_operation", {**waiting, "state": "awaiting-review"})
    store.admit(
        "pipeline_operation",
        record("other", "queued"),
        private={},
        scope="same-release",
        key="other",
        fingerprint="other",
    )
    with pytest.raises(QueryError, match="ui-operation-conflict"):
        store.update("pipeline_operation", {**waiting, "state": "queued"})
    assert store.get("pipeline_operation", "waiting")[0]["state"] == "awaiting-review"


def test_future_schema_and_second_dispatcher_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "control" / "operations.sqlite3"
    first, second = Store(path), Store(path)
    first.acquire_dispatcher()
    try:
        with pytest.raises(QueryError, match="ui-operation-conflict"):
            second.acquire_dispatcher()
    finally:
        first.close()
    second.acquire_dispatcher()
    second.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(QueryError, match="ui-state-invalid"):
        first.records("operation")


def test_idempotency_is_scoped_by_explicit_principal(tmp_path: Path) -> None:
    store = Store(tmp_path / "operations.sqlite3")
    value = store.admit(
        "operation",
        record("one"),
        private={},
        scope="scope",
        key="key",
        fingerprint="body",
        principal="operator",
    )
    assert store.idempotent("key", "body", "operation", principal="operator") == value
    assert store.idempotent("key", "body", "operation", principal="local") is None


def test_interrupted_schema_initialization_rolls_back_completely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "control" / "operations.sqlite3"
    connect = sqlite3.connect

    class InterruptedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if "CREATE UNIQUE INDEX" in sql:
                raise sqlite3.OperationalError("injected interruption")
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *args, **kwargs: connect(*args, factory=InterruptedConnection, **kwargs),
    )
    with pytest.raises(sqlite3.OperationalError):
        Store(path).records("operation")
    monkeypatch.setattr(sqlite3, "connect", connect)
    assert Store(path).records("operation") == []


def test_replay_is_scoped_bounded_and_resets_after_retention(tmp_path: Path) -> None:
    store = Store(tmp_path / "operations.sqlite3")
    store.put("operation", record("one", "succeeded"))
    store.put(
        "operation", {**record("foreign", "succeeded"), "application_id": "other"}
    )
    store.put("evaluation", record("two", "succeeded"))
    batch, cursor, reset = store.events("shop", "0")
    assert not reset and cursor == "3"
    assert [(event.cursor, event.kind, event.subject_id) for event in batch] == [
        ("1", "operation", "one"),
        ("3", "upsert", "two"),
    ]
    assert all(event.scope == "shop" for event in batch)
    assert store.events("other", "0")[0][0].subject_id == "foreign"
    assert store.events("shop", "0", limit=1) == ([], "3", True)
    assert store.events("shop", "3") == ([], "3", False)
    with store.transaction() as connection:
        connection.execute("DELETE FROM outbox WHERE sequence<=2")
    assert store.events("shop", "0") == ([], "3", True)
    with pytest.raises(QueryError, match="ui-invalid-request"):
        store.events("shop", "4")
    with pytest.raises(QueryError, match="ui-invalid-request"):
        store.events("shop", "-1")
