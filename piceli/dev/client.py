"""One development run from the client's side (``piceli dev run``, 0.18.0).

Pack the sources, create the run's Job, wait until its pod runs, upload the
archive, follow the log, read the wrapper's result, copy artifacts back,
and remove the Job (also when interrupted). The result is
``piceli.dev-run.v1``: the wrapper's result with the client's phases
(``pack``, ``queue``, ``transfer``) added.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import secrets
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from piceli.dev.model import DevBuilds, DevError
from piceli.dev.pack import SourceRequest, pack
from piceli.dev.pod import RESULT_MARKER

RESULT_SCHEMA = "piceli.dev-run.v1"
PRIORITIES = ("round", "agent", "normal")


@dataclass
class RunRequest:
    """What ``piceli dev run`` was asked to do."""

    root: SourceRequest
    command: list[str]
    extras: list[SourceRequest] = field(default_factory=list)
    cwd: str = "."
    profile: str | None = None
    requester: str = "anonymous"
    priority: str = "agent"
    artifacts: list[str] = field(default_factory=list)
    artifacts_dir: Path = Path("piceli-dev-artifacts")
    queue_timeout: float = 3600.0
    keep: bool = False


def new_run_id(now: datetime | None = None) -> str:
    """``<UTC time>-<4 hex>``: sortable, a valid part of a Job name."""
    moment = (now or datetime.now(UTC)).strftime("%Y%m%dt%H%M%S")
    return f"{moment}-{secrets.token_hex(2)}"


def _safe_extract(archive: Path, into: Path) -> list[str]:
    into.mkdir(parents=True, exist_ok=True)
    names = []
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if (
                not member.isfile()
                or member.name.startswith("/")
                or ".." in Path(member.name).parts
            ):
                continue
            tar.extract(member, into, filter="data")
            names.append(str(into / member.name))
    return names


def run(
    port: Any,
    dev: DevBuilds,
    request: RunRequest,
    *,
    say: Callable[[str], None] = lambda _line: None,
    log: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    run_id: str | None = None,
    suspend: bool | None = None,
) -> dict[str, Any]:
    """Run ``request`` on the builder; the ``piceli.dev-run.v1`` result.

    :param log: Receives each line of the run's output (``None``: dropped).
    :raises DevError: refusals before anything runs (``dev-profile-unknown``,
        ``dev-source-invalid``, ``dev-ref-unknown``, ``dev-run-invalid``).
    """
    from piceli.dev.jobs import run_job

    profile = dev.profile(request.profile)
    if not request.command:
        raise DevError("dev-run-invalid", "give the command after --")
    if request.priority not in PRIORITIES:
        raise DevError("dev-run-invalid", f"priority is one of {', '.join(PRIORITIES)}")
    cwd = Path(request.cwd)
    if cwd.is_absolute() or ".." in cwd.parts:
        raise DevError("dev-run-invalid", "--cwd is a path inside the root source")
    run_id = run_id or new_run_id()
    if suspend is None:
        # A live scheduler queues the run and records it; without one the
        # run starts at once and this client removes its Job.
        from piceli.dev.scheduler import alive

        status = getattr(port, "status", lambda: None)()
        suspend = alive(status, time.time())
    began = clock()
    durations: dict[str, float] = {}
    body: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "run": run_id,
        "command": list(request.command),
        "cwd": str(Path(request.root.name) / cwd),
        "profile": profile.name,
        "node": dev.node,
        "requester": request.requester,
        "priority": request.priority,
    }
    with tempfile.TemporaryDirectory(prefix="piceli-dev-") as scratch:
        packed = pack(request.root, request.extras, Path(scratch))
        durations["pack"] = round(clock() - began, 3)
        body["sources"] = packed.sources
        body["upload_bytes"] = packed.bytes
        hint = request.root.ref or f"worktree:{request.root.path.resolve()}"
        spec = {
            "command": list(request.command),
            "cwd": body["cwd"],
            "archive_sha256": packed.sha256,
            "lineage_key": f"{request.root.name}-{profile.name}",
            "lineage_hint": hint,
            "artifacts": list(request.artifacts),
        }
        job = run_job(
            dev,
            profile,
            run=run_id,
            spec=spec,
            requester=request.requester,
            priority=request.priority,
            suspend=suspend,
            annotations={
                "piceli.io/dev-command": json.dumps(request.command)[:1000],
                "piceli.io/dev-sources": json.dumps(
                    {k: v.get("commit") for k, v in packed.sources.items()}
                ),
            },
        )
        port.create(job)
        say(
            f"run {run_id}: {profile.name} on {dev.node}, {packed.bytes} bytes to upload"
        )
        result: dict[str, Any] | None = None
        # With a scheduler the Job stays until it records the run; a run that
        # never got its result (interrupted, failed to start) goes now.
        remove = not suspend
        try:
            step = clock()
            pod = port.wait_started(
                run_id,
                request.queue_timeout,
                lambda state: say(f"run {run_id}: {state}"),
            )
            durations["queue"] = round(clock() - step, 3)
            step = clock()
            port.upload(pod, packed.path)
            durations["transfer"] = round(clock() - step, 3)
            found: list[dict[str, Any]] = []

            def line(text: str) -> None:
                if not text.startswith(RESULT_MARKER):
                    if log is not None:
                        log(text)
                    return
                try:
                    value = json.loads(text[len(RESULT_MARKER) :])
                except ValueError:
                    return
                if value.get("artifacts"):
                    # Now: the pod waits for them before it ends (and its log).
                    archive = Path(scratch) / "artifacts.tar.gz"
                    port.download(pod, archive)
                    value["artifacts"] = _safe_extract(
                        archive, request.artifacts_dir / run_id
                    )
                found.append(value)

            port.follow(pod, line)
            result = found[-1] if found else None
            if result is None:
                reason = port.ended(run_id)
                result = {
                    "state": "cancelled" if reason == "dev-run-cancelled" else "error",
                    "reason": reason,
                    "exit_code": None,
                    "durations": {},
                }
        except KeyboardInterrupt:
            remove = True
            result = {
                "state": "cancelled",
                "reason": "dev-run-cancelled",
                "exit_code": None,
                "durations": {},
            }
        except DevError as error:
            remove = True
            result = {
                "state": "cancelled" if error.code == "dev-run-cancelled" else "error",
                "reason": error.code,
                "message": str(error),
                "exit_code": None,
                "durations": {},
            }
        finally:
            if remove and not request.keep:
                try:
                    port.delete(run_id)
                except Exception:  # the TTL removes it
                    pass
    pod_durations = dict(result.get("durations") or {})
    pod_durations.pop("total", None)
    durations.update(pod_durations)
    durations["total"] = round(clock() - began, 3)
    body.update(
        {k: v for k, v in result.items() if k not in {"schema", "run", "durations"}}
    )
    body["durations"] = durations
    body.setdefault("log_tail", [])
    return body
