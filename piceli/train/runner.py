"""Where a train's commands run, and what it tells observers.

Two seams, so later work can change *where* and *who listens* without
touching the train's logic:

- :class:`Runner` runs one :class:`Command` and returns a :class:`Completed`
  (exit status, start, duration, host, output). :class:`LocalRunner` runs it
  here (host ``local``), in one of this machine's slots
  (:mod:`piceli.train.local`); :class:`piceli.train.hosts.HostRunner`
  spreads commands over the train's build hosts.
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
        goes to the log. Such a command always runs here, in no slot.
    :param hosts: Where it may run, in order of preference (empty: the
        runner's default).
    :param cargo: It runs cargo: a local run gets the shared target
        directory and the compiler cache.
    :param outputs: Paths (relative to ``cwd``; ``target/...`` is the cargo
        target directory) the command produces: after it, they are in
        ``cwd`` wherever it ran.
    :param step_env: The variables the train and the step set (a subset of
        ``env``): what a remote host gets, never the caller's environment.
    """

    argv: Sequence[str]
    cwd: Path
    log: Path
    env: Mapping[str, str] = field(default_factory=dict)
    timeout: float | None = None
    stdout: bool = False
    hosts: tuple[str, ...] = ()
    cargo: bool = False
    outputs: tuple[str, ...] = ()
    step_env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Completed:
    """What one :class:`Command` did.

    ``exit_code`` is ``None`` when it was stopped at its timeout or could not
    start (``error`` says which: ``timeout``, ``not-started``, or
    ``unavailable`` when its host turned out down). ``notes`` say how it was
    placed (a host skipped, the target directory used).
    """

    exit_code: int | None
    started: float
    duration: float
    host: str
    output: str
    error: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Runner(Protocol):
    """Runs a train's commands somewhere; ``host`` names where."""

    host: str

    def run(self, command: Command) -> Completed: ...


class LocalRunner:
    """Run commands on this machine, each in its own process group.

    :param slots: This machine's slots (:class:`piceli.train.local.Slots`):
        a command waits for a free one, so the machine keeps its limit across
        every train on it. ``None``: no limit.
    :param cargo: The environment of a cargo command in slot ``n``
        (:class:`piceli.train.local.CargoCache`): the shared target directory
        and the compiler cache. ``None``: the caller's.
    """

    host = "local"

    def __init__(self, slots: Any = None, cargo: Any = None) -> None:
        self.slots = slots
        self.cargo = cargo

    def state(self) -> tuple[bool, int, str]:
        """``(up, free slots, detail)``."""
        free = 1 if self.slots is None else self.slots.free()
        return True, free, ""

    def run(self, command: Command) -> Completed:
        if command.stdout or self.slots is None:
            return self._placed(command, None)
        with self.slots.take() as slot:
            return self._placed(command, slot)

    def _placed(self, command: Command, slot: int | None) -> Completed:
        notes: list[str] = []
        target: Path | None = None
        if command.cargo and self.cargo is not None and not command.stdout:
            extra, target = self.cargo.environment(slot or 0, command.env, command.cwd)
            if extra:
                command = Command(
                    command.argv, command.cwd, command.log,
                    {**command.env, **extra}, command.timeout, command.stdout,
                    command.hosts, command.cargo, command.outputs, command.step_env,
                )  # fmt: skip
                notes += [f"{key}={value}" for key, value in sorted(extra.items())]
        done = self._run(command)
        if target is not None and command.outputs:
            from piceli.train.local import bring_outputs

            bring_outputs(target, command.cwd, command.outputs)
        if notes:
            done = Completed(
                done.exit_code, done.started, done.duration, done.host,
                done.output, done.error, (*done.notes, *notes),
            )  # fmt: skip
        return done

    def _run(self, command: Command) -> Completed:
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
