"""Find which local process listens on a loopback TCP port, without extra packages.

Used to explain a port conflict ("port 18080 is held by pid 4242:
``kubectl port-forward service/web 18080:3000``") instead of silently waiting
for the port. Linux reads ``/proc``; other systems (macOS) ask
``lsof -nP -iTCP:<port> -sTCP:LISTEN`` and ``ps`` when they are installed.
Every lookup degrades to ``None`` when the owner cannot be determined (no
permission, no tool, a race with the process exiting).

The command line of another process is shown to the local user only; it is
truncated to :data:`MAX_COMMAND` characters. Importing this module is
side-effect free.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MAX_COMMAND = 300
_LISTEN = "0A"  # TCP_LISTEN in /proc/net/tcp
_TIMEOUT = 3.0


@dataclass(frozen=True)
class ProcessInfo:
    """One local process: its id and command line (possibly truncated)."""

    pid: int
    command: str | None = None


@dataclass(frozen=True)
class PortOwner:
    """The process listening on a port, and its parent (often the supervisor)."""

    port: int
    pid: int
    command: str | None = None
    parent: ProcessInfo | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if self.parent is None:
            value["parent"] = None
        return value

    def describe(self) -> str:
        """One line for humans: ``pid 42 (kubectl port-forward …)``."""
        text = f"pid {self.pid}"
        if self.command:
            text += f" ({self.command})"
        if self.parent is not None:
            text += f", started by pid {self.parent.pid}"
            if self.parent.command:
                text += f" ({self.parent.command})"
        return text


def _truncate(command: str | None) -> str | None:
    if not command:
        return None
    command = " ".join(command.split())
    if len(command) > MAX_COMMAND:
        return command[: MAX_COMMAND - 1] + "…"
    return command


# ---------------------------------------------------------------------- linux


def _proc_inodes(port: int, proc: Path) -> set[str]:
    inodes: set[str] = set()
    for table in ("tcp", "tcp6"):
        try:
            lines = (proc / "net" / table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != _LISTEN:
                continue
            try:
                local_port = int(fields[1].rsplit(":", 1)[1], 16)
            except (IndexError, ValueError):
                continue
            if local_port == port:
                inodes.add(fields[9])
    return inodes


def _proc_command(pid: int, proc: Path) -> str | None:
    try:
        raw = (proc / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return _truncate(raw.replace(b"\0", b" ").decode(errors="replace").strip())


def _proc_parent(pid: int, proc: Path) -> int | None:
    try:
        stat = (proc / str(pid) / "stat").read_text()
    except OSError:
        return None
    # "pid (comm) state ppid ..."; comm may contain spaces and parentheses.
    fields = stat.rsplit(")", 1)[-1].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return None


def _proc_owner(port: int, proc: Path) -> tuple[int, str | None, int | None] | None:
    inodes = _proc_inodes(port, proc)
    if not inodes:
        return None
    targets = {f"socket:[{inode}]" for inode in inodes}
    try:
        pids = sorted(int(item.name) for item in proc.iterdir() if item.name.isdigit())
    except OSError:
        return None
    for pid in pids:
        try:
            descriptors = list((proc / str(pid) / "fd").iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                if os.readlink(descriptor) in targets:
                    return pid, _proc_command(pid, proc), _proc_parent(pid, proc)
            except OSError:
                continue
    return None


# ------------------------------------------------------------------ lsof / ps


def _run(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout


def _ps(pid: int) -> tuple[str | None, int | None]:
    ps = shutil.which("ps")
    if ps is None:
        return None, None
    output = _run([ps, "-o", "ppid=", "-o", "command=", "-p", str(pid)])
    if not output or not output.strip():
        return None, None
    ppid_text, _, command = output.strip().partition(" ")
    try:
        ppid: int | None = int(ppid_text)
    except ValueError:
        ppid = None
    return _truncate(command.strip()), ppid


def _lsof_owner(port: int) -> tuple[int, str | None, int | None] | None:
    lsof = shutil.which("lsof") or (
        "/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else None
    )
    if lsof is None:
        return None
    output = _run([lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"])
    if not output:
        return None
    pid: int | None = None
    name: str | None = None
    for line in output.splitlines():
        if line.startswith("p") and pid is None:
            try:
                pid = int(line[1:])
            except ValueError:
                return None
        elif line.startswith("c") and pid is not None and name is None:
            name = line[1:]
    if pid is None:
        return None
    command, ppid = _ps(pid)
    return pid, command or _truncate(name), ppid


# ----------------------------------------------------------------- public API


def port_owner(port: int, *, proc: Path = Path("/proc")) -> PortOwner | None:
    """The process listening on TCP ``port`` (any local address), or ``None``.

    Never raises: an owner that cannot be determined is ``None``.
    """
    try:
        if sys.platform.startswith("linux") and (proc / "net" / "tcp").exists():
            found = _proc_owner(port, proc)
            parent_command = _proc_command
        else:
            found = _lsof_owner(port)
            parent_command = None
        if found is None:
            return None
        pid, command, ppid = found
        parent = None
        if ppid is not None and ppid > 1:
            if parent_command is not None:
                parent = ProcessInfo(ppid, parent_command(ppid, proc))
            else:
                parent = ProcessInfo(ppid, _ps(ppid)[0])
        return PortOwner(port=port, pid=pid, command=command, parent=parent)
    except Exception:  # an explanation must never break the caller
        return None
