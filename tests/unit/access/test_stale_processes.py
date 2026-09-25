"""Piceli's own stale processes: recognised, named and stoppable; others pid only."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from piceli.k8s import access as access_module
from piceli.k8s.access import (
    PortConflict,
    holder_check,
    port_conflicts,
    resolve_target,
    stop_stale,
)
from piceli.k8s.cli import access as cli_access
from piceli.k8s.cli import app
from piceli.k8s.port_owner import (
    OTHER,
    PICELI_FORWARD,
    PICELI_SERVER,
    Holder,
    PortOwner,
    ProcessInfo,
    recognise,
)
from tests.unit.access import test_access_status as base
from tests.unit.access.test_access_status import (
    REPO,
    _free_port,
    _read_events,
    write_spec,
)

fake_kubectl = base.fake_kubectl
upstream = base.upstream

KUBECONFIG = "/work/kubeconfig"
FORWARDS = (("shop", "service/api", 18090, 80),)
ARGV = (
    f"/usr/local/bin/kubectl --kubeconfig {KUBECONFIG} --context demo "
    "--namespace shop port-forward service/api 18090:80 --address 127.0.0.1"
)
PICELI = ProcessInfo(77, "/work/.venv/bin/python -m piceli access release.toml")


def _check(owner: PortOwner | None, **kwargs: Any) -> Holder:
    return recognise(
        owner,
        kubeconfig=KUBECONFIG,
        context="demo",
        forwards=FORWARDS,
        targets=kwargs.pop("targets", ("release.toml",)),
    )


def test_a_supervised_forward_is_stopped_through_its_supervisor() -> None:
    holder = _check(PortOwner(18090, 4242, ARGV, PICELI))
    assert holder.kind == PICELI_FORWARD and holder.stop == 77
    assert holder.public()["owner"]["command"] == ARGV
    assert holder.describe() == "piceli's own forward for this app (pid 4242)"


def test_an_orphaned_forward_is_stopped_itself() -> None:
    holder = _check(PortOwner(18090, 4242, ARGV, None))
    assert holder.kind == PICELI_FORWARD and holder.stop == 4242


@pytest.mark.parametrize(
    "command,parent",
    [
        # Another app's forward (other namespace), started by piceli.
        (ARGV.replace("--namespace shop", "--namespace other"), PICELI),
        # The same argv typed by hand in a shell.
        (ARGV, ProcessInfo(55, "-zsh")),
        # A parent whose command could not be read is not proof.
        (ARGV, ProcessInfo(55, None)),
        # Truncated: cannot be verified.
        (ARGV[:40] + "…", PICELI),
        ("python3 -m http.server 18090", None),
    ],
)
def test_anything_else_is_foreign_and_named_by_pid_only(
    command: str, parent: ProcessInfo | None
) -> None:
    holder = _check(PortOwner(18090, 4242, command, parent))
    assert holder.kind == OTHER and holder.stop is None
    assert holder.public() == {
        "holder": "other",
        "owner": {"port": 18090, "pid": 4242, "command": None, "parent": None},
    }
    assert holder.describe() == "pid 4242 (not piceli)"


def test_piceli_servers_for_the_same_target(tmp_path: Path) -> None:
    spec = tmp_path / "release.toml"
    spec.write_text("")
    access = PortOwner(9000, 11, f"piceli access {spec} --dashboard 9000")
    holder = _check(access, targets=("release.toml", str(spec)))
    assert (holder.kind, holder.stop) == (PICELI_SERVER, 11)
    assert _check(access, targets=("other.toml",)).kind == OTHER
    serve = PortOwner(
        9000,
        12,
        f"python -m piceli observe serve --kubeconfig {KUBECONFIG} --context demo",
    )
    assert _check(serve).kind == PICELI_SERVER
    operator = PortOwner(
        9000,
        13,
        f"piceli operator serve --kubeconfig {KUBECONFIG} --context prod",
    )
    assert _check(operator).kind == OTHER
    assert _check(PortOwner(9000, 14, "piceli observe serve")).kind == OTHER
    assert _check(None).kind == "unknown"


def test_conflicts_keep_the_command_of_piceli_processes_only() -> None:
    mine = Holder(PICELI_FORWARD, PortOwner(18090, 4242, ARGV, PICELI), 77)
    conflict = PortConflict("api", 18090, True, mine.owner, mine)
    assert conflict.to_dict()["holder"] == "piceli-forward"
    assert conflict.to_dict()["owner"]["command"] == ARGV
    assert "piceli's own forward" in conflict.describe()
    foreign = PortConflict("api", 18090, True, PortOwner(18090, 5, "secret argv"))
    assert foreign.to_dict()["owner"]["command"] is None
    assert foreign.to_dict()["holder"] == "other"
    assert "secret argv" not in foreign.describe()
    assert (
        PortConflict("api", 18090, True).describe().endswith("an unidentified process")
    )


def test_stop_stale_signals_only_piceli_processes() -> None:
    owners = {
        18090: PortOwner(18090, 4242, ARGV, PICELI),
        18091: PortOwner(18091, 4243, ARGV.replace("18090", "18091"), PICELI),
        18092: PortOwner(18092, 500, "nginx: master process"),
    }
    forwards = (
        *FORWARDS,
        ("shop", "service/api", 18091, 80),
    )
    held = {18090, 18091, 18092}
    signalled: list[object] = []

    def terminate(pid: object) -> bool:
        signalled.append(pid)
        # The supervisor stops both of its forwards a moment later.
        threading.Timer(0.2, held.difference_update, [{18090, 18091}]).start()
        return True

    def check(owner: PortOwner | None) -> Holder:
        return recognise(
            owner, kubeconfig=KUBECONFIG, context="demo", forwards=forwards
        )

    result = stop_stale(
        [18090, 18091, 18092, 18093],
        check,
        in_use=lambda port: port in held,
        owner=owners.get,
        terminate=terminate,
        wait_seconds=1,
    )
    assert signalled == [77]  # one supervisor, signalled once
    assert [item["port"] for item in result["stopped"]] == [18090, 18091]
    assert result["left"] == [
        {
            "port": 18092,
            "holder": "other",
            "owner": {"port": 18092, "pid": 500, "command": None, "parent": None},
        }
    ]
    assert result["free"] == [18093] and result["failed"] == []


def test_stop_stale_rechecks_and_reports_a_port_still_held() -> None:
    looks = iter(
        [PortOwner(18090, 4242, ARGV, None), PortOwner(18090, 9999, ARGV, None)]
    )
    result = stop_stale(
        [18090],
        lambda owner: _check(owner),
        in_use=lambda port: True,
        owner=lambda port: next(looks),
        terminate=lambda pid: pytest.fail("a changed holder must not be signalled"),
        wait_seconds=0,
    )
    assert result["left"][0]["owner"]["pid"] == 9999 and result["stopped"] == []
    result = stop_stale(
        [18090],
        lambda owner: _check(owner),
        in_use=lambda port: True,
        owner=lambda port: PortOwner(18090, 4242, ARGV, None),
        terminate=lambda pid: True,
        wait_seconds=0.2,
    )
    assert [item["pid"] for item in result["failed"]] == [4242]


@pytest.mark.parametrize("pid", [MagicMock().pid, 1, 0, -5, os.getpid(), "42"])
def test_terminate_refuses_what_is_not_a_real_process(pid: object) -> None:
    assert access_module._terminate(pid) is False


def test_cli_access_stop_needs_stale(tmp_path: Path) -> None:
    spec = write_spec(tmp_path, local=_free_port(), upstream=80)
    result = CliRunner().invoke(app, ["access", "stop", str(spec)])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "access-stop-needs-stale"


def test_cli_access_conflict_offers_stop_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_kubectl: Path
) -> None:
    local = _free_port()
    spec = write_spec(tmp_path, local=local, upstream=80)
    target = resolve_target(str(spec))
    argv = (
        f"kubectl --kubeconfig {target.kubeconfig} --context demo --namespace shop "
        f"port-forward service/api {local}:80 --address 127.0.0.1"
    )
    stale = PortOwner(local, 4242, argv, None)
    real = port_conflicts

    def conflicts(shortcuts: Any, **kwargs: Any) -> Any:
        return real(
            shortcuts,
            in_use=lambda port: port == local,
            owner=lambda port: stale,
            **kwargs,
        )

    monkeypatch.setattr(cli_access, "port_conflicts", conflicts)
    result = CliRunner().invoke(
        app, ["access", str(spec), "--only", "api", "--kubectl", str(fake_kubectl)]
    )
    assert result.exit_code == 2
    body = json.loads(result.stdout)
    assert body["reason"] == "access-port-conflict"
    assert body["conflicts"][0]["holder"] == "piceli-forward"
    assert "piceli's own forward for this app (pid 4242)" in result.stderr
    assert f"piceli access stop --stale {spec}" in result.stderr


def test_holder_check_uses_the_target_forwards(tmp_path: Path) -> None:
    local = _free_port()
    target = resolve_target(str(write_spec(tmp_path, local=local, upstream=80)))
    check = holder_check(target)
    argv = (
        f"kubectl --kubeconfig {target.kubeconfig} --context demo --namespace shop "
        f"port-forward service/api {local}:80 --address 127.0.0.1"
    )
    assert check(PortOwner(local, 4242, argv, None)).kind == PICELI_FORWARD
    assert check(PortOwner(local, 4242, argv.replace("demo", "x"), None)).kind == OTHER


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or shutil.which("lsof")),
    reason="needs /proc or lsof to find a port's owner",
)
def test_cli_access_stop_stale_stops_a_leftover_piceli_access(
    tmp_path: Path, fake_kubectl: Path, upstream: int
) -> None:
    local, dashboard = _free_port(), _free_port()
    spec = write_spec(tmp_path, local=local, upstream=upstream)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "piceli",
            "access",
            str(spec),
            "--only",
            "api",
            "--json",
            "--kubectl",
            str(fake_kubectl),
            "--poll",
            "0.2",
            "--dashboard",
            str(dashboard),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO,
        env={**os.environ, "PICELI__UI_CONFIG": ""},
    )
    try:
        events = _read_events(process, lambda event: event.get("health") == "healthy")
        assert events and events[-1].get("health") == "healthy", events
        # A second access sees the dashboard port held by piceli for this app.
        second = CliRunner().invoke(
            app,
            [
                "access",
                str(spec),
                "--only",
                "api",
                "--kubectl",
                str(fake_kubectl),
                "--dashboard",
                str(dashboard),
            ],
        )
        assert second.exit_code == 2
        # The fake kubectl is not a real kubectl: its port shows as foreign,
        # by pid only (a real kubectl forward is covered by the kind test).
        assert json.loads(second.stdout)["conflicts"][0]["owner"]["command"] is None
        stop = CliRunner().invoke(
            app, ["access", "stop", "--stale", str(spec), "--port", str(dashboard)]
        )
        assert stop.exit_code == 0, stop.stdout + stop.stderr
        body = json.loads(stop.stdout)
        assert [(item["port"], item["pid"]) for item in body["stopped"]] == [
            (dashboard, process.pid)
        ]
        assert body["stopped"][0]["holder"] == "piceli-server"
        assert [item["port"] for item in body["left"]] == [local]
        assert body["left"][0]["owner"]["command"] is None
        process.wait(timeout=15)
        assert process.returncode is not None
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGKILL)
            process.wait(timeout=5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and access_module.local_port_in_use(local):
        time.sleep(0.05)
    assert not access_module.local_port_in_use(local)
    assert not access_module.local_port_in_use(dashboard)
