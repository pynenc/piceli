"""Release retention (0.49.0): keep the newest releases, retire the rest."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from piceli.k8s.ops.discovery import PlanTarget
from piceli.k8s.ops.execution_journal import ExecutionJournal
from piceli.k8s.ops.secret_versions import SecretVersionStore
from piceli.k8s.release import RELEASE_CATALOG_SCHEMA_VERSION, ReleaseCatalog
from piceli.k8s.release_retention import INTENT, ReleaseRetention
from piceli.k8s.release_runner import ReleaseError, ReleaseRunner

TARGET = PlanTarget("cluster-uid", "prod")
T0 = datetime(2026, 1, 1, tzinfo=UTC)
LATER = T0 + timedelta(days=1)


class State:
    """A state directory with fake releases: every place that names a version."""

    def __init__(self, tmp_path: Path, keep: int | None = None) -> None:
        self.dir = tmp_path / "state"
        self.dir.mkdir(mode=0o700)
        self.spec = SimpleNamespace(
            state_dir=self.dir,
            catalog_path=self.dir / "catalog.json",
            journal_path=self.dir / "journal.sqlite",
            secret_store_path=self.dir / "secrets.sqlite",
            model=SimpleNamespace(
                release=SimpleNamespace(
                    approval_window_seconds=900, keep_releases=keep
                ),
                target=SimpleNamespace(namespace="prod"),
            ),
        )
        self.runner = ReleaseRunner(self.spec)  # type: ignore[arg-type]
        self.records: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.selected: str | None = None
        self.info: dict[str, dict[str, Any]] = {}

    def open(self) -> tuple[ExecutionJournal, SecretVersionStore]:
        return (
            ExecutionJournal(self.spec.journal_path),
            SecretVersionStore(self.spec.secret_store_path),
        )

    def add(self, name: str, created: datetime, *, state: str = "ready") -> None:
        journal, store = self.open()
        try:
            session = uuid.uuid4().hex
            execution = uuid.uuid4().hex
            bound = store.put_once(TARGET, session, "password", "secret")
            internal = store.put(TARGET, "internal")
            before = store.put(TARGET, {"kind": "ConfigMap"})
            after = store.put(TARGET, {"kind": "ConfigMap", "v": 2})
            archive = {
                "schema_version": 1,
                "composition": [],
                "revision": {},
                "session_id": session,
                "private_inputs": [{"name": "password", "reference": bound.__dict__}],
                "bundle": {"execution_id": execution},
            }
            journal.store_session(session, archive)
            journal.start(
                execution,
                {"bindings": [{"reference": bound.__dict__}]},
                [uuid.uuid4().hex],
            )
            journal.record(
                execution,
                0,
                "ready",
                {"before": before.__dict__, "after": after.__dict__},
            )
            journal.set_state(execution, state)
        finally:
            journal.close()
            store.close()
        self.records.append(
            {
                "name": name,
                "source": {"kind": "git", "identity": "a" * 40},
                "archive": archive,
                "namespace": "prod",
                "ttl_seconds": None,
                "retained_pvc_policy": "retain",
            }
        )
        releases = self.dir / "releases"
        releases.mkdir(exist_ok=True)
        (releases / f"{name}.json").write_text(
            json.dumps(
                {
                    "created_at": created.isoformat(),
                    "secret_refs": {"internal": internal.version},
                }
            )
        )
        (releases / f"{name}.discovery.json").write_text("{}")
        checks = self.dir / "checks"
        checks.mkdir(exist_ok=True)
        (checks / f"{execution}.json").write_text("{}")
        self.history.append(
            {
                "release": name,
                "intent": "apply",
                "mode": "create",
                "execution_id": execution,
                "state": state,
            }
        )
        if state == "ready":
            self.selected = name
        self.info[name] = {
            "session": session,
            "execution": execution,
            "versions": {
                bound.version,
                internal.version,
                before.version,
                after.version,
            },
        }
        self.write()

    def write(self) -> None:
        self.spec.catalog_path.write_text(
            json.dumps(
                {
                    "schema_version": RELEASE_CATALOG_SCHEMA_VERSION,
                    "releases": self.records,
                    "selected": self.selected,
                }
            )
        )
        (self.dir / "history.json").write_text(
            json.dumps({"schema_version": 1, "entries": self.history})
        )

    def names(self) -> list[str]:
        return [r.name for r in ReleaseCatalog(self.spec.catalog_path).records()]

    def versions(self) -> set[str]:
        _, store = self.open()
        try:
            return set(store.version_sizes())
        finally:
            store.close()

    def sessions(self) -> set[str]:
        journal, _ = self.open()
        try:
            return journal.session_ids()
        finally:
            journal.close()

    def retain(self, keep: int, now: datetime = T0, dry: bool = False) -> Any:
        return ReleaseRetention(self.runner, keep, now=now).run(dry_run=dry)


def _fill(tmp_path: Path, count: int, keep: int | None = None) -> State:
    state = State(tmp_path, keep)
    for index in range(count):
        state.add(f"web-{index:02d}", T0 + timedelta(minutes=index))
    return state


def _big(tmp_path: Path) -> State:
    """30 releases and 50 more history entries of the newest one, so the
    oldest releases' executions are beyond what compaction keeps anyway."""
    state = _fill(tmp_path, 30)
    newest = state.history[-1]
    state.history += [
        dict(newest) | {"execution_id": uuid.uuid4().hex} for _ in range(50)
    ]
    state.write()
    return state


def test_keeps_newest_n_by_created_at(tmp_path: Path) -> None:
    state = State(tmp_path)
    # Names sort the opposite way to creation: retention follows created_at.
    for index in range(25):
        state.add(f"web-{99 - index:02d}", T0 + timedelta(minutes=index))
    report = state.retain(20)
    assert report["state"] == "done"
    assert sorted(report["retired"]) == [f"web-{n}" for n in range(95, 100)]
    assert len(state.names()) == 20


def test_keeps_selected_and_previous_even_if_old(tmp_path: Path) -> None:
    state = _fill(tmp_path, 6)
    state.selected = "web-00"  # rolled back to the oldest
    state.history.append(
        dict(state.history[1]) | {"intent": "rollback", "execution_id": "e" * 32}
    )
    state.write()
    report = state.retain(2)
    kept = set(state.names())
    assert {"web-00", "web-01", "web-04", "web-05"} == kept, report


def test_keeps_auto_rollback_target(tmp_path: Path) -> None:
    state = _fill(tmp_path, 5)
    # Two newer releases failed their checks: the last ready one (web-02)
    # is what an automatic rollback returns to.
    state.add("web-05", T0 + timedelta(hours=1), state="checks-failed")
    state.add("web-06", T0 + timedelta(hours=2), state="checks-failed")
    state.selected = "web-04"
    state.write()
    state.retain(2)
    assert {"web-03", "web-04", "web-05", "web-06"} <= set(state.names())


def test_off_by_default_and_below_two_refused(tmp_path: Path) -> None:
    state = _fill(tmp_path, 4)
    with pytest.raises(ReleaseError) as raised:
        state.runner.retention()
    assert raised.value.code == "release-retention-off"
    with pytest.raises(ReleaseError):
        state.runner.retention(keep=1)
    with pytest.raises(ValueError):
        ReleaseRetention(state.runner, 0)
    outcome: dict[str, Any] = {}
    state.runner._retain_after_run(outcome)  # keep_releases unset: nothing
    assert outcome == {} and len(state.names()) == 4


def test_spec_refuses_keep_releases_below_two() -> None:
    from pydantic import ValidationError

    from piceli.k8s.release_spec import ReleaseSettings

    base = {
        "name": "web",
        "owner": "owner",
        "field_manager": "fm",
        "composition": "compose.py:build",
        "state_dir": "state",
    }
    assert ReleaseSettings(**base).keep_releases is None
    assert ReleaseSettings(**base, keep_releases=2).keep_releases == 2
    for value in (0, 1):
        with pytest.raises(ValidationError, match="at least 2"):
            ReleaseSettings(**base, keep_releases=value)


def test_dry_run_changes_nothing_and_lists_everything(tmp_path: Path) -> None:
    state = _fill(tmp_path, 5)

    def files() -> dict[str, bytes]:
        return {
            str(path): path.read_bytes()
            for path in state.dir.rglob("*")
            if path.is_file() and path.suffix != ".lock"  # the journal lock
        }

    before = files()
    report = state.retain(3, dry=True)
    assert before == files()
    assert report["state"] == "dry-run"
    assert report["retired"] == ["web-00", "web-01"]
    assert sorted(report["sessions"]) == sorted(
        state.info[n]["session"] for n in ("web-00", "web-01")
    )
    assert "releases/web-00.json" in report["files"]
    assert "releases/web-01.discovery.json" in report["files"]
    secrets = report["secret_versions"]
    # Their executions are among the newest 20 and stay journaled, so only
    # the internal outputs named by the retired sidecars are unreferenced.
    assert secrets["total"] == 20 and secrets["unreferenced"] == 2
    assert secrets["removable_now"] == 0  # none has waited its grace yet
    assert report["backups"] == {"count": 0, "bytes": 0}


def test_deferred_while_plan_pending(tmp_path: Path) -> None:
    state = _fill(tmp_path, 4)
    plans = state.dir / "plans"
    plans.mkdir()
    (plans / f"{'1' * 64}.json").write_text(
        json.dumps({"expires_at": (T0 + timedelta(hours=1)).isoformat()})
    )
    report = state.retain(2)
    assert report["state"] == "deferred" and report["reason"] == "plan-pending"
    assert len(state.names()) == 4
    (plans / f"{'1' * 64}.json").write_text(
        json.dumps({"expires_at": (T0 - timedelta(hours=1)).isoformat()})
    )
    assert state.retain(2)["state"] == "done"  # an expired plan does not defer


def test_deferred_while_running(tmp_path: Path) -> None:
    state = _fill(tmp_path, 4)
    state.history.append(dict(state.history[-1]) | {"state": "running"})
    state.write()
    report = state.retain(2)
    assert report["reason"] == "execution-running" and len(state.names()) == 4


def test_retired_release_files_removed(tmp_path: Path) -> None:
    state = _big(tmp_path)
    state.retain(2)
    old, new = state.info["web-00"], state.info["web-29"]
    releases = state.dir / "releases"
    assert not (releases / "web-00.json").exists()
    assert not (releases / "web-00.discovery.json").exists()
    assert (releases / "web-29.json").exists()
    assert (releases / "web-29.discovery.json").exists()
    assert old["session"] not in state.sessions()
    assert new["session"] in state.sessions()
    _, store = state.open()
    try:
        bound = {s for s, _ in store.session_versions()}
    finally:
        store.close()
    assert old["session"] not in bound and new["session"] in bound
    assert not (state.dir / "checks" / f"{old['execution']}.json").exists()
    assert (state.dir / "checks" / f"{new['execution']}.json").exists()
    assert not (state.dir / INTENT).exists()


def test_compact_ignores_retired_releases(tmp_path: Path) -> None:
    state = _big(tmp_path)
    state.retain(2)
    journal, _ = state.open()
    try:
        ids = journal.execution_ids()
    finally:
        journal.close()
    # Retired releases lose even their latest execution beyond the newest 20.
    assert state.info["web-00"]["execution"] not in ids
    assert state.info["web-29"]["execution"] in ids


def test_sweep_needs_two_runs_and_grace(tmp_path: Path) -> None:
    state = _big(tmp_path)
    retired = state.info["web-00"]["versions"] | state.info["web-01"]["versions"]
    first = state.retain(2)
    assert first["secret_versions"]["removed"] == 0
    assert retired <= state.versions()  # first seen now
    inside = state.retain(2, now=T0 + timedelta(minutes=30))
    assert inside["secret_versions"]["removed"] == 0
    assert retired <= state.versions()  # still inside the 1 h grace
    after = state.retain(2, now=LATER)
    assert after["secret_versions"]["removed"] >= len(retired) == 8
    assert after["secret_versions"]["removed_bytes"] > 0
    assert not retired & state.versions()
    for name in ("web-28", "web-29"):
        assert state.info[name]["versions"] <= state.versions()


def test_orphaned_versions_swept_after_grace(tmp_path: Path) -> None:
    """Object copies whose executions an earlier compaction deleted."""
    state = _fill(tmp_path, 3)
    journal, store = state.open()
    try:
        orphan = store.put(TARGET, {"kind": "ConfigMap", "old": True}).version
    finally:
        journal.close()
        store.close()
    state.retain(5)
    assert orphan in state.versions()
    report = state.retain(5, now=LATER)
    assert report["retired"] == [] and report["secret_versions"]["removed"] == 1
    assert orphan not in state.versions()


def test_sweep_spares_plan_in_progress(tmp_path: Path) -> None:
    state = _fill(tmp_path, 3)
    state.retain(2)  # nothing pending from earlier runs
    _, store = state.open()
    try:
        fresh = store.put(TARGET, "written by a plan, not named yet").version
    finally:
        store.close()
    state.retain(2, now=LATER)
    assert fresh in state.versions()  # first seen only now


def test_mark_walks_all_ref_places(tmp_path: Path) -> None:
    state = _fill(tmp_path, 2)
    _, store = state.open()
    try:
        planned = store.put(TARGET, "named by a pending plan only")
    finally:
        store.close()
    plans = state.dir / "plans"
    plans.mkdir()
    (plans / f"{'2' * 64}.json").write_text(
        json.dumps(
            {
                "expires_at": (T0 - timedelta(hours=1)).isoformat(),
                "discovery": {"x": [planned.__dict__]},
            }
        )
    )
    state.retain(2)
    state.retain(2, now=LATER)
    for name in ("web-00", "web-01"):  # archive, sidecar, binding, payload
        assert state.info[name]["versions"] <= state.versions()
    assert planned.version in state.versions()


def test_kept_release_never_loses_a_version(tmp_path: Path) -> None:
    state = _fill(tmp_path, 6)
    for day in range(1, 4):
        state.retain(3, now=T0 + timedelta(days=day))
    for name in state.names():
        assert state.info[name]["versions"] <= state.versions()


def test_crash_after_catalog_write_is_finished_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _fill(tmp_path, 5)

    def crash(self: Any, ids: Any) -> int:
        raise RuntimeError("power cut")

    monkeypatch.setattr(ExecutionJournal, "forget_sessions", crash)
    with pytest.raises(RuntimeError):
        state.retain(2)
    assert state.names() == ["web-03", "web-04"]  # step 2 done
    assert (state.dir / INTENT).exists()
    for name in state.names():  # kept releases intact
        assert (state.dir / "releases" / f"{name}.json").exists()
        assert state.info[name]["session"] in state.sessions()
    monkeypatch.undo()
    report = state.retain(2)
    assert sorted(report["retired"]) == ["web-00", "web-01", "web-02"]
    assert not (state.dir / INTENT).exists()
    assert state.info["web-00"]["session"] not in state.sessions()
    assert not (state.dir / "releases" / "web-01.json").exists()


def test_backups_kept_and_reported(tmp_path: Path) -> None:
    state = _fill(tmp_path, 4)
    backup = state.dir / "backups" / state.info["web-00"]["execution"]
    backup.mkdir(parents=True)
    (backup / "ConfigMap-settings.json").write_text("x" * 100)
    report = state.retain(2)
    assert report["backups"] == {"count": 1, "bytes": 100}
    assert (backup / "ConfigMap-settings.json").exists()


def test_runs_after_a_ready_apply_when_set(tmp_path: Path) -> None:
    state = _fill(tmp_path, 4, keep=2)
    outcome: dict[str, Any] = {}
    state.runner._retain_after_run(outcome)
    assert outcome["retention"]["state"] == "done"
    assert outcome["retention"]["retired"] == ["web-00", "web-01"]


def test_retired_name_says_so_on_rollback(tmp_path: Path) -> None:
    state = _fill(tmp_path, 4)
    state.retain(2)
    with pytest.raises(ReleaseError, match="retired") as raised:
        state.runner.resolve_rollback_target("web-00")
    assert raised.value.code == "unknown-release"


def test_catalog_remove_refuses_selected(tmp_path: Path) -> None:
    state = _fill(tmp_path, 3)
    catalog = ReleaseCatalog(state.spec.catalog_path)
    with pytest.raises(ValueError, match="selected"):
        catalog.remove(["web-02"])
    assert catalog.remove(["web-00", "missing"]) == ["web-00"]
    assert catalog.remove(["web-00"]) == []


def test_secret_budget_counts_used_pages(tmp_path: Path) -> None:
    store = SecretVersionStore(tmp_path / "secrets.sqlite", max_bytes=1_000_000)
    try:
        with pytest.raises(ValueError, match="budget exhausted"):
            for _ in range(100):
                store.put(TARGET, "x" * 20_000)
        store.connection.execute("DELETE FROM versions")
        store.connection.commit()  # freed pages; the file is as big as before
        assert store.used_bytes() < store.path.stat().st_size
        store.put(TARGET, "x" * 20_000)  # room again without a VACUUM
    finally:
        store.close()


def test_sweep_vacuums_when_a_quarter_is_free(tmp_path: Path) -> None:
    store = SecretVersionStore(tmp_path / "secrets.sqlite")
    try:
        for _ in range(40):
            store.put(TARGET, "x" * 20_000)
        store.sweep(set(), grace_seconds=0, now=T0)
        found = store.sweep(set(), grace_seconds=0, now=LATER)
        assert found["removed"] == 40 and found["vacuumed"] == 1
        assert found["bytes_after"] < found["bytes_before"] / 4
    finally:
        store.close()
