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

from piceli.k8s import owned_processes as owned
from piceli.k8s.owned_processes import OwnedProcessRegistry, process_identity
from piceli.k8s.ui_state import private_ui_state_dir
from tests.ports import LISTENER, worker_port

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="process groups and SIGKILL are POSIX"
)

REPO = Path(__file__).resolve().parents[3]

# A healthy forward keeps its connections open: one that closed them would
# look like a dead upstream, and the supervisor would restart it (a new pid).
_FAKE_KUBECTL = """import socket
import sys

args = sys.argv[1:]
port = int(args[args.index("port-forward") + 2].split(":", 1)[0])
held = []
with socket.socket() as server:
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(8)
    while True:
        connection, _ = server.accept()
        held.append(connection)
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
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", int(port))) == 0:
            break
    time.sleep(0.05)
(status,) = supervisor.statuses()
print(status.pid, flush=True)
time.sleep(120)
"""


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
        process.wait(timeout=60)
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
    port = worker_port()
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
            owner.wait(timeout=60)
        # The crashed server's forward keeps running with nobody to stop it.
        assert _alive(child)
        assert _accepting(port)

        assert OwnedProcessRegistry(registry).reap_orphans() == (child,)
        # A hang guard, not a bound: on a busy runner the stopped forward may
        # take a moment to finish exiting after its socket is closed.
        deadline = time.monotonic() + 60
        while (_accepting(port) or _alive(child)) and time.monotonic() < deadline:
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
    with _process([sys.executable, "-c", LISTENER]) as foreign:
        assert foreign.stdout is not None
        port = int(foreign.stdout.readline())
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
    monkeypatch.setattr(
        uvicorn.Server, "run", lambda server, **_kwargs: served.append(server)
    )
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


def test_a_zombie_has_no_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reaped forward whose parent died stays a zombie until init waits for
    it (seen on CI runners): it has exited, so it is not a live process."""
    import piceli.k8s.owned_processes as owned

    monkeypatch.setattr(owned.sys, "platform", "linux")
    rest = " ".join(["0"] * 19) + " 12345 0 0"
    for pid, state in ((41, "S"), (42, "Z")):
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "stat").write_text(f"{pid} (kubectl) {state} {rest}")
        (tmp_path / str(pid) / "cmdline").write_bytes(b"kubectl\0port-forward\0")
    assert process_identity(41, proc=tmp_path) is not None
    assert process_identity(42, proc=tmp_path) is None


def _entry(identity: str = "recorded") -> owned._Entry:
    return owned._Entry(pid=4242, identity=identity, owner_pid=1, owner_identity="o")


@pytest.mark.parametrize(
    ("reads", "exists", "gone"),
    [
        (["recorded"], True, False),  # still the recorded process
        ([None], True, False),  # unreadable for a moment (exiting): wait
        ([None], False, True),  # the pid no longer exists, or is a zombie
        (["other", "other"], True, True),  # another process holds the pid
        (["other", "recorded"], True, False),  # a flicker, not a reuse
        (["other", None], True, False),  # changed, then unreadable: not proof
    ],
)
def test_a_process_is_gone_only_when_that_is_confirmed(
    monkeypatch: pytest.MonkeyPatch,
    reads: list[str | None],
    exists: bool,
    gone: bool,
) -> None:
    pending = list(reads)
    monkeypatch.setattr(owned, "process_identity", lambda pid: pending.pop(0))
    monkeypatch.setattr(owned, "_exists", lambda pid: exists)
    monkeypatch.setattr(owned.time, "sleep", lambda seconds: None)
    assert owned._gone(_entry()) is gone


def test_an_exiting_process_has_no_fingerprint(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    (proc / "77").mkdir(parents=True)
    fields = " ".join(["S", *["0"] * 19])
    (proc / "77" / "stat").write_text(f"77 (python) {fields}")
    (proc / "77" / "cmdline").write_bytes(b"python\0-c\0x\0")
    if not sys.platform.startswith("linux"):
        pytest.skip("the /proc reading is Linux's")
    assert process_identity(77, proc=proc) is not None
    (proc / "77" / "cmdline").write_bytes(b"")
    assert process_identity(77, proc=proc) is None


@pytest.mark.parametrize(
    ("reads", "exists", "identity"),
    [
        (["child"], True, "child"),
        # Just exec'd: its command line reads empty for a moment (0.26.1).
        ([None, None, "child"], True, "child"),
        ([None], False, None),  # it has already exited: nothing to record
    ],
)
def test_a_just_started_child_is_read_once_its_fingerprint_is_readable(
    monkeypatch: pytest.MonkeyPatch,
    reads: list[str | None],
    exists: bool,
    identity: str | None,
) -> None:
    pending = list(reads)
    monkeypatch.setattr(owned, "process_identity", lambda pid: pending.pop(0))
    monkeypatch.setattr(owned, "_exists", lambda pid: exists)
    monkeypatch.setattr(owned.time, "sleep", lambda seconds: None)
    assert owned._identity_once_readable(4242) == identity
    assert pending == []


def test_a_child_unreadable_right_after_its_start_is_still_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = OwnedProcessRegistry(tmp_path / "forwards")
    child = iter([None, "child", "child"])

    def identity(pid: int) -> str | None:
        return "owner" if pid == os.getpid() else next(child)

    monkeypatch.setattr(owned, "process_identity", identity)
    monkeypatch.setattr(owned, "_exists", lambda pid: True)
    monkeypatch.setattr(owned.time, "sleep", lambda seconds: None)
    registry.record(4242, label="sha256:forward")
    recorded = registry.lookup(4242)
    assert recorded is not None and recorded.owner_pid == os.getpid()
    assert recorded.owner_alive and recorded.label == "sha256:forward"
