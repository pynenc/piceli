"""``piceli builder install``: a build host serves as a service (0.28.0).

``piceli builder serve`` in a terminal stops when the terminal or the
machine sleeps. ``install`` writes and enables a service on the machine it
runs on (the build host itself):

- **systemd** on Linux: a system unit in ``/etc/systemd/system`` when run as
  root (``--user USER`` runs it), else a user unit in
  ``~/.config/systemd/user`` (``loginctl enable-linger`` keeps it running
  while that user is logged out);
- **launchd** on macOS: a LaunchDaemon in ``/Library/LaunchDaemons`` (root,
  with ``UserName``) or a LaunchAgent in ``~/Library/LaunchAgents``.

The service runs the same ``piceli`` (absolute path, its version in the
description) with the exact ``builder serve`` arguments, in the directory
``install`` ran in (``--cluster MODULE:ATTR`` loads from there), restarts on
failure after 10 seconds and stops with SIGINT, the signal ``serve`` stops
cleanly on. Its ``PATH`` holds the directories of docker, git and kubectl
as found now. Only directories go into its environment, never a secret.

Every ``systemctl``/``launchctl`` call goes through :attr:`Machine.run`, so
tests use a fake. Importing this module is side-effect free.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The programs ``builder serve`` runs (docker only on a docker host).
TOOLS = ("docker", "git", "kubectl")
#: Environment passed on to the service when set where ``install`` runs.
PASSED_ENV = ("PICELI_CREDENTIALS_DIR", "PICELI_PROFILES_DIR")
#: Seconds before a failed service starts again.
RESTART_SECONDS = 10
#: Seconds a stopping service may take (a build stops at SIGINT).
STOP_SECONDS = 120
_BASE_PATH = ("/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,61}[a-z0-9])?")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class ServiceError(Exception):
    """A refusal (``builder-service-invalid``) or a failed service manager
    call (``builder-service-failed``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), check=False, capture_output=True, text=True, timeout=120
    )


def _home_of(user: str) -> Path:
    import pwd

    try:
        return Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        raise ServiceError(
            "builder-service-invalid", f"there is no user {user} on this machine"
        ) from None


def _this_piceli() -> Path | None:
    """The ``piceli`` program running now (absolute, not resolved: a
    ``uv tool`` link keeps pointing at the installed version)."""
    argv0 = Path(sys.argv[0])
    if argv0.name == "piceli" and argv0.is_file():
        return argv0.absolute()
    found = shutil.which("piceli")
    return Path(found).absolute() if found else None


@dataclass(frozen=True)
class Machine:
    """The machine ``install`` runs on: everything it asks of it."""

    system: str  # "linux" or "darwin"
    euid: int
    home: Path
    environ: Mapping[str, str]
    which: Callable[..., str | None]
    run: Runner
    home_of: Callable[[str], Path]
    piceli: Path | None
    #: Prefix of system paths (``/``; a temporary directory in tests).
    root: Path = Path("/")

    @classmethod
    def current(cls) -> Machine:
        return cls(
            system=sys.platform,
            euid=os.geteuid(),
            home=Path.home(),
            environ=dict(os.environ),
            which=shutil.which,
            run=_run,
            home_of=_home_of,
            piceli=_this_piceli(),
        )


@dataclass(frozen=True)
class Service:
    """One build host's service, as :func:`render` writes it."""

    host: str
    manager: str  # "systemd" or "launchd"
    scope: str  # "system" or "user"
    name: str  # the unit name or launchd label
    path: Path  # where the unit or property list goes
    argv: tuple[str, ...]
    environment: dict[str, str]
    working_dir: Path
    user: str | None
    version: str
    log: Path | None  # launchd: the log file under the state directory
    uid: int
    notes: list[str] = field(default_factory=list)


def unit_name(host: str) -> str:
    return f"piceli-builder-{host}.service"


def label(host: str) -> str:
    return f"piceli.builder.{host}"


def _manager(machine: Machine) -> str:
    if machine.system.startswith("linux"):
        return "systemd"
    if machine.system == "darwin":
        return "launchd"
    raise ServiceError(
        "builder-service-invalid",
        f"builder install writes systemd (Linux) or launchd (macOS) services, "
        f"not for {machine.system}; run builder serve under your own supervisor",
    )


def _location(machine: Machine, manager: str, host: str, system: bool) -> Path:
    if manager == "systemd":
        base = (
            machine.root / "etc/systemd/system"
            if system
            else machine.home / ".config/systemd/user"
        )
        return base / unit_name(host)
    base = (
        machine.root / "Library/LaunchDaemons"
        if system
        else machine.home / "Library/LaunchAgents"
    )
    return base / f"{label(host)}.plist"


def plan(
    machine: Machine,
    *,
    host: str,
    serve: Sequence[str],
    user: str | None,
    state_dir: Path | None,
    cwd: Path,
    version: str,
    tools: Sequence[str] = TOOLS,
) -> Service:
    """The service of ``host`` on ``machine``: ``serve`` are the ``builder
    serve`` arguments (``--state-dir`` is added when given).

    :raises ServiceError: ``builder-service-invalid`` (no service manager,
        root without ``--user``, another user without root, a missing tool,
        no ``piceli`` program).
    """
    if not _NAME.fullmatch(host):
        raise ServiceError("builder-service-invalid", "a build host name is a DNS name")
    manager = _manager(machine)
    root = machine.euid == 0
    if root and not user:
        raise ServiceError(
            "builder-service-invalid",
            "as root, name the user the build host runs as: --user USER "
            "(it needs docker access and holds the credentials); without root "
            "the service runs as you",
        )
    if user and not root:
        raise ServiceError(
            "builder-service-invalid",
            f"a service that runs as {user} is installed as root (sudo); "
            "without --user it runs as you",
        )
    if machine.piceli is None:
        raise ServiceError(
            "builder-service-invalid",
            "cannot tell which piceli program runs; run builder install with "
            "the piceli command the service should use",
        )
    search = machine.environ.get("PATH") or os.pathsep.join(_BASE_PATH)
    found = {name: machine.which(name, path=search) for name in tools}
    missing = [name for name, where in found.items() if not where]
    if missing:
        raise ServiceError(
            "builder-service-invalid",
            f"builder serve needs {', '.join(sorted(missing))} on this machine "
            f"(not found in PATH={search}); install them or add their "
            "directories to PATH, then run builder install again",
        )
    home = machine.home_of(str(user)) if user else machine.home
    dirs = [str(Path(str(where)).parent) for where in found.values()]
    dirs.append(str(machine.piceli.parent))
    path = ":".join(dict.fromkeys([*dirs, *_BASE_PATH]))
    environment = {"HOME": str(home), "PATH": path}
    for name in PASSED_ENV:
        if machine.environ.get(name):
            environment[name] = str(machine.environ[name])
    argv = [str(machine.piceli), "builder", "serve", *serve]
    if state_dir is not None:
        argv += ["--state-dir", str(state_dir)]
    notes = []
    if manager == "systemd" and not root:
        notes.append(
            "a user service stops when you log out unless lingering is on: "
            f"sudo loginctl enable-linger {machine.environ.get('USER') or '$USER'}"
        )
    log = None
    if manager == "launchd":
        base = state_dir or home / ".cache/piceli/builder"
        log = Path(base) / "builder.log"
    return Service(
        host=host,
        manager=manager,
        scope="system" if root else "user",
        name=unit_name(host) if manager == "systemd" else label(host),
        path=_location(machine, manager, host, root),
        argv=tuple(argv),
        environment=environment,
        working_dir=cwd,
        user=user,
        version=version,
        log=log,
        uid=machine.euid,
        notes=notes,
    )


def _systemd_word(text: str) -> str:
    """One ``ExecStart=``/``Environment=`` word, as systemd reads it."""
    text = text.replace("%", "%%").replace("$", "$$")
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _header(service: Service) -> str:
    # No double dash: the launchd header is an XML comment.
    return (
        f"Written by piceli builder install (Piceli {service.version}) for "
        f"build host {service.host}; piceli builder uninstall removes it."
    )


def render(service: Service) -> str:
    """The unit file (systemd) or property list (launchd)."""
    if service.manager == "systemd":
        return _systemd(service)
    return _launchd(service)


def _systemd(service: Service) -> str:
    system = service.scope == "system"
    lines = [
        f"# {_header(service)}",
        "[Unit]",
        f"Description=Piceli build host {service.host} (Piceli {service.version})",
        "Wants=network-online.target",
        "After=network-online.target docker.service"
        if system
        else "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        *([f"User={service.user}"] if system and service.user else []),
        f"WorkingDirectory={service.working_dir}",
        *(
            f'Environment="{name}={value.replace("%", "%%")}"'
            for name, value in service.environment.items()
        ),
        "ExecStart=" + " ".join(_systemd_word(word) for word in service.argv),
        "Restart=on-failure",
        f"RestartSec={RESTART_SECONDS}",
        # builder serve stops cleanly on SIGINT (Ctrl-C); SIGTERM would end
        # a build half-way.
        "KillSignal=SIGINT",
        f"TimeoutStopSec={STOP_SECONDS}",
        "StandardOutput=journal",
        "StandardError=journal",
        f"SyslogIdentifier=piceli-builder-{service.host}",
        "",
        "[Install]",
        f"WantedBy={'multi-user.target' if system else 'default.target'}",
        "",
    ]
    return "\n".join(lines)


def _launchd(service: Service) -> str:
    body: dict[str, Any] = {
        "Label": service.name,
        "ProgramArguments": list(service.argv),
        "WorkingDirectory": str(service.working_dir),
        "EnvironmentVariables": dict(service.environment),
        "RunAtLoad": True,
        # Started again when it exits with an error, not after a clean stop.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": RESTART_SECONDS,
        "ExitTimeOut": STOP_SECONDS,
        "StandardOutPath": str(service.log),
        "StandardErrorPath": str(service.log),
    }
    if service.scope == "system" and service.user:
        body["UserName"] = service.user
    text = plistlib.dumps(body, sort_keys=False).decode()
    head, _, rest = text.partition("\n")
    return f"{head}\n<!-- {_header(service)} -->\n{rest}"


def _call(machine: Machine, argv: Sequence[str], *, quiet: bool = False) -> None:
    result = machine.run(list(argv))
    if result.returncode != 0 and not quiet:
        detail = (result.stderr or result.stdout or "").strip()[-400:]
        raise ServiceError(
            "builder-service-failed",
            f"{' '.join(argv)} failed (exit {result.returncode})"
            + (f": {detail}" if detail else ""),
        )


def _systemctl(scope: str) -> list[str]:
    return ["systemctl"] if scope == "system" else ["systemctl", "--user"]


def _domain(scope: str, uid: int) -> str:
    return "system" if scope == "system" else f"gui/{uid}"


def install(machine: Machine, service: Service) -> list[list[str]]:
    """Write ``service`` and (re)start it; the commands run.

    :raises ServiceError: ``builder-service-failed`` (a manager call failed).
    """
    service.path.parent.mkdir(parents=True, exist_ok=True)
    service.path.write_text(render(service))
    commands: list[list[str]]
    if service.manager == "systemd":
        ctl = _systemctl(service.scope)
        commands = [
            [*ctl, "daemon-reload"],
            [*ctl, "enable", service.name],
            # restart, not start: an earlier version of the unit may run.
            [*ctl, "restart", service.name],
        ]
        for argv in commands:
            _call(machine, argv)
        return commands
    if service.log is not None:
        service.log.parent.mkdir(parents=True, exist_ok=True)
    domain = _domain(service.scope, service.uid)
    commands = [
        ["launchctl", "bootout", f"{domain}/{service.name}"],
        ["launchctl", "bootstrap", domain, str(service.path)],
        ["launchctl", "enable", f"{domain}/{service.name}"],
    ]
    _call(machine, commands[0], quiet=True)  # not loaded yet: fine
    for argv in commands[1:]:
        _call(machine, argv)
    return commands


def uninstall(machine: Machine, host: str) -> Path | None:
    """Stop, disable and remove ``host``'s service; its file, or ``None``
    when none is installed (nothing is run then)."""
    if not _NAME.fullmatch(host):
        raise ServiceError("builder-service-invalid", "a build host name is a DNS name")
    manager = _manager(machine)
    system = machine.euid == 0
    path = _location(machine, manager, host, system)
    if not path.is_file():
        return None
    scope = "system" if system else "user"
    if manager == "systemd":
        ctl = _systemctl(scope)
        _call(machine, [*ctl, "disable", "--now", unit_name(host)])
        path.unlink()
        _call(machine, [*ctl, "daemon-reload"])
        return path
    _call(
        machine,
        ["launchctl", "bootout", f"{_domain(scope, machine.euid)}/{label(host)}"],
        quiet=True,
    )
    path.unlink()
    return path


__all__ = [
    "PASSED_ENV",
    "TOOLS",
    "Machine",
    "Service",
    "ServiceError",
    "install",
    "label",
    "plan",
    "render",
    "uninstall",
    "unit_name",
]
