"""0.28.0: ``piceli builder install``: a build host serves as a service.

The unit (systemd) or property list (launchd) runs the same ``piceli`` that
installed it with the exact ``builder serve`` arguments, restarts on
failure, stops with SIGINT and finds docker, git and kubectl. Every
``systemctl``/``launchctl`` call goes through a fake runner here.
"""

from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from piceli.infra import builder_service as bs

PICELI = Path("/opt/piceli/bin/piceli")
SERVE = ["--cluster", "deploy.infra:cluster", "--host", "builder-1"]
TOOLS = ("docker", "git", "kubectl")


class FakeRunner:
    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        code = 1 if self.fail and self.fail in argv else 0
        return subprocess.CompletedProcess(list(argv), code, "", "boom" if code else "")


def _machine(
    tmp_path: Path,
    *,
    system: str = "linux",
    euid: int = 1000,
    missing: Sequence[str] = (),
    runner: FakeRunner | None = None,
) -> bs.Machine:
    def which(name: str, path: str | None = None) -> str | None:
        if name in missing:
            return None
        return {"docker": "/usr/local/bin/docker"}.get(name, f"/opt/tools/bin/{name}")

    return bs.Machine(
        system=system,
        euid=euid,
        home=tmp_path / "home",
        root=tmp_path / "root",
        environ={
            "PATH": "/opt/tools/bin:/usr/bin",
            "PICELI_CREDENTIALS_DIR": "/srv/piceli/credentials",
            "PICELI_PROFILES_DIR": "/srv/piceli/profiles",
            "SECRET_TOKEN": "s3cr3t-value",
        },
        which=which,
        run=runner or FakeRunner(),
        home_of=lambda user: Path(f"/home/{user}"),
        piceli=PICELI,
    )


def _plan(machine: bs.Machine, **extra: object) -> bs.Service:
    options: dict[str, object] = {
        "host": "builder-1",
        "serve": SERVE,
        "user": None,
        "state_dir": None,
        "cwd": Path("/srv/deploy"),
        "version": "0.28.0",
        "tools": TOOLS,
    }
    options.update(extra)
    return bs.plan(machine, **options)  # type: ignore[arg-type]


def test_a_user_systemd_unit_runs_the_same_piceli_with_the_serve_arguments(
    tmp_path: Path,
) -> None:
    machine = _machine(tmp_path)
    service = _plan(machine)
    assert (service.manager, service.scope) == ("systemd", "user")
    assert (
        service.path
        == tmp_path / "home/.config/systemd/user/piceli-builder-builder-1.service"
    )
    text = bs.render(service)
    lines = text.splitlines()
    assert "Piceli 0.28.0" in lines[0]
    assert "Description=Piceli build host builder-1 (Piceli 0.28.0)" in lines
    assert (
        "ExecStart=/opt/piceli/bin/piceli builder serve --cluster "
        "deploy.infra:cluster --host builder-1" in lines
    )
    for line in (
        "Restart=on-failure",
        "RestartSec=10",
        "KillSignal=SIGINT",
        "StandardOutput=journal",
        "StandardError=journal",
        "WorkingDirectory=/srv/deploy",
        f'Environment="HOME={tmp_path / "home"}"',
        'Environment="PICELI_CREDENTIALS_DIR=/srv/piceli/credentials"',
        'Environment="PICELI_PROFILES_DIR=/srv/piceli/profiles"',
        "WantedBy=default.target",
    ):
        assert line in lines, line
    (path,) = [line for line in lines if line.startswith('Environment="PATH=')]
    dirs = path.removeprefix('Environment="PATH=').rstrip('"').split(":")
    assert dirs[:3] == ["/usr/local/bin", "/opt/tools/bin", "/opt/piceli/bin"]
    assert "/usr/bin" in dirs and "/bin" in dirs
    assert "User=" not in text  # a user unit runs as its user
    assert "s3cr3t-value" not in text
    assert service.notes and "loginctl enable-linger" in service.notes[0]


def test_a_system_unit_as_root_runs_as_the_named_user(tmp_path: Path) -> None:
    machine = _machine(tmp_path, euid=0)
    service = _plan(machine, user="builder", state_dir=Path("/var/lib/piceli-builder"))
    assert (service.manager, service.scope) == ("systemd", "system")
    assert (
        service.path
        == tmp_path / "root/etc/systemd/system/piceli-builder-builder-1.service"
    )
    lines = bs.render(service).splitlines()
    assert "User=builder" in lines
    assert 'Environment="HOME=/home/builder"' in lines
    assert "WantedBy=multi-user.target" in lines
    assert "After=network-online.target docker.service" in lines
    (start,) = [line for line in lines if line.startswith("ExecStart=")]
    assert start == (
        "ExecStart=/opt/piceli/bin/piceli builder serve --cluster deploy.infra:cluster "
        "--host builder-1 --state-dir /var/lib/piceli-builder"
    )
    assert service.notes == []


def test_root_without_a_user_is_refused(tmp_path: Path) -> None:
    with pytest.raises(bs.ServiceError) as raised:
        _plan(_machine(tmp_path, euid=0))
    assert raised.value.code == "builder-service-invalid"
    assert "--user USER" in str(raised.value)


def test_a_user_other_than_oneself_needs_root(tmp_path: Path) -> None:
    with pytest.raises(bs.ServiceError) as raised:
        _plan(_machine(tmp_path), user="builder")
    assert "as root" in str(raised.value)


def test_a_missing_tool_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(bs.ServiceError) as raised:
        _plan(_machine(tmp_path, missing=("kubectl", "git")))
    assert raised.value.code == "builder-service-invalid"
    assert "git, kubectl" in str(raised.value)


def test_arguments_with_spaces_and_percent_are_quoted_for_systemd(
    tmp_path: Path,
) -> None:
    service = _plan(
        _machine(tmp_path), serve=[*SERVE, "--image", "reg.example.org/b:1%x y"]
    )
    (line,) = [
        text
        for text in bs.render(service).splitlines()
        if text.startswith("ExecStart=")
    ]
    assert line.endswith('--image "reg.example.org/b:1%%x y"')


def test_a_launch_agent_on_macos_logs_under_the_state_dir(tmp_path: Path) -> None:
    machine = _machine(tmp_path, system="darwin", euid=501)
    service = _plan(machine)
    assert (service.manager, service.scope) == ("launchd", "user")
    assert (
        service.path
        == tmp_path / "home/Library/LaunchAgents/piceli.builder.builder-1.plist"
    )
    text = bs.render(service)
    assert "<!-- Written by piceli builder install (Piceli 0.28.0)" in text
    body = plistlib.loads(text.encode())
    assert body["Label"] == "piceli.builder.builder-1"
    assert body["ProgramArguments"] == [str(PICELI), "builder", "serve", *SERVE]
    assert body["KeepAlive"] == {"SuccessfulExit": False}
    assert body["ThrottleInterval"] == 10
    assert body["RunAtLoad"] is True
    assert body["WorkingDirectory"] == "/srv/deploy"
    log = str(tmp_path / "home/.cache/piceli/builder/builder.log")
    assert body["StandardOutPath"] == body["StandardErrorPath"] == log
    env = body["EnvironmentVariables"]
    assert env["HOME"] == str(tmp_path / "home")
    assert env["PICELI_CREDENTIALS_DIR"] == "/srv/piceli/credentials"
    assert "/usr/local/bin" in env["PATH"].split(":")
    assert "UserName" not in body
    assert "s3cr3t-value" not in text


def test_a_launch_daemon_as_root_names_its_user(tmp_path: Path) -> None:
    machine = _machine(tmp_path, system="darwin", euid=0)
    service = _plan(machine, user="builder")
    assert (
        service.path
        == tmp_path / "root/Library/LaunchDaemons/piceli.builder.builder-1.plist"
    )
    body = plistlib.loads(bs.render(service).encode())
    assert body["UserName"] == "builder"
    assert body["StandardOutPath"] == "/home/builder/.cache/piceli/builder/builder.log"


def test_install_writes_the_unit_and_enables_it_through_the_runner(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    machine = _machine(tmp_path, runner=runner)
    service = _plan(machine)
    bs.install(machine, service)
    assert service.path.read_text() == bs.render(service)
    unit = "piceli-builder-builder-1.service"
    assert runner.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", unit],
        ["systemctl", "--user", "restart", unit],
    ]


def test_install_on_macos_bootstraps_the_agent(tmp_path: Path) -> None:
    runner = FakeRunner()
    machine = _machine(tmp_path, system="darwin", euid=501, runner=runner)
    service = _plan(machine)
    bs.install(machine, service)
    assert (tmp_path / "home/.cache/piceli/builder").is_dir()  # the log's directory
    assert runner.calls == [
        ["launchctl", "bootout", "gui/501/piceli.builder.builder-1"],
        ["launchctl", "bootstrap", "gui/501", str(service.path)],
        ["launchctl", "enable", "gui/501/piceli.builder.builder-1"],
    ]


def test_a_failing_systemctl_is_reported(tmp_path: Path) -> None:
    machine = _machine(tmp_path, runner=FakeRunner(fail="restart"))
    with pytest.raises(bs.ServiceError) as raised:
        bs.install(machine, _plan(machine))
    assert raised.value.code == "builder-service-failed"
    assert "boom" in str(raised.value)


def test_uninstall_stops_disables_and_removes_it(tmp_path: Path) -> None:
    runner = FakeRunner()
    machine = _machine(tmp_path, runner=runner)
    service = _plan(machine)
    bs.install(machine, service)
    runner.calls.clear()
    removed = bs.uninstall(machine, "builder-1")
    assert removed == service.path and not service.path.exists()
    unit = "piceli-builder-builder-1.service"
    assert runner.calls == [
        ["systemctl", "--user", "disable", "--now", unit],
        ["systemctl", "--user", "daemon-reload"],
    ]
    runner.calls.clear()
    assert bs.uninstall(machine, "builder-1") is None  # nothing left: no call
    assert runner.calls == []


def test_uninstall_on_macos_boots_out_the_daemon(tmp_path: Path) -> None:
    runner = FakeRunner()
    machine = _machine(tmp_path, system="darwin", euid=0, runner=runner)
    service = _plan(machine, user="builder")
    service.path.parent.mkdir(parents=True)
    service.path.write_text(bs.render(service))
    assert bs.uninstall(machine, "builder-1") == service.path
    assert runner.calls == [["launchctl", "bootout", "system/piceli.builder.builder-1"]]
    assert not service.path.exists()


def test_an_unsupported_system_is_refused(tmp_path: Path) -> None:
    with pytest.raises(bs.ServiceError) as raised:
        _plan(_machine(tmp_path, system="windows"))
    assert "systemd" in str(raised.value) and "launchd" in str(raised.value)
