"""A private record of the forward processes a server started, to reap them after a crash.

:class:`~piceli.k8s.observe.ForwardSupervisor` starts every ``kubectl
port-forward`` in a fresh process group, so a forward outlives a server that
is killed with ``SIGKILL`` (or crashes) before it can stop its children. An
:class:`OwnedProcessRegistry` writes one small file per started child into a
private directory: the child's pid and a fingerprint of that process (its
start time and command line), plus the same for the owning server. At the
next start, :meth:`OwnedProcessRegistry.reap_orphans` stops a recorded child
only when its owner is gone **and** the pid still names the very process that
was recorded. A pid reused by another process, or a child whose owner is
still running, is never signalled. Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_MAX_ENTRIES = 4096
_MAX_ENTRY_BYTES = 1024


def process_identity(pid: int, *, proc: Path = Path("/proc")) -> str | None:
    """A fingerprint of the live process ``pid`` (start time and command), or ``None``.

    Two different processes that reuse a pid have different fingerprints. The
    value is a digest: it never contains the command line itself.
    """
    if pid <= 0:
        return None
    raw: bytes | None = None
    if sys.platform.startswith("linux") and (proc / str(pid)).is_dir():
        try:
            stat = (proc / str(pid) / "stat").read_text()
            command = (proc / str(pid) / "cmdline").read_bytes()
        except OSError:
            return None
        # "pid (comm) state ppid ..."; field 22 (starttime) is the 20th after comm.
        fields = stat.rsplit(")", 1)[-1].split()
        if len(fields) < 20:
            return None
        raw = fields[19].encode() + b"\0" + command
    else:
        ps = shutil.which("ps")
        if ps is None:
            return None
        try:
            result = subprocess.run(
                [ps, "-ww", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        raw = result.stdout.strip()
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class _Entry:
    pid: int
    identity: str
    owner_pid: int
    owner_identity: str


class OwnedProcessRegistry:
    """Owned child processes of one server, recorded in a private directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._owner_pid = os.getpid()
        self._owner_identity: str | None = None

    def _prepare(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)

    def _path(self, pid: int) -> Path:
        return self.directory / f"{pid}.json"

    def record(self, pid: int) -> None:
        """Record a child this process just started (best effort, never raises)."""
        try:
            identity = process_identity(pid)
            if self._owner_identity is None:
                self._owner_identity = process_identity(self._owner_pid)
            if identity is None or self._owner_identity is None:
                return
            self._prepare()
            data = json.dumps(
                {
                    "pid": pid,
                    "identity": identity,
                    "owner_pid": self._owner_pid,
                    "owner_identity": self._owner_identity,
                }
            ).encode()
            temporary = self.directory / f".{pid}.{os.getpid()}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.write(descriptor, data)
            finally:
                os.close(descriptor)
            os.replace(temporary, self._path(pid))
        except OSError:
            return

    def forget(self, pid: int) -> None:
        """Drop the record of a child that this process stopped or saw exit."""
        try:
            self._path(pid).unlink(missing_ok=True)
        except OSError:
            return

    def _entries(self) -> list[tuple[Path, _Entry | None]]:
        try:
            paths = sorted(self.directory.glob("*.json"))[:_MAX_ENTRIES]
        except OSError:
            return []
        entries: list[tuple[Path, _Entry | None]] = []
        for path in paths:
            entry: _Entry | None = None
            try:
                if not path.is_symlink() and path.stat().st_size <= _MAX_ENTRY_BYTES:
                    value = json.loads(path.read_bytes())
                    entry = _Entry(
                        pid=int(value["pid"]),
                        identity=str(value["identity"]),
                        owner_pid=int(value["owner_pid"]),
                        owner_identity=str(value["owner_identity"]),
                    )
                    if path.name != f"{entry.pid}.json":
                        entry = None
            except (OSError, ValueError, KeyError, TypeError):
                entry = None
            entries.append((path, entry))
        return entries

    def reap_orphans(self, *, timeout: float = 3.0) -> tuple[int, ...]:
        """Stop recorded children whose owner is gone; return the pids stopped.

        A child is signalled only when its pid still has the recorded
        fingerprint. Entries of a live owner are left alone; entries of exited
        or reused pids are removed without signalling anything.
        """
        if not self.directory.is_dir():
            return ()
        reaped: list[int] = []
        for path, entry in self._entries():
            if entry is None:
                path.unlink(missing_ok=True)
                continue
            if process_identity(entry.pid) != entry.identity:
                path.unlink(missing_ok=True)  # exited, or the pid was reused
                continue
            if (
                entry.owner_pid == self._owner_pid
                or process_identity(entry.owner_pid) == entry.owner_identity
            ):
                continue  # its server is still running
            if _terminate(entry, timeout):
                reaped.append(entry.pid)
                path.unlink(missing_ok=True)
        return tuple(reaped)


def _terminate(entry: _Entry, timeout: float) -> bool:
    """Stop one verified orphan (its process group when it leads one)."""

    def send(signum: int) -> None:
        try:
            if hasattr(os, "getpgid") and os.getpgid(entry.pid) == entry.pid:
                os.killpg(entry.pid, signum)
            else:
                os.kill(entry.pid, signum)
        except (ProcessLookupError, PermissionError):
            pass

    for signum in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        send(signum)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process_identity(entry.pid) != entry.identity:
                return True
            time.sleep(0.05)
    return process_identity(entry.pid) != entry.identity
