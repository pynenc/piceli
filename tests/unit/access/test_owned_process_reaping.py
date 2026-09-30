"""Forwards left by a SIGKILLed server are reaped at the next start; nothing else is."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

from piceli.k8s.owned_processes import OwnedProcessRegistry, process_identity
from piceli.k8s.ui_state import private_ui_state_dir

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="process groups and SIGKILL are POSIX"
)

REPO = Path(__file__).resolve().parents[3]

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

_OWNER = """import socket
import sys
import time
from pathlib import Path

from piceli.k8s.observe import ForwardSupervisor
from piceli.k8s.owned_processes import OwnedProcessRegistry
from piceli.k8s.ui_config import UiShortcut

kubectl, registry, kubeconfig, port = sys.argv[1:5]
supervisor = ForwardSupervisor(
    kubeconfig=Path(kubeconfig),
    context="fake",
    kubectl=kubectl,
    shortcuts=(
        UiShortcut(
            id="ui-demo",
            label="Local access",
            target="service/api",
            namespace="shop",
            local_port=int(port),
            remote_port=8080,
        ),
    ),
    namespace="shop",
    registry=OwnedProcessRegistry(Path(registry)),
)
supervisor.quick_start("ui-demo", "shop")
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", int(port))) == 0:
            break
    time.sleep(0.05)
(status,) = supervisor.statuses()
print(status.pid, flush=True)
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
def _process(argv: list[str]) -> Iterator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        argv,
        cwd=REPO,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        yield process
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()


def test_a_sigkilled_servers_forward_is_reaped_at_the_next_start(
    tmp_path: Path,
) -> None:
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(f"#!{sys.executable}\n{_FAKE_KUBECTL}")
    kubectl.chmod(0o700)
    state = private_ui_state_dir(tmp_path / "state")
    assert state.stat().st_mode & 0o777 == 0o700
    registry = state / "forwards"
    port = _free_port()
    child = 0
    try:
        with _process(
            [
                sys.executable,
                "-c",
                _OWNER,
                str(kubectl),
                str(registry),
                str(tmp_path / "kubeconfig"),
                str(port),
            ]
        ) as owner:
            assert owner.stdout is not None
            child = int(owner.stdout.readline())
            assert _accepting(port)
            assert (registry / f"{child}.json").stat().st_mode & 0o777 == 0o600
            owner.send_signal(signal.SIGKILL)
            owner.wait(timeout=5)
        # The crashed server's forward keeps running with nobody to stop it.
        assert _alive(child)
        assert _accepting(port)

        assert OwnedProcessRegistry(registry).reap_orphans() == (child,)
        deadline = time.monotonic() + 5
        while _accepting(port) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _accepting(port)
        assert not _alive(child)
        assert list(registry.glob("*.json")) == []
    finally:
        if child and _alive(child):
            with suppress(ProcessLookupError):
                os.killpg(child, signal.SIGKILL)


def test_a_reused_pid_or_a_live_owners_child_is_never_signalled(
    tmp_path: Path,
) -> None:
    registry = OwnedProcessRegistry(tmp_path / "forwards")
    port = _free_port()
    with _process([sys.executable, "-c", _LISTENER, str(port)]) as foreign:
        assert foreign.stdout is not None
        assert foreign.stdout.readline().strip() == "ready"
        identity = process_identity(foreign.pid)
        assert identity is not None
        registry.directory.mkdir(mode=0o700)
        # A dead owner's record whose pid now names another process (reuse).
        (registry.directory / f"{foreign.pid}.json").write_text(
            json.dumps(
                {
                    "pid": foreign.pid,
                    "identity": "0" * 64,
                    "owner_pid": 2**22 + 1,
                    "owner_identity": "0" * 64,
                }
            )
        )
        assert registry.reap_orphans() == ()
        assert foreign.poll() is None and _accepting(port)
        assert list(registry.directory.glob("*.json")) == []

        # The exact process, but its owner is still running: not an orphan.
        (registry.directory / f"{foreign.pid}.json").write_text(
            json.dumps(
                {
                    "pid": foreign.pid,
                    "identity": identity,
                    "owner_pid": os.getpid(),
                    "owner_identity": process_identity(os.getpid()),
                }
            )
        )
        assert OwnedProcessRegistry(registry.directory).reap_orphans() == ()
        assert foreign.poll() is None and _accepting(port)
        assert (registry.directory / f"{foreign.pid}.json").exists()


def test_unreadable_records_are_dropped_without_signalling(tmp_path: Path) -> None:
    registry = OwnedProcessRegistry(tmp_path / "forwards")
    registry.directory.mkdir(mode=0o700)
    (registry.directory / "1.json").write_text("not json")
    (registry.directory / "2.json").write_text(
        json.dumps({"pid": 1, "identity": "x", "owner_pid": 3, "owner_identity": "y"})
    )
    assert registry.reap_orphans() == ()
    assert list(registry.directory.iterdir()) == []


def test_state_dir_refuses_a_symlink(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(ValueError):
        private_ui_state_dir(tmp_path / "link")


def test_ui_serve_reaps_orphans_before_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn
    from typer.testing import CliRunner

    from piceli.k8s.cli import app

    reaped: list[Path] = []

    def reap(self: OwnedProcessRegistry, **_kwargs: object) -> tuple[int, ...]:
        reaped.append(self.directory)
        return ()

    served: list[object] = []
    monkeypatch.setattr(OwnedProcessRegistry, "reap_orphans", reap)
    monkeypatch.setattr(uvicorn, "run", lambda server, **_kwargs: served.append(server))
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n")
    result = CliRunner().invoke(
        app,
        [
            "ui",
            "serve",
            "--kubeconfig",
            str(kubeconfig),
            "--context",
            "fake",
            "--namespace",
            "shop",
            "--state-dir",
            str(tmp_path / "state"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert reaped == [tmp_path / "state" / "forwards"]
    assert len(served) == 1
    assert (tmp_path / "state").stat().st_mode & 0o777 == 0o700
