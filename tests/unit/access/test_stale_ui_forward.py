"""A stale ``piceli access ui``: its forward is Piceli's, and ``stop --stale`` frees it.

No cluster and no real kubectl: a fake ``piceli access ui`` (a console script
run by its interpreter, as ``ps`` shows a real one) supervises a fake
forward child under the owned-process registry. Every process a test starts
is killed when it ends.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s import access as access_module
from piceli.k8s.access import port_conflicts, stop_stale
from piceli.k8s.cli import access as cli_access
from piceli.k8s.cli import app
from piceli.k8s.cli.ui_forward import ui_holder_check
from piceli.k8s.owned_processes import (
    OwnedProcessRegistry,
    forward_label,
    process_identity,
)
from piceli.k8s.port_owner import (
    OTHER,
    PICELI_FORWARD,
    PortOwner,
    ProcessInfo,
    recognise,
)
from piceli.k8s.ui_config import UiShortcut
from tests.unit.cli.test_access_ui import lab  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="process groups and signals are POSIX"
)

REPO = Path(__file__).resolve().parents[3]
KUBECONFIG = "/work/lab.kubeconfig"
UI_ARGV = (
    f"/usr/local/bin/kubectl --kubeconfig {KUBECONFIG} --context lab "
    "--namespace piceli-system port-forward pod/piceli-ui-6d9f7c-x2b4q "
    "8790:8790 --address 127.0.0.1"
)

_FAKE_KUBECTL = """import socket
import sys

args = sys.argv[1:]
port = int(args[args.index("port-forward") + 2].split(":", 1)[0])
with socket.socket() as server:
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(8)
    while True:
        connection, _ = server.accept()
        connection.close()
"""

# A stale `piceli access ui`: supervises the UI forward until SIGTERM.
_FAKE_PICELI = """import os
import signal
import socket
import sys
import time
from pathlib import Path

from piceli.k8s.observe import ForwardSupervisor
from piceli.k8s.owned_processes import OwnedProcessRegistry
from piceli.k8s.ui_config import UiShortcut

port = int(os.environ["FAKE_UI_PORT"])
supervisor = ForwardSupervisor(
    kubeconfig=Path(os.environ["FAKE_UI_KUBECONFIG"]),
    context="lab",
    kubectl=os.environ["FAKE_UI_KUBECTL"],
    shortcuts=(
        UiShortcut(
            id="ui",
            label="Piceli UI",
            target="service/piceli-ui",
            namespace="piceli-system",
            local_port=port,
            remote_port=8790,
        ),
    ),
    namespace="piceli-system",
    registry=OwnedProcessRegistry(Path(os.environ["FAKE_UI_REGISTRY"])),
)


def stop(*_):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, stop)
try:
    supervisor.quick_start("ui", "piceli-system")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.05)
    (status,) = supervisor.statuses()
    print(status.pid, flush=True)
    time.sleep(120)
except KeyboardInterrupt:
    pass
finally:
    supervisor.close()
"""

_LISTENER = """import socket
import sys
import time

with socket.socket() as server:
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", int(sys.argv[1])))
    server.listen(8)
    print("ready", flush=True)
    time.sleep(120)
"""


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _accepting(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _alive(pid: int) -> bool:
    return process_identity(pid) is not None


@contextmanager
def _process(
    argv: list[str], env: dict[str, str] | None = None
) -> Iterator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        argv,
        cwd=REPO,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={**os.environ, **(env or {})},
    )
    try:
        yield process
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()


# ------------------------------------------------------------ recognition


def _ui(owner: PortOwner, registry: OwnedProcessRegistry | None = None) -> Any:
    return recognise(
        owner,
        kubeconfig=KUBECONFIG,
        context="lab",
        forwards=(("piceli-system", "service/piceli-ui", 8790, 8790),),
        registry=registry,
    )


@pytest.mark.parametrize(
    "supervisor",
    [
        # The console script, as ps shows it (`uv run piceli access ui`).
        "/work/.venv/bin/python /work/.venv/bin/piceli access ui --profile lab",
        "/framework/MacOS/Python /work/.venv/bin/piceli access ui --cluster i.py:c",
        "/work/.venv/bin/python3 -I /work/.venv/bin/piceli access ui --profile lab",
        "/work/.venv/bin/python -X dev -m piceli access ui --profile lab",
        "/work/.venv/bin/piceli access ui --profile lab",
    ],
)
def test_the_kubectl_of_an_access_ui_is_piceli_s_forward(supervisor: str) -> None:
    holder = _ui(PortOwner(8790, 4242, UI_ARGV, ProcessInfo(77, supervisor)))
    assert holder.kind == PICELI_FORWARD and holder.stop == 77
    assert holder.describe() == "piceli's own forward for this app (pid 4242)"


@pytest.mark.parametrize(
    "supervisor",
    [
        "/usr/bin/python3 /work/tools/not-piceli access ui",
        "/usr/bin/python3 /work/script.py -m piceli access ui",
        "/usr/bin/python3 /work/piceli/__main__.py access ui",
        "-zsh",
    ],
)
def test_a_kubectl_started_by_anything_else_is_not(supervisor: str) -> None:
    holder = _ui(PortOwner(8790, 4242, UI_ARGV, ProcessInfo(77, supervisor)))
    assert holder.kind == OTHER and holder.stop is None


def test_another_cluster_s_ui_forward_is_not_this_one() -> None:
    parent = ProcessInfo(77, "/work/.venv/bin/piceli access ui --profile other")
    argv = UI_ARGV.replace("--context lab", "--context other")
    assert _ui(PortOwner(8790, 4242, argv, parent)).kind == OTHER


# ---------------------------------------------------- the access ui command


def test_access_ui_names_its_stale_forward_and_how_to_stop_it(
    lab: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv = UI_ARGV.replace(KUBECONFIG, str(lab.absolute()))
    stale = PortOwner(
        8790, 4242, argv, ProcessInfo(77, "/v/bin/python /v/bin/piceli access ui")
    )

    def conflicts(shortcuts: Any, **kwargs: Any) -> Any:
        return port_conflicts(
            shortcuts, in_use=lambda port: True, owner=lambda port: stale, **kwargs
        )

    monkeypatch.setattr(access_module, "port_conflicts", conflicts)
    result = CliRunner().invoke(app, ["access", "ui", "--profile", "my-cluster"])
    assert result.exit_code == 2
    body = json.loads(result.stdout)
    assert body["reason"] == "access-port-conflict"
    assert body["conflicts"][0]["holder"] == "piceli-forward"
    assert "(not piceli)" not in result.stderr
    assert "piceli access stop --stale --profile my-cluster" in result.stderr


def test_access_stop_stale_with_a_profile_stops_the_ui_forward(
    lab: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv = UI_ARGV.replace(KUBECONFIG, str(lab.absolute()))
    owners = {
        8790: PortOwner(
            8790, 4242, argv, ProcessInfo(77, "/v/bin/python /v/bin/piceli access ui")
        ),
        18795: PortOwner(18795, 500, "nginx: master process"),
    }
    held = {8790, 18795}
    signalled: list[object] = []

    def terminate(pid: object) -> bool:
        signalled.append(pid)
        held.discard(8790)
        return True

    def stop(ports: Any, check: Any) -> Any:
        return stop_stale(
            ports,
            check,
            in_use=lambda port: port in held,
            owner=owners.get,
            terminate=terminate,
            wait_seconds=1,
        )

    monkeypatch.setattr(cli_access, "stop_stale", stop)
    result = CliRunner().invoke(
        app,
        ["access", "stop", "--stale", "--profile", "my-cluster", "--port", "18795"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    body = json.loads(result.stdout)
    assert (body["app"], body["namespace"]) == ("ui", "piceli-system")
    assert [(item["port"], item["pid"]) for item in body["stopped"]] == [(8790, 77)]
    assert [item["port"] for item in body["left"]] == [18795]
    assert body["left"][0]["owner"]["command"] is None
    assert signalled == [77]


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["access", "stop", "--stale"], "access-target-invalid"),
        (
            ["access", "stop", "--stale", "release.toml", "--profile", "my-cluster"],
            "access-ui-target-required",
        ),
        (["access", "stop", "--profile", "my-cluster"], "access-stop-needs-stale"),
    ],
)
def test_access_stop_ui_refusals(
    lab: Path,  # noqa: F811
    argv: list[str],
    code: str,
) -> None:
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == code


# ------------------------------------------------- real processes, no cluster


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or shutil.which("lsof")),
    reason="needs /proc or lsof to find a port's owner",
)
def test_a_stale_access_ui_forward_is_recognised_and_stopped(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(f"#!{sys.executable}\n{_FAKE_KUBECTL}")
    kubectl.chmod(0o700)
    piceli = bin_dir / "piceli"
    piceli.write_text(f"#!{sys.executable}\n{_FAKE_PICELI}")
    piceli.chmod(0o700)
    kubeconfig = tmp_path / "lab.kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n")
    registry = OwnedProcessRegistry(tmp_path / "state" / "forwards")
    port, foreign_port = _free_port(), _free_port()
    child = 0
    env = {
        "FAKE_UI_PORT": str(port),
        "FAKE_UI_KUBECONFIG": str(kubeconfig),
        "FAKE_UI_KUBECTL": str(kubectl),
        "FAKE_UI_REGISTRY": str(registry.directory),
    }
    try:
        with (
            _process(
                [sys.executable, str(piceli), "access", "ui", "--profile", "lab"], env
            ) as owner,
            _process([sys.executable, "-c", _LISTENER, str(foreign_port)]) as foreign,
        ):
            assert owner.stdout is not None and foreign.stdout is not None
            child = int(owner.stdout.readline())
            assert foreign.stdout.readline().strip() == "ready"
            assert _accepting(port)
            recorded = registry.lookup(child)
            assert recorded is not None and recorded.owner_pid == owner.pid
            assert recorded.owner_alive
            assert recorded.label == forward_label(
                str(kubeconfig), "lab", "piceli-system", "service/piceli-ui", port, 8790
            )
            assert registry.lookup(foreign.pid) is None

            # Without the registry the fake kubectl's argv proves nothing.
            bare = ui_holder_check(kubeconfig, "lab", port=port)
            shortcut = UiShortcut(
                id="ui",
                label="Piceli UI",
                target="service/piceli-ui",
                namespace="piceli-system",
                local_port=port,
                remote_port=8790,
            )
            (unknown,) = port_conflicts([shortcut], recognise_owner=bare)
            assert unknown.holder is not None and unknown.holder.kind == OTHER
            # Another cluster's credentials: not this forward either.
            other = ui_holder_check(kubeconfig, "other", registry=registry, port=port)
            (theirs,) = port_conflicts([shortcut], recognise_owner=other)
            assert theirs.holder is not None and theirs.holder.kind == OTHER

            check = ui_holder_check(kubeconfig, "lab", registry=registry, port=port)
            (conflict,) = port_conflicts([shortcut], recognise_owner=check)
            assert conflict.holder is not None
            assert conflict.holder.kind == PICELI_FORWARD
            assert conflict.holder.stop == owner.pid
            assert "(not piceli)" not in conflict.describe()

            result = stop_stale([port, foreign_port], check, wait_seconds=10)
            assert [(item["port"], item["pid"]) for item in result["stopped"]] == [
                (port, owner.pid)
            ]
            assert [item["port"] for item in result["left"]] == [foreign_port]
            assert result["failed"] == []
            owner.wait(timeout=10)
            deadline = time.monotonic() + 5
            while (_alive(child) or _accepting(port)) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not _accepting(port) and not _alive(child)
            # Never another process: the foreign listener still runs.
            assert foreign.poll() is None and _accepting(foreign_port)
            assert list(registry.directory.glob("*.json")) == []
    finally:
        if child and _alive(child):
            with suppress(ProcessLookupError):
                os.killpg(child, signal.SIGKILL)
