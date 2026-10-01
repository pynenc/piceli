"""A machine-wide lock for heavy work, with a receipt per run.

Several agents or processes on one machine may start builds, test gates or
clusters at once. :class:`HeavyLock` serializes them: an OS-level ``flock`` on
a lock file in a per-user state directory, so a holder that crashes (even
``SIGKILL``) releases it by itself. There is no marker directory to clean up.

State directory (owner-only): ``$PICELI_HEAVY_DIR``, else
``$XDG_STATE_HOME/piceli/heavy``, else ``~/.local/state/piceli/heavy``.

``heavy.lock``
    The lock file (its content is not used to decide anything).
``holder.json``
    Who holds the lock now: command, pid, start time, cwd. Never environment
    variables. Removed on release.
``receipts/``
    One ``<timestamp>-<pid>.json`` per finished run, bounded to
    :data:`RECEIPT_KEEP` (oldest removed first).

:func:`run` runs a command as a child, forwarding ``SIGINT``, ``SIGTERM`` and
``SIGHUP`` to its process group and preserving its exit status. :func:`section`
takes the lock around Piceli's own heavy operations (host builds, Docker
``Build.spec`` runs) when ``PICELI_HEAVY_LOCK=1``. A child started by
:func:`run` knows it holds the lock, so a nested Piceli build does not wait for
its own parent.

Importing this module is side-effect free.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import resource
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RECEIPT_SCHEMA = "piceli.heavy-receipt.v1"
STATUS_SCHEMA = "piceli.heavy-status.v1"
#: Set to ``1`` to make Piceli's own builds take the lock.
ENABLE_ENV = "PICELI_HEAVY_LOCK"
#: Overrides the state directory.
DIR_ENV = "PICELI_HEAVY_DIR"
#: Exported to a child of :func:`run`: the pid that holds the lock for it.
HELD_ENV = "PICELI_HEAVY_HELD_BY"
#: Receipts kept; older ones are removed after each run.
RECEIPT_KEEP = 100
#: Default ``--wait`` (seconds) of the CLI.
DEFAULT_WAIT = 3600.0
POLL_SECONDS = 0.2
ANNOUNCE_SECONDS = 30.0

_SECRET_ARG = re.compile(
    r"(?i)^(--?[\w-]*(?:token|secret|passw(?:or)?d|key|credential)[\w-]*)(=|\s).*$"
)


class HeavyError(Exception):
    """A refusal with a registered error code (``code``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def enabled(environ: dict[str, str] | None = None) -> bool:
    """Whether Piceli's own heavy operations take the lock (opt-in)."""
    value = (environ if environ is not None else os.environ).get(ENABLE_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def default_dir() -> Path:
    """The per-user state directory (not created here)."""
    explicit = os.environ.get(DIR_ENV)
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "piceli" / "heavy"


def redact(argv: Sequence[str]) -> list[str]:
    """``argv`` with the value of secret-looking options replaced by ``***``."""
    result: list[str] = []
    hide_next = False
    for arg in argv:
        if hide_next:
            result.append("***")
            hide_next = False
            continue
        match = _SECRET_ARG.match(arg)
        if match:
            result.append(f"{match.group(1)}=***")
        elif re.fullmatch(
            r"(?i)--?[\w-]*(token|secret|passw(or)?d|key|credential)[\w-]*", arg
        ):
            result.append(arg)
            hide_next = True
        else:
            result.append(arg)
    return result


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    temporary.write_text(json.dumps(value, sort_keys=True))
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _git(cwd: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={"PATH": os.environ.get("PATH", os.defpath), "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def sources(cwd: Path) -> dict[str, Any] | None:
    """The git commit and whether the tree is dirty, or ``None`` outside a repository."""
    commit = _git(cwd, "rev-parse", "HEAD")
    if commit is None:
        return None
    dirty = _git(cwd, "status", "--porcelain")
    return {"commit": commit, "dirty": bool(dirty)}


def _peak_children_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


@dataclass
class Held:
    """A held lock: what was recorded and how long the caller waited."""

    waited_seconds: float
    started_at: str


class HeavyLock:
    """The lock and its state directory."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self.dir = state_dir or default_dir()
        self.lock_path = self.dir / "heavy.lock"
        self.holder_path = self.dir / "holder.json"
        self.receipts_dir = self.dir / "receipts"
        self._fd: int | None = None

    def _prepare(self) -> None:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.receipts_dir.mkdir(mode=0o700, exist_ok=True)

    def holder(self) -> dict[str, Any] | None:
        """Who holds the lock now, or ``None`` when it is free."""
        if not self.lock_path.exists():
            return None
        fd = os.open(self.lock_path, os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return _read_json(self.holder_path) or {"command": None, "pid": None}
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
        finally:
            os.close(fd)

    def acquire(
        self,
        command: Sequence[str],
        *,
        name: str | None = None,
        wait: float | None = DEFAULT_WAIT,
        on_wait: Callable[[dict[str, Any]], None] | None = None,
    ) -> Held:
        """Take the lock, waiting up to ``wait`` seconds (``None``: forever).

        ``on_wait`` receives the holder's record when waiting starts and every
        :data:`ANNOUNCE_SECONDS` after. Raises :class:`HeavyError`
        (``heavy-lock-timeout``) when the wait expires.
        """
        if self._fd is not None:
            raise RuntimeError("the lock is already held by this object")
        self._prepare()
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        began = time.monotonic()
        next_notice = began
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                pass
            now = time.monotonic()
            if wait is not None and now - began >= wait:
                os.close(fd)
                raise HeavyError(
                    "heavy-lock-timeout",
                    f"the heavy-work lock was still held after {wait:g}s",
                )
            if on_wait is not None and now >= next_notice:
                on_wait(_read_json(self.holder_path) or {})
                next_notice = now + ANNOUNCE_SECONDS
            time.sleep(POLL_SECONDS)
        self._fd = fd
        started_at = _now()
        _write_json(
            self.holder_path,
            {
                "command": redact(command),
                "name": name,
                "pid": os.getpid(),
                "started_at": started_at,
                "cwd": str(Path.cwd()),
            },
        )
        return Held(time.monotonic() - began, started_at)

    def release(self) -> None:
        """Release the lock (idempotent)."""
        if self._fd is None:
            return
        try:
            self.holder_path.unlink()
        except FileNotFoundError:
            pass
        os.close(self._fd)  # closing the descriptor drops the flock
        self._fd = None

    def recent_receipts(self, limit: int = 10) -> list[dict[str, Any]]:
        """The newest ``limit`` receipts, newest first."""
        try:
            names = sorted(
                (n for n in os.listdir(self.receipts_dir) if n.endswith(".json")),
                reverse=True,
            )
        except FileNotFoundError:
            return []
        found = (_read_json(self.receipts_dir / name) for name in names[:limit])
        return [item for item in found if item is not None]

    def write_receipt(self, receipt: dict[str, Any]) -> Path:
        """Store ``receipt`` and prune to :data:`RECEIPT_KEEP`."""
        self._prepare()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        path = self.receipts_dir / f"{stamp}-{os.getpid()}.json"
        _write_json(path, receipt)
        names = sorted(n for n in os.listdir(self.receipts_dir) if n.endswith(".json"))
        for old in names[:-RECEIPT_KEEP]:
            try:
                (self.receipts_dir / old).unlink()
            except FileNotFoundError:
                pass
        return path

    def status(self, limit: int = 10) -> dict[str, Any]:
        """The ``piceli heavy status`` document."""
        holder = self.holder()
        return {
            "schema": STATUS_SCHEMA,
            "state": "held" if holder is not None else "free",
            "holder": holder,
            "receipts": self.recent_receipts(limit),
        }


def _receipt(
    command: Sequence[str],
    cwd: Path,
    held: Held,
    started: float,
    exit_code: int | None,
    *,
    kind: str,
    peak_bytes: int | None,
) -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "kind": kind,
        "command": redact(command),
        "cwd": str(cwd),
        "sources": sources(cwd),
        "pid": os.getpid(),
        "started_at": held.started_at,
        "finished_at": _now(),
        "waited_seconds": round(held.waited_seconds, 3),
        "duration_seconds": round(time.monotonic() - started, 3),
        "exit_code": exit_code,
        "peak_memory_bytes": peak_bytes,
    }


def _exit_status(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def run(
    command: Sequence[str],
    *,
    name: str | None = None,
    wait: float | None = DEFAULT_WAIT,
    state_dir: Path | None = None,
    on_wait: Callable[[dict[str, Any]], None] | None = None,
    stdout: int | None = None,
) -> dict[str, Any]:
    """Run ``command`` under the lock; return its receipt (``exit_code`` preserved).

    ``name`` labels the run in the holder record and receipt. The child gets
    its own process group; ``SIGINT``, ``SIGTERM`` and ``SIGHUP`` sent to this
    process are forwarded to that group. A child killed by signal ``N`` has
    exit code ``128 + N``. Peak memory is the largest resident set of any
    process the child tree finished (``getrusage(RUSAGE_CHILDREN)``).
    Raises :class:`HeavyError` (``heavy-lock-timeout``, ``heavy-command-missing``,
    ``heavy-command-empty``).
    """
    if not command:
        raise HeavyError("heavy-command-empty", "no command given")
    lock = HeavyLock(state_dir)
    held = lock.acquire(command, name=name, wait=wait, on_wait=on_wait)
    cwd = Path.cwd()
    started = time.monotonic()
    proc: subprocess.Popen[bytes] | None = None
    received: list[int] = []

    def forward(signum: int, _frame: Any) -> None:
        received.append(signum)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signum)
            except (ProcessLookupError, PermissionError):
                pass

    forwarded = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {sig: signal.signal(sig, forward) for sig in forwarded}
    before = _peak_children_bytes()
    try:
        try:
            proc = subprocess.Popen(
                list(command),
                cwd=cwd,
                env={**os.environ, HELD_ENV: str(os.getpid())},
                start_new_session=True,
                stdout=stdout,
            )
        except OSError:
            raise HeavyError(
                "heavy-command-missing", "the command could not be started"
            ) from None
        for signum in received:  # a signal that arrived before the child existed
            os.killpg(proc.pid, signum)
        while True:
            try:
                returncode = proc.wait()
                break
            except KeyboardInterrupt:  # forwarded by `forward`; keep waiting
                continue
        exit_code = _exit_status(returncode)
        peak = _peak_children_bytes()
        receipt = _receipt(
            command,
            cwd,
            held,
            started,
            exit_code,
            kind="run",
            peak_bytes=peak if peak >= before else before,
        )
        if name:
            receipt["name"] = name
        lock.write_receipt(receipt)
        return receipt
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        lock.release()


@contextmanager
def section(label: str, *, state_dir: Path | None = None) -> Iterator[None]:
    """Hold the lock around an in-process heavy operation, when enabled.

    Does nothing unless ``PICELI_HEAVY_LOCK=1`` (or the process is itself a
    child of a :func:`run` that holds the lock). Waits without limit, telling
    stderr who holds it. Writes a receipt (no peak memory: the work is
    in-process).
    """
    if not enabled() or os.environ.get(HELD_ENV):
        yield
        return
    lock = HeavyLock(state_dir)
    held = lock.acquire(
        [f"piceli {label}"], wait=None, on_wait=lambda h: _announce(h, sys.stderr)
    )
    started = time.monotonic()
    code: int | None = None
    try:
        yield
        code = 0
    except BaseException:
        code = 1
        raise
    finally:
        try:
            lock.write_receipt(
                _receipt(
                    [f"piceli {label}"],
                    Path.cwd(),
                    held,
                    started,
                    code,
                    kind="section",
                    peak_bytes=None,
                )
            )
        finally:
            lock.release()


def describe_holder(holder: dict[str, Any]) -> str:
    """One human line naming a holder record (command, pid, start time)."""
    command = holder.get("command")
    text = " ".join(command) if isinstance(command, list) else "unknown command"
    return f"{text} (pid {holder.get('pid', '?')}, started {holder.get('started_at', '?')})"


def _announce(holder: dict[str, Any], stream: Any) -> None:
    stream.write(f"waiting for the heavy-work lock held by {describe_holder(holder)}\n")
    stream.flush()
