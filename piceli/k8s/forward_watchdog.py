"""Stop the ``kubectl port-forward`` children of a Piceli process when it dies.

``piceli access`` starts each ``kubectl port-forward`` in a new session (its
own process group), so Ctrl-C in the terminal never reaches it and Piceli
stops it itself on the way out. A Piceli process killed with ``SIGKILL`` (or
crashing) cannot: its forwards kept their ports until ``piceli access stop
--stale``. Linux has ``PR_SET_PDEATHSIG`` for a direct child, macOS has no
equivalent, and neither reaches a child in another session; so both use the
same watchdog:

- :class:`ParentWatchdog` starts one small helper process (the Python
  interpreter running :data:`WATCHDOG_SOURCE`, no Piceli import) in the
  caller's process group, connected by a pipe whose write end only the
  caller holds;
- the caller tells it the process-group leader of every forward it starts
  (``+PID``) and stops (``-PID``);
- when the caller dies, however it dies, the kernel closes the pipe: the
  helper reads end-of-file, sends ``SIGTERM`` to every group still
  registered, ``SIGKILL`` to any left after a grace period, and exits.

The helper ignores ``SIGINT``, ``SIGHUP`` and ``SIGTERM`` (the caller handles
those and stops its forwards itself); it dies with the caller's process group
on ``SIGKILL`` of the whole group. It never signals a group it was not told
about, nor group 0 or 1, nor its own.

Importing this module is side-effect free.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from typing import IO

__all__ = ["WATCHDOG_SOURCE", "ParentWatchdog"]

#: The helper's program (``python -c``): stdlib only, so it starts fast.
WATCHDOG_SOURCE = r"""
import os, signal, sys, time
for name in ("SIGINT", "SIGHUP", "SIGTERM"):
    signal.signal(getattr(signal, name), signal.SIG_IGN)
groups = set()
own = os.getpgrp()
for line in sys.stdin:
    line = line.strip()
    if len(line) < 2 or not line[1:].isdigit():
        continue
    pid = int(line[1:])
    if pid <= 1 or pid == own:
        continue
    if line[0] == "+":
        groups.add(pid)
    elif line[0] == "-":
        groups.discard(pid)
def alive(pid):
    try:
        os.killpg(pid, 0)
    except OSError:
        return False
    return True
for pid in sorted(groups):
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        pass
deadline = time.monotonic() + float(sys.argv[1] if len(sys.argv) > 1 else 3)
while time.monotonic() < deadline and any(alive(pid) for pid in groups):
    time.sleep(0.05)
for pid in sorted(groups):
    if alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
"""


class ParentWatchdog:
    """One helper process that stops registered forwards when its parent dies.

    :param grace: Seconds between ``SIGTERM`` and ``SIGKILL``.
    :param python: The interpreter that runs the helper.
    """

    def __init__(self, *, grace: float = 3.0, python: str = sys.executable) -> None:
        self._grace = grace
        self._python = python
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    @property
    def pid(self) -> int | None:
        """The helper's pid (``None`` before the first :meth:`add`)."""
        return None if self._process is None else self._process.pid

    def _pipe(self) -> IO[str] | None:
        if self._process is None:
            try:
                self._process = subprocess.Popen(
                    [self._python, "-c", WATCHDOG_SOURCE, str(self._grace)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    close_fds=True,
                )
            except OSError:
                return None
        if self._process.poll() is not None:
            return None
        return self._process.stdin

    def _send(self, line: str) -> None:
        with self._lock:
            pipe = self._pipe()
            if pipe is None:
                return
            try:
                pipe.write(line + "\n")
                pipe.flush()
            except (OSError, ValueError):
                pass  # the helper is gone: Piceli still stops its forwards itself

    def add(self, pid: int) -> None:
        """Watch the process group led by ``pid`` (a forward just started)."""
        if type(pid) is int and pid > 1:
            self._send(f"+{pid}")

    def remove(self, pid: int) -> None:
        """Stop watching ``pid`` (Piceli stopped that forward itself)."""
        if type(pid) is int and pid > 1 and self._process is not None:
            self._send(f"-{pid}")

    def close(self) -> None:
        """End the helper after the caller stopped its forwards itself.

        Closing the pipe is what the helper waits for; with nothing left
        registered it exits without signalling anything.
        """
        with self._lock:
            process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=self._grace + 2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
