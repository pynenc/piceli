"""Find which local process listens on a loopback TCP port, without extra packages.

Used to explain a port conflict ("port 18080 is held by pid 4242:
``kubectl port-forward service/web 18080:3000``") instead of silently waiting
for the port. Linux reads ``/proc``; other systems (macOS) ask
``lsof -nP -iTCP:<port> -sTCP:LISTEN`` and ``ps`` when they are installed.
Every lookup degrades to ``None`` when the owner cannot be determined (no
permission, no tool, a race with the process exiting).

:func:`recognise` tells Piceli's own processes for a target (its
``kubectl port-forward`` children, ``piceli access``, ``observe serve``,
``operator serve``) from anything else. Only their command lines are shown;
another process is named by its pid alone. Importing this module is
side-effect free.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterable
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

    def shortened(self) -> PortOwner:
        """The same owner with command lines cut to :data:`MAX_COMMAND`."""
        parent = self.parent
        if parent is not None:
            parent = ProcessInfo(parent.pid, _truncate(parent.command))
        return PortOwner(self.port, self.pid, _truncate(self.command), parent)

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


def _truncate(command: str | None, limit: int = MAX_COMMAND) -> str | None:
    if not command:
        return None
    command = " ".join(command.split())
    if len(command) > limit:
        return command[: limit - 1] + "…"
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


def _proc_command(pid: int, proc: Path, limit: int = MAX_COMMAND) -> str | None:
    try:
        raw = (proc / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return _truncate(raw.replace(b"\0", b" ").decode(errors="replace").strip(), limit)


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


def _proc_owner(
    port: int, proc: Path, limit: int = MAX_COMMAND
) -> tuple[int, str | None, int | None] | None:
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
                    return (
                        pid,
                        _proc_command(pid, proc, limit),
                        _proc_parent(pid, proc),
                    )
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


def _ps(pid: int, limit: int = MAX_COMMAND) -> tuple[str | None, int | None]:
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
    return _truncate(command.strip(), limit), ppid


def _lsof_owner(
    port: int, limit: int = MAX_COMMAND
) -> tuple[int, str | None, int | None] | None:
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
    command, ppid = _ps(pid, limit)
    return pid, command or _truncate(name, limit), ppid


# ----------------------------------------------------------------- public API


def port_owner(
    port: int, *, proc: Path = Path("/proc"), limit: int = MAX_COMMAND
) -> PortOwner | None:
    """The process listening on TCP ``port`` (any local address), or ``None``.

    ``limit`` bounds the command lines kept (longer ones end in ``…``).
    Never raises: an owner that cannot be determined is ``None``.
    """
    try:
        if sys.platform.startswith("linux") and (proc / "net" / "tcp").exists():
            found = _proc_owner(port, proc, limit)
            parent_command = _proc_command
        else:
            found = _lsof_owner(port, limit)
            parent_command = None
        if found is None:
            return None
        pid, command, ppid = found
        parent = None
        if ppid is not None and ppid > 1:
            if parent_command is not None:
                parent = ProcessInfo(ppid, parent_command(ppid, proc, limit))
            else:
                parent = ProcessInfo(ppid, _ps(ppid, limit)[0])
        return PortOwner(port=port, pid=pid, command=command, parent=parent)
    except Exception:  # an explanation must never break the caller
        return None


def is_piceli_forward(
    owner: PortOwner | None,
    *,
    context: str,
    namespace: str,
    target: str,
    local_port: int,
    remote_port: int,
) -> bool:
    """Whether ``owner`` is the ``kubectl port-forward`` Piceli starts for a forward.

    True only for a ``kubectl … --context CONTEXT --namespace NAMESPACE
    port-forward TARGET LOCAL:REMOTE`` process whose parent is a Piceli
    process (``piceli access``, ``observe serve`` or ``operator serve``):
    the exact argv :meth:`~piceli.k8s.observe.PortForward.command` builds.
    Anything else on the port (another project's forward, a dev server, an
    orphaned ``kubectl``) is not Piceli's, and its command line must not be
    shown. Pass an owner looked up with a generous ``limit`` so a long
    kubeconfig path does not truncate the argv.
    """
    if owner is None or not owner.command or owner.command.endswith("…"):
        return False
    argv = owner.command.split()

    def after(flag: str) -> str | None:
        try:
            return argv[argv.index(flag) + 1]
        except (ValueError, IndexError):
            return None

    if "port-forward" not in argv or not Path(argv[0]).name.startswith("kubectl"):
        return False
    rest = argv[argv.index("port-forward") + 1 :]
    parent = owner.parent.command if owner.parent is not None else None
    return (
        rest[:2] == [target, f"{local_port}:{remote_port}"]
        and after("--context") == context
        and after("--namespace") == namespace
        and parent is not None
        and _is_piceli(parent)
    )


def _is_piceli(command: str) -> bool:
    """``…/piceli …`` or ``python -m piceli …``."""
    argv = command.split()
    return any(
        Path(item).name == "piceli"
        or (item == "-m" and index + 1 < len(argv) and argv[index + 1] == "piceli")
        for index, item in enumerate(argv)
    )


# ------------------------------------------------------- Piceli's own processes

#: A port's holder, from :func:`recognise`.
PICELI_FORWARD = "piceli-forward"
PICELI_SERVER = "piceli-server"
OTHER = "other"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Holder:
    """Who holds a port, as far as Piceli can prove it.

    ``kind`` is :data:`PICELI_FORWARD` (the ``kubectl port-forward`` Piceli
    starts for one of this target's declared forwards), :data:`PICELI_SERVER`
    (``piceli access`` / ``observe serve`` / ``operator serve`` for this
    target), :data:`OTHER` (anything else: only its pid may be shown) or
    :data:`UNKNOWN` (no owner found). ``stop`` is the pid ``piceli access stop
    --stale`` would signal: the supervising Piceli process of a forward (so it
    does not restart it), else the listener itself; ``None`` for others.
    """

    kind: str
    owner: PortOwner | None = None
    stop: int | None = None

    @property
    def piceli(self) -> bool:
        return self.kind in {PICELI_FORWARD, PICELI_SERVER}

    def public(self) -> dict[str, Any]:
        """Owner JSON: a Piceli process with its command, anything else pid only."""
        if self.owner is None:
            return {"holder": self.kind, "owner": None}
        owner = (
            self.owner.shortened()
            if self.piceli
            else PortOwner(port=self.owner.port, pid=self.owner.pid)
        )
        return {"holder": self.kind, "owner": owner.to_dict()}

    def describe(self) -> str:
        """``pid 42 (not piceli)`` / ``piceli's forward for this app (pid 42)``."""
        if self.owner is None:
            return "an unidentified process"
        if self.kind == PICELI_FORWARD:
            return f"piceli's own forward for this app (pid {self.owner.pid})"
        if self.kind == PICELI_SERVER:
            return f"a piceli process for this app (pid {self.owner.pid})"
        return f"pid {self.owner.pid} (not piceli)"


def _argv(command: str | None) -> list[str] | None:
    if not command or command.endswith("…"):
        return None
    return command.split()


def _piceli_args(argv: list[str]) -> list[str] | None:
    """The arguments after ``piceli`` (``…/piceli ARGS``, ``python -m piceli ARGS``)."""
    name = Path(argv[0]).name
    if name == "piceli":
        return argv[1:]
    if name.startswith("python"):
        for index, item in enumerate(argv[1:-1], start=1):
            if item == "-m":
                return argv[index + 2 :] if argv[index + 1] == "piceli" else None
    return None


def _same_path(left: str, right: str) -> bool:
    if left == right:
        return True
    try:
        return (
            Path(left).is_absolute() and Path(left).resolve() == Path(right).resolve()
        )
    except OSError:
        return False


def recognise(
    owner: PortOwner | None,
    *,
    kubeconfig: str,
    context: str,
    forwards: Iterable[tuple[str, str, int, int]] = (),
    targets: Iterable[str] = (),
) -> Holder:
    """Whether ``owner`` is one of Piceli's own processes for this target.

    A **forward** is a ``kubectl`` whose arguments are exactly the ones Piceli
    builds (``--kubeconfig KUBECONFIG --context CONTEXT --namespace NAMESPACE
    port-forward TARGET LOCAL:REMOTE --address 127.0.0.1``) for one of
    ``forwards`` (``(namespace, target, local, remote)``), started by a Piceli process or
    orphaned (its Piceli parent died). A **server** is a Piceli process
    (``piceli …`` or ``python -m piceli …``) running ``access`` for one of
    ``targets`` (the TARGET argument as given, or its absolute path), or
    ``observe serve`` / ``operator serve`` with the same ``--kubeconfig`` and
    ``--context``. Anything else is :data:`OTHER`. Look the owner up with a
    generous ``limit`` so long paths are not truncated.
    """
    if owner is None:
        return Holder(UNKNOWN)
    argv = _argv(owner.command)
    if argv is None:
        return Holder(OTHER, owner)
    parent = owner.parent
    if Path(argv[0]).name.startswith("kubectl"):
        expected = {
            (
                "--kubeconfig",
                kubeconfig,
                "--context",
                context,
                "--namespace",
                namespace,
                "port-forward",
                target,
                f"{local}:{remote}",
                "--address",
                "127.0.0.1",
            )
            for namespace, target, local, remote in forwards
        }
        if tuple(argv[1:]) not in expected:
            return Holder(OTHER, owner)
        if parent is None:  # orphaned: its supervisor died
            return Holder(PICELI_FORWARD, owner, owner.pid)
        supervisor = _piceli_args(parent.command.split()) if parent.command else None
        if supervisor and supervisor[0] in {"access", "observe", "operator"}:
            return Holder(PICELI_FORWARD, owner, parent.pid)
        return Holder(OTHER, owner)
    args = _piceli_args(argv)
    if args is None:
        return Holder(OTHER, owner)

    def after(flag: str) -> str | None:
        try:
            return args[args.index(flag) + 1]
        except (ValueError, IndexError):
            return None

    wanted = set(targets)
    if args[:1] == ["access"]:
        positional = [item for item in args[1:] if not item.startswith("-")]
        if any(_same_path(item, name) for item in positional for name in wanted):
            return Holder(PICELI_SERVER, owner, owner.pid)
        return Holder(OTHER, owner)
    if args[:2] in (["observe", "serve"], ["operator", "serve"]):
        config = after("--kubeconfig")
        if (
            after("--context") == context
            and config is not None
            and _same_path(config, kubeconfig)
        ):
            return Holder(PICELI_SERVER, owner, owner.pid)
    return Holder(OTHER, owner)
