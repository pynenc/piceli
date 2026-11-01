"""The execution journal forgets finished executions nothing needs (0.47.0)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from piceli.k8s.ops.execution_journal import ExecutionJournal

PAD = "x" * 20_000


def _run(journal: ExecutionJournal, state: str = "ready") -> str:
    execution = uuid.uuid4().hex
    journal.start(execution, {"pad": PAD}, [uuid.uuid4().hex])
    journal.record(execution, 0, "applied", {"pad": PAD})
    if state != "pending":
        journal.set_state(execution, state)
    return execution


def _ids(journal: ExecutionJournal) -> set[str]:
    return {row[0] for row in journal.connection.execute("SELECT id FROM executions")}


def test_finished_executions_go_and_the_file_shrinks(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "journal.sqlite")
    old = [_run(journal) for _ in range(30)]
    pending = _run(journal, "pending")
    failed = _run(journal, "failed")
    latest = _run(journal)
    before = journal.path.stat().st_size
    found = journal.compact({latest}, retain=3)
    kept = _ids(journal)
    assert pending in kept and latest in kept and failed in kept  # failed: newest 3
    assert not kept & set(old[:-1]) and found["removed"] == 30
    assert found["vacuumed"] == 1 and journal.path.stat().st_size < before / 4
    assert journal.connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    with pytest.raises(ValueError):
        journal.summary(old[0])
    assert journal.summary(latest)["state"] == "ready"


def test_lineage_and_sessions_are_never_removed(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "journal.sqlite")
    source, migrated = _run(journal), _run(journal)
    journal.connection.execute(
        "INSERT INTO migration_lineage VALUES (?,?,?,?)",
        (migrated, source, "a" * 64, "b" * 64),
    )
    journal.connection.commit()
    session = uuid.uuid4().hex
    journal.store_session(session, {"pad": PAD})
    for _ in range(5):
        _run(journal)
    journal.compact(set(), retain=0)
    assert {source, migrated} <= _ids(journal)
    assert journal.session(session) == {"pad": PAD}


def test_the_budget_counts_used_pages_not_the_file(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "journal.sqlite", max_bytes=1_000_000)
    runs = [_run(journal) for _ in range(20)]
    with pytest.raises(ValueError, match="budget exhausted"):
        for _ in range(40):
            _run(journal)
    journal.connection.execute("DELETE FROM actions")
    journal.connection.execute("UPDATE executions SET binding='{}'")
    journal.connection.commit()  # freed pages, the file is as big as before
    assert journal.used_bytes() < journal.path.stat().st_size
    _run(journal)  # room again without a VACUUM
    assert runs


def test_the_runner_keeps_the_latest_of_each_release_and_recent_history(
    tmp_path: Path,
) -> None:
    import json
    from types import SimpleNamespace

    from piceli.k8s.release_runner import ReleaseRunner, _History

    journal = ExecutionJournal(tmp_path / "journal.sqlite")
    runs = [_run(journal) for _ in range(80)]
    first_of_other = runs[0]
    entries = [
        {"release": "other" if i == 0 else "web", "intent": "apply",
         "execution_id": execution}
        for i, execution in enumerate(runs)
    ]  # fmt: skip
    history = tmp_path / "history.json"
    history.write_text(json.dumps({"schema_version": 1, "entries": entries}))
    runner = SimpleNamespace(history=_History(history))
    ReleaseRunner._compact(runner, journal)  # type: ignore[arg-type]
    kept = _ids(journal)
    assert first_of_other in kept  # the latest (only) execution of "other"
    assert set(runs[-50:]) <= kept and not kept & set(runs[1:30])
    history.write_text("{}")  # malformed: nothing is removed
    _run(journal)
    count = len(_ids(journal))
    ReleaseRunner._compact(runner, journal)  # type: ignore[arg-type]
    assert len(_ids(journal)) == count
