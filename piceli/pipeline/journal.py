"""The durable journal of ``piceli deploy`` runs.

One private JSON file per run under ``<state_dir>/runs/``, rewritten
atomically (``fsync`` + rename) at every stage change, so an interrupted run
can be resumed at the stage that did not finish. A run records the approved
combined plan hash, each stage's state, content identity and outputs
(receipt paths, digests, release names), never secret values.

A lock file serializes runs that share a state directory.

Importing this module is side-effect free.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from piceli.pipeline.errors import PipelineError
from piceli.pipeline.model import STAGES

RUN_SCHEMA = "piceli.deploy-run.v1"
#: Run states that ended; any other state can be resumed.
FINISHED = frozenset({"ready", "rolled-back", "stopped"})


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def write_private(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text`` (mode 0600, fsynced)."""
    _private_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class Run:
    """One journaled run; every mutation is persisted before it returns."""

    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data

    @property
    def run_id(self) -> str:
        return str(self.data["run_id"])

    @property
    def state(self) -> str:
        return str(self.data["state"])

    def stage(self, name: str) -> dict[str, Any]:
        return dict(self.data["stages"][name])

    def save(self) -> None:
        self.data["updated_at"] = now()
        write_private(self.path, json.dumps(self.data, sort_keys=True, indent=2) + "\n")

    def set_stage(self, name: str, **values: Any) -> dict[str, Any]:
        entry = self.data["stages"][name]
        entry.update(values)
        if values.get("state") == "running":
            entry["started_at"] = now()
        elif values.get("state") in {"done", "skipped", "failed", "rejected"}:
            entry["finished_at"] = now()
        self.save()
        return dict(entry)

    def set_state(self, state: str, **values: Any) -> None:
        self.data["state"] = state
        self.data.update(values)
        self.save()

    def output(self, name: str) -> dict[str, Any]:
        return dict(self.data["stages"][name].get("output") or {})


class Journal:
    """The runs of one state directory."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.directory = state_dir / "runs"

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the state directory's run lock (refused when another run holds it)."""
        _private_dir(self.state_dir)
        handle: IO[str] = open(self.state_dir / "deploy.lock", "a+")  # noqa: SIM115
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PipelineError(
                    "pipeline-locked",
                    "another piceli deploy run is using this state directory",
                ) from None
            yield
        finally:
            handle.close()

    def create(
        self,
        *,
        pipeline: Mapping[str, Any],
        combined_hash: str,
        until: str,
        approval: str,
        plan: Mapping[str, Any],
        refs: Mapping[str, Any] | None = None,
    ) -> Run:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        run_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
        data = {
            "schema": RUN_SCHEMA,
            "run_id": run_id,
            "created_at": now(),
            "state": "running",
            "pipeline": dict(pipeline),
            "combined_hash": combined_hash,
            "approval": approval,
            "until": until,
            "plan": dict(plan),
            "stages": {name: {"state": "pending"} for name in STAGES},
        }
        if refs:
            # ``--ref``: source → {ref, commit}; --resume re-opens these commits.
            data["refs"] = {name: dict(value) for name, value in refs.items()}
        run = Run(self.directory / f"{run_id}.json", data)
        run.save()
        return run

    def runs(self) -> list[Run]:
        """Every run, oldest first."""
        if not self.directory.is_dir():
            return []
        result = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("schema") == RUN_SCHEMA:
                result.append(Run(path, data))
        return result

    def latest(self) -> Run | None:
        runs = self.runs()
        return runs[-1] if runs else None

    def last_verified(self, release: str) -> bool:
        """Whether the newest run that applied or kept ``release`` passed its checks."""
        for run in reversed(self.runs()):
            checks = run.data["stages"].get("checks", {})
            if run.output("plan").get("release") != release:
                continue
            if checks.get("state") in {"done", "skipped"} and checks.get(
                "output", {}
            ).get("passed", False):
                return True
            if checks.get("state") in {"failed", "done"}:
                return False
        return False
