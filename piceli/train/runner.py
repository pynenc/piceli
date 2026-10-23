"""Where a train's commands run, and what it tells observers.

Two seams, so later work can change *where* and *who listens* without
touching the train's logic:

- :class:`Runner` runs one :class:`Command` and returns a :class:`Completed`
  (exit status, start, duration, host, output). :class:`LocalRunner` runs it
  here (host ``local``). A remote build host implements the same protocol.
- An :data:`EventSink` receives one dict per event (``run-started``,
  ``trial-started``, ``step-started``, ``step-finished``, ``trial-finished``,
  ``branch-joined``, ``branch-ejected``, ``promoted``, ``published``,
  ``pushed``, ``run-finished``), each with ``event`` and ``at`` (UTC, ISO
  8601). Status views, OpenTelemetry spans and notifications subscribe here.

Importing this module is side-effect free.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from piceli.process_group import signal_group

EventSink = Callable[[Mapping[str, Any]], None]

#: Seconds a stopped command gets between ``SIGTERM`` and ``SIGKILL``.
KILL_GRACE = 10.0


def now_iso(at: float | None = None) -> str:
    """UTC time as ``YYYY-MM-DDTHH:MM:SS.ffffffZ``."""
    moment = datetime.fromtimestamp(time.time() if at is None else at, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def no_events(_event: Mapping[str, Any]) -> None:
    """The default :data:`EventSink`: nobody listens."""


def fan_out(*sinks: EventSink) -> EventSink:
    """One sink that calls each of ``sinks`` in turn."""

    def sink(event: Mapping[str, Any]) -> None:
        for each in sinks:
            each(event)

    return sink


@dataclass(frozen=True)
class Command:
    """One process to run in a checkout.

    :param argv: The program and its arguments.
    :param cwd: The checkout (the program sees the merged tree).
    :param log: The step's log file; output is appended to it.
    :param env: The complete environment of the process.
    :param timeout: Seconds before the process group is stopped.
    :param stdout: Return standard output alone in :attr:`Completed.output`
        (machine output such as ``cargo metadata``); standard error still
        goes to the log.
    """

    argv: Sequence[str]
    cwd: Path
    log: Path
    env: Mapping[str, str] = field(default_factory=dict)
    timeout: float | None = None
    stdout: bool = False


@dataclass(frozen=True)
class Completed:
    """What one :class:`Command` did.

    ``exit_code`` is ``None`` when it was stopped at its timeout or could not
    start (``error`` says which).
    """

    exit_code: int | None
    started: float
    duration: float
    host: str
    output: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Runner(Protocol):
    """Runs a train's commands somewhere; ``host`` names where."""

    host: str

    def run(self, command: Command) -> Completed: ...


class LocalRunner:
    """Run commands on this machine, each in its own process group."""

    host = "local"

    def run(self, command: Command) -> Completed:
        command.log.parent.mkdir(parents=True, exist_ok=True)
        started = time.time()
        clock = time.monotonic()
        with command.log.open("a", encoding="utf-8", errors="replace") as log:
            log.write(f"$ {' '.join(command.argv)}  # {now_iso(started)}\n")
            log.flush()
            offset = log.tell()
            try:
                process = subprocess.Popen(
                    list(command.argv),
                    cwd=command.cwd,
                    env=dict(command.env),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE if command.stdout else log,
                    stderr=log,
                    start_new_session=True,
                )
            except OSError as failure:
                log.write(f"cannot start: {type(failure).__name__}: {failure}\n")
                return Completed(
                    None,
                    started,
                    time.monotonic() - clock,
                    self.host,
                    "",
                    error="not-started",
                )
            error: str | None = None
            try:
                captured, _ = process.communicate(timeout=command.timeout)
            except subprocess.TimeoutExpired:
                error = "timeout"
                captured = _stop(process)
            except BaseException:
                _stop(process)
                raise
            duration = time.monotonic() - clock
            if error:
                log.write(f"\nstopped after {duration:.1f} s (timeout)\n")
            log.flush()
        if command.stdout:
            output = (captured or b"").decode("utf-8", errors="replace")
        else:
            with command.log.open("r", encoding="utf-8", errors="replace") as log:
                log.seek(offset)
                output = log.read()
        code = None if error else process.returncode
        return Completed(code, started, duration, self.host, output, error=error)


def _stop(process: subprocess.Popen[bytes]) -> bytes | None:
    """Stop the process group (``SIGTERM``, then ``SIGKILL``); its stdout."""
    try:
        signal_group(process.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        captured, _ = process.communicate(timeout=KILL_GRACE)
        return captured
    except subprocess.TimeoutExpired:
        try:
            signal_group(process.pid, signal.SIGKILL)
        except OSError:
            pass
        captured, _ = process.communicate()
        return captured


def base_environment() -> dict[str, str]:
    """The environment a step starts from: the caller's."""
    return dict(os.environ)
