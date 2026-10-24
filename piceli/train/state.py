"""A train's history: run records, logs and the flaky-test registry.

Layout under the train's state directory (default
``$XDG_STATE_HOME/piceli/train/<name>``, else
``~/.local/state/piceli/train/<name>``)::

    lock                      held while a run is active; names its run and pid
    runs/<run id>/run.json    the run's result (written after each trial)
    runs/<run id>/events.jsonl  append-only: every event of the run, as it happens
    runs/<run id>/*.log       one log per step
    flaky.jsonl               append-only: one line per flaky observation

Each ``flaky.jsonl`` line carries the test's running totals (``count``,
``first_seen``, ``last_seen``, ``last_tail``), so the last line of a test is
its current state and the file is never rewritten.

Importing this module is side-effect free.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from piceli.train.model import TrainError

RUN_SCHEMA = "piceli.train.v1"
EVENTS = "events.jsonl"
_NAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_RUN = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}")


def default_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "piceli" / "train"


def new_run_id(at: float | None = None) -> str:
    moment = datetime.fromtimestamp(time.time() if at is None else at, UTC)
    return f"{moment.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def train_names(root: Path | None = None) -> list[str]:
    """The trains with a state directory under ``root``, sorted."""
    base = root if root is not None else default_root()
    if not base.is_dir():
        return []
    return sorted(
        path.name
        for path in base.iterdir()
        if path.is_dir() and _NAME.fullmatch(path.name)
    )


def is_run_id(value: str) -> bool:
    return bool(_RUN.fullmatch(value))


class TrainState:
    """The state directory of one train."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def for_train(cls, name: str, state_dir: Path | None = None) -> TrainState:
        return cls((state_dir if state_dir is not None else default_root()) / name)

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def flaky_path(self) -> Path:
        return self.root / "flaky.jsonl"

    def run_dir(self, run_id: str) -> Path:
        path = self.runs / run_id
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    @contextmanager
    def lock(self, run_id: str | None = None) -> Iterator[None]:
        """Hold the train's lock; ``train-busy`` when another run holds it.

        While held, the lock file names the run and the process holding it
        (:meth:`owner` reads it), so a status view can tell a running run
        from one that was killed without taking the lock itself.
        """
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / "lock").open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TrainError(
                    "train-busy", "another run of this train is in progress"
                ) from None
            try:
                handle.seek(0)
                handle.truncate()
                handle.write(
                    json.dumps(
                        {"run": run_id, "pid": os.getpid(), "since": time.time()}
                    )
                )
                handle.flush()
                yield
            finally:
                handle.seek(0)
                handle.truncate()
                handle.flush()
                fcntl.flock(handle, fcntl.LOCK_UN)

    def owner(self) -> dict[str, Any] | None:
        """The run holding the lock (``run``, ``pid``, ``since``), or ``None``
        when no live process holds it. Never takes the lock."""
        try:
            body = json.loads((self.root / "lock").read_text() or "null")
        except (OSError, ValueError):
            return None
        if not isinstance(body, dict) or not isinstance(body.get("pid"), int):
            return None
        try:
            os.kill(body["pid"], 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass  # alive, owned by someone else
        return body

    def events_path(self, run_id: str) -> Path:
        return self.runs / run_id / EVENTS

    def append_event(self, run_id: str, event: Mapping[str, Any]) -> None:
        """Append one event to ``runs/<id>/events.jsonl``."""
        path = self.run_dir(run_id) / EVENTS
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")

    def events(self, run_id: str) -> list[dict[str, Any]]:
        """The run's events, in order (a torn last line is skipped)."""
        try:
            text = self.events_path(run_id).read_text(encoding="utf-8")
        except OSError:
            return []
        found = []
        for line in text.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                found.append(item)
        return found

    def record(self, run_id: str) -> dict[str, Any] | None:
        """``runs/<id>/run.json``, or ``None``."""
        try:
            body = json.loads((self.runs / run_id / "run.json").read_text())
        except (OSError, ValueError):
            return None
        return body if isinstance(body, dict) else None

    def write_record(self, run_id: str, record: Mapping[str, Any]) -> Path:
        """Write ``runs/<id>/run.json`` atomically."""
        path = self.run_dir(run_id) / "run.json"
        partial = path.with_suffix(".json.partial")
        partial.write_text(
            json.dumps(record, indent=2, sort_keys=True, default=str) + "\n"
        )
        os.replace(partial, path)
        return path

    def history(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Run records, newest first."""
        if not self.runs.is_dir():
            return []
        records = []
        for path in sorted(self.runs.glob("*/run.json"), reverse=True)[:limit]:
            try:
                records.append(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue
        return records

    def flaky(self) -> dict[str, dict[str, Any]]:
        """Each flaky test's current totals (its last registry line), by key."""
        current: dict[str, dict[str, Any]] = {}
        if not self.flaky_path.is_file():
            return current
        for line in self.flaky_path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                current[entry["key"]] = entry
        return current

    def record_flakes(
        self, observations: Sequence[Mapping[str, Any]], run_id: str, at: str
    ) -> list[dict[str, Any]]:
        """Append one line per flaky test seen in a run (once per run)."""
        known = self.flaky()
        lines: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in observations:
            key = f"{item['target']} {item['test']}"
            if key in seen:
                continue
            seen.add(key)
            before = known.get(key, {})
            lines.append(
                {
                    "key": key,
                    "test": item["test"],
                    "target": item["target"],
                    "step": item.get("step"),
                    "count": int(before.get("count", 0)) + 1,
                    "first_seen": before.get("first_seen", at),
                    "last_seen": at,
                    "last_run": run_id,
                    "last_branches": list(item.get("branches") or ()),
                    "last_tail": item.get("tail", ""),
                }
            )
        if lines:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.flaky_path.open("a", encoding="utf-8") as handle:
                for line in lines:
                    handle.write(json.dumps(line, sort_keys=True) + "\n")
        return lines
