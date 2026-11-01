"""Release retention (0.49.0): keep the newest releases, retire the rest.

An environment's release state grows with every release: the catalog record,
its session archive in the journal, the sidecar and discovery files, the
executions and the secret versions they name. With ``[release]
keep_releases = N`` the newest ``N`` releases (by sidecar ``created_at``) are
kept, plus the selected one, ``history.previous()`` and the release an
automatic rollback would return to; the rest are retired.

Retirement goes root first, so a run that stops midway never leaves a kept
release broken, and the next run finishes it (an intent file,
``retention.json``, names what was being retired):

1. write ``retention.json``;
2. remove the records from ``catalog.json`` (one locked atomic write);
3. delete their session archives (journal) and input bindings (secret store);
4. delete their sidecar and discovery files;
5. compact the journal, keeping only the latest execution of releases still
   catalogued, and delete check reports of executions no longer journaled;
6. mark every secret version something still names and sweep the rest, two
   runs and a grace period apart (a plan in progress writes versions before
   it names them);
7. remove ``retention.json``.

Replace backups (``backups/<execution>/``) are recovery data and are never
deleted; their size is reported so growth stays visible. ``history.json``
stays as the audit trail.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Collection, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from piceli.k8s.ops.bounds import timestamp
from piceli.k8s.ops.execution_journal import ExecutionJournal
from piceli.k8s.ops.secret_versions import SecretVersionStore

if TYPE_CHECKING:
    from piceli.k8s.release_runner import ReleaseRunner

logger = logging.getLogger(__name__)

#: The shortest grace period of the secret sweep (seconds).
MIN_GRACE_SECONDS = 3600
INTENT = "retention.json"
_HEX32 = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")


def refs_in(value: Any, store_id: str) -> Iterator[str]:
    """Versions named in a JSON value: every object whose keys are exactly
    ``store_id`` and ``version`` with this store's id (one generic walker, so
    new places that embed a reference stay covered)."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if set(item) == {"store_id", "version"}:
                if item["store_id"] == store_id and isinstance(item["version"], str):
                    yield item["version"]
                continue
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def refs_in_text(text: str, store_id: str) -> set[str]:
    """:func:`refs_in` of a JSON text; text that does not parse marks every
    32-hex token in it (never delete what cannot be read)."""
    if store_id not in text:
        return set()
    try:
        return set(refs_in(json.loads(text), store_id))
    except ValueError:
        return set(_HEX32.findall(text))


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    return 0


class ReleaseRetention:
    """One retention run over a runner's state directory."""

    def __init__(
        self,
        runner: ReleaseRunner,
        keep: int,
        *,
        now: datetime | None = None,
    ) -> None:
        if isinstance(keep, bool) or not isinstance(keep, int) or keep < 2:
            raise ValueError("keep_releases must be an integer of at least 2")
        self.runner = runner
        self.keep = keep
        self.now = now or datetime.now(UTC)
        self.state = runner.state
        window = runner.spec.model.release.approval_window_seconds
        self.grace = max(int(window), MIN_GRACE_SECONDS)

    # ---------------------------------------------------------------- inputs
    def _catalog(self) -> dict[str, Any]:
        path = self.runner.spec.catalog_path
        if not path.exists():
            return {"releases": [], "selected": None}
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or not isinstance(value.get("releases"), list):
            raise ValueError("release catalog is malformed")
        return value

    def _intent(self) -> list[dict[str, str]]:
        path = self.state / INTENT
        if not path.exists():
            return []
        value = json.loads(path.read_text())
        return [
            {"name": str(item["name"]), "session_id": str(item["session_id"])}
            for item in value.get("releases", [])
        ]

    def _deferred(self) -> str | None:
        """Why retention must wait: a pending plan or a running execution is
        rebuilt from its sidecar's ``previous_releases``, so no record it
        names may go until it is finished."""
        plans = self.state / "plans"
        if plans.is_dir():
            for path in plans.glob("*.json"):
                try:
                    expires = json.loads(path.read_text())["expires_at"]
                    if timestamp(expires, allow_future=True) <= self.now:
                        continue
                except (OSError, ValueError, KeyError, TypeError):
                    pass  # unreadable: treat it as pending
                return "plan-pending"
        entries = self.runner.history.entries()
        if entries and entries[-1].get("state") == "running":
            return "execution-running"
        return None

    def _protected(self, selected: str | None) -> set[str]:
        """The selected release, ``previous`` and the automatic rollback target."""
        history = self.runner.history
        deployed = history.deployed()
        names = {selected, history.previous()}
        if deployed:
            names.add(deployed[-1])
            names.add(next((n for n in reversed(deployed) if n != deployed[-1]), None))
        return {name for name in names if name}

    def _select(
        self, catalog: Mapping[str, Any]
    ) -> tuple[list[str], list[dict[str, str]]]:
        """``(kept names, doomed {name, session_id})`` for the catalog."""
        records = catalog["releases"]
        created = {
            item["name"]: str(
                self.runner._sidecar(item["name"]).get("created_at") or ""
            )
            for item in records
        }
        ordered = sorted(records, key=lambda i: (created[i["name"]], i["name"]))
        newest = {item["name"] for item in ordered[-self.keep :]}
        protected = self._protected(catalog.get("selected"))
        kept = [i["name"] for i in ordered if i["name"] in newest | protected]
        doomed = [
            {"name": i["name"], "session_id": str(i["archive"]["session_id"])}
            for i in ordered
            if i["name"] not in newest | protected
        ]
        return kept, doomed

    # ------------------------------------------------------------------ mark
    def _live(
        self,
        catalog: Mapping[str, Any],
        journal: ExecutionJournal,
        store: SecretVersionStore,
        *,
        names: Collection[str] = (),
        sessions: Collection[str] = (),
        executions: Collection[str] = (),
    ) -> set[str]:
        """Every version something still names, ignoring what is retired
        (``names``, ``sessions``, ``executions``: given by a dry run, which
        changes nothing; a real run marks after deleting them)."""
        sid = store.store_id
        live: set[str] = set()
        for item in catalog["releases"]:
            if item["name"] not in names:
                live.update(refs_in(item, sid))
        for text in journal.references_text(skip={*sessions, *executions}):
            live |= refs_in_text(text, sid)
        live.update(v for s, v in store.session_versions() if s not in sessions)
        releases = self.state / "releases"
        if releases.is_dir():
            for path in releases.glob("*.json"):
                stem = path.name.removesuffix(".json").removesuffix(".discovery")
                if stem in names:
                    continue
                text = path.read_text()
                try:
                    value = json.loads(text)
                except ValueError:
                    live.update(_HEX32.findall(text))
                    continue
                if isinstance(value, dict) and isinstance(
                    value.get("secret_refs"), dict
                ):
                    # Sidecars keep bare version strings of internal outputs.
                    live.update(str(v) for v in value["secret_refs"].values())
                live.update(refs_in(value, sid))
        plans = self.state / "plans"
        if plans.is_dir():
            for path in plans.glob("*.json"):
                live |= refs_in_text(path.read_text(), sid)
        return live

    # ------------------------------------------------------------------- run
    def _sizes(self) -> dict[str, int]:
        spec = self.runner.spec
        return {
            "catalog": _size(spec.catalog_path),
            "journal": _size(spec.journal_path),
            "secrets": _size(spec.secret_store_path),
            "releases": _size(self.state / "releases"),
        }

    def _backups(self) -> dict[str, int]:
        directory = self.runner.backups
        count = (
            sum(1 for item in directory.iterdir() if item.is_dir())
            if directory.is_dir()
            else 0
        )
        return {"count": count, "bytes": _size(directory)}

    def _files(self, doomed: Iterable[dict[str, str]]) -> list[Path]:
        paths = []
        for item in doomed:
            for path in (
                self.runner._sidecar_path(item["name"]),
                self.runner._discovery_path(item["name"]),
            ):
                if path.exists():
                    paths.append(path)
        return paths

    def _checks(self, executions: set[str]) -> list[Path]:
        """Check reports of executions not in ``executions``."""
        directory = self.state / "checks"
        if not directory.is_dir():
            return []
        return sorted(
            path for path in directory.glob("*.json") if path.stem not in executions
        )

    def run(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Retire what is beyond ``keep``; with ``dry_run`` only report it.

        Raises on an unexpected failure (the caller decides whether that
        fails its command); every step is safe to repeat.
        """
        report: dict[str, Any] = {
            "keep_releases": self.keep,
            "dry_run": dry_run,
            "grace_seconds": self.grace,
        }
        if not self.state.exists():
            return report | {"state": "empty", "kept": [], "retired": []}
        spec = self.runner.spec
        journal = ExecutionJournal(spec.journal_path)
        journal.on_commit = self.runner.checkpoint
        store = SecretVersionStore(spec.secret_store_path)
        try:
            try:
                with journal.exclusive():
                    return self._run(report, journal, store, dry_run=dry_run)
            except ValueError as error:
                if str(error) != "journal is already executing":
                    raise
                return report | {"state": "busy", "kept": [], "retired": []}
        finally:
            journal.close()
            store.close()

    def _run(
        self,
        report: dict[str, Any],
        journal: ExecutionJournal,
        store: SecretVersionStore,
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        from piceli.k8s.release import ReleaseCatalog
        from piceli.k8s.release_runner import _canonical, _write_private

        catalog = self._catalog()
        kept, doomed = self._select(catalog)
        listed = {item["name"] for item in doomed}
        protected = set(kept)
        for item in self._intent():  # a run that stopped midway
            if item["name"] not in listed and item["name"] not in protected:
                doomed.append(item)
                listed.add(item["name"])
        sessions = {item["session_id"] for item in doomed}
        remaining = [i["name"] for i in catalog["releases"] if i["name"] not in listed]
        keep_executions = self.runner._compact_keep(remaining)
        doomed_executions = (
            set(journal.doomed(keep_executions))
            if keep_executions is not None
            else set()
        )
        sizes = store.version_sizes()
        candidates = store.candidates()
        report |= {
            "kept": kept,
            "retired": [item["name"] for item in doomed],
            "sessions": sorted(sessions),
            "backups": self._backups(),
            "bytes_before": self._sizes(),
        }
        deferred = self._deferred()
        if dry_run or deferred:
            live = self._live(
                catalog,
                journal,
                store,
                names=listed,
                sessions=sessions,
                executions=doomed_executions,
            )
            unnamed = set(sizes) - live
            limit = self.now.timestamp() - self.grace
            ready = {
                v
                for v in unnamed
                if v in candidates
                and datetime.fromisoformat(candidates[v]).timestamp() <= limit
            }
            files = self._files(doomed) + self._checks(
                journal.execution_ids() - doomed_executions
            )
            return report | {
                "state": "deferred" if deferred else "dry-run",
                **({"reason": deferred} if deferred else {}),
                "files": [str(path.relative_to(self.state)) for path in files],
                "executions": {
                    "count": len(doomed_executions),
                    "ids": sorted(doomed_executions),
                },
                "secret_versions": {
                    "total": len(sizes),
                    "unreferenced": len(unnamed),
                    "unreferenced_bytes": sum(sizes[v] for v in unnamed),
                    "removable_now": len(ready),
                    "removable_now_bytes": sum(sizes[v] for v in ready),
                },
            }
        removed_files: list[str] = []
        intent = self.state / INTENT
        if doomed:
            _write_private(
                intent,
                _canonical(
                    {
                        "schema_version": 1,
                        "at": self.now.isoformat(),
                        "releases": doomed,
                    }
                )
                + "\n",
            )
            ReleaseCatalog(self.runner.spec.catalog_path).remove(listed)
            journal.forget_sessions(sessions)
            store.forget_sessions(sessions)
            for path in self._files(doomed):
                path.unlink(missing_ok=True)
                removed_files.append(str(path.relative_to(self.state)))
        compacted = (
            journal.compact(keep_executions)
            if keep_executions is not None
            else {"removed": 0}
        )
        for path in self._checks(journal.execution_ids()):
            path.unlink(missing_ok=True)
            removed_files.append(str(path.relative_to(self.state)))
        live = self._live(self._catalog(), journal, store)
        swept = store.sweep(live, grace_seconds=self.grace, now=self.now)
        intent.unlink(missing_ok=True)
        report |= {
            "state": "done",
            "files": removed_files,
            "executions": {"count": compacted["removed"]},
            "secret_versions": {
                "total": len(sizes),
                "removed": swept["removed"],
                "removed_bytes": swept["removed_bytes"],
                "waiting": swept["waiting"],
            },
            "bytes_after": self._sizes(),
        }
        logger.info(
            "release retention: kept %d, retired %d, %d executions and %d "
            "secret versions (%d bytes) removed, %d versions waiting for the "
            "grace period; backups kept: %d (%d bytes)",
            len(kept), len(doomed), compacted["removed"], swept["removed"],
            swept["removed_bytes"], swept["waiting"],
            report["backups"]["count"], report["backups"]["bytes"],
        )  # fmt: skip
        return report


def summary(report: Mapping[str, Any]) -> str:
    """One human line for a retention report."""
    state = report.get("state")
    if state in {"busy", "empty"}:
        return f"retention: {state}"
    secrets = report.get("secret_versions", {})
    backups = report.get("backups", {})
    verb = "would retire" if state in {"dry-run", "deferred"} else "retired"
    line = (
        f"retention (keep {report.get('keep_releases')}): kept "
        f"{len(report.get('kept', []))}, {verb} {len(report.get('retired', []))}"
    )
    if state == "done":
        line += (
            f"; secret versions removed {secrets.get('removed', 0)} "
            f"({secrets.get('removed_bytes', 0)} bytes), waiting "
            f"{secrets.get('waiting', 0)}"
        )
    elif secrets:
        line += (
            f"; secret versions unreferenced {secrets.get('unreferenced', 0)} "
            f"({secrets.get('unreferenced_bytes', 0)} bytes), removable now "
            f"{secrets.get('removable_now', 0)}"
        )
    line += (
        f"; backups kept {backups.get('count', 0)} ({backups.get('bytes', 0)} bytes)"
    )
    if state == "deferred":
        line += f"; deferred: {report.get('reason')}"
    return line
