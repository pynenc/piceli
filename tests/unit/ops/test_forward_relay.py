"""0.19: a relayed forward that moves bytes is busy, never restarted (no cluster).

``ForwardSupervisor(relay=True)`` holds each local port in a byte-counting
relay (:mod:`piceli.k8s.forward_relay`) in front of the supervised
"kubectl", here the same tiny local proxy script as in
``test_forward_health``. The upstream serves a slow download while its
health endpoint hangs: the probe times out over and over, and the forward
must stay up until the download completes.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

import pytest

from piceli.k8s.forward_relay import PortRelay
from piceli.k8s.observe import ForwardStatus, ForwardSupervisor
from piceli.k8s.ui_config import HealthProbe, RestartPolicy, UiShortcut
from tests.ports import worker_port
from tests.unit.ops.test_forward_health import FAKE_KUBECTL

CHUNKS, CHUNK_DELAY = 50, 0.1  # a 5-second download


def _wait_for(predicate: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class _Slow(BaseHTTPRequestHandler):
    """``/download`` streams slowly; ``/healthz`` hangs while it runs."""

    downloading = threading.Event()

    def do_GET(self) -> None:
        if self.path == "/download":
            self.send_response(200)
            self.send_header("Content-Length", str(CHUNKS * 1024))
            self.end_headers()
            _Slow.downloading.set()
            try:
                # Pause before each chunk, not after: the health endpoint must
                # answer again as soon as the client has the last byte (a
                # pause after it left a closed, idle forward whose probe
                # still hung, and a loaded runner restarted it).
                for _ in range(CHUNKS):
                    time.sleep(CHUNK_DELAY)
                    self.wfile.write(b"x" * 1024)
                    self.wfile.flush()
            finally:
                _Slow.downloading.clear()
            return
        while _Slow.downloading.is_set():  # a saturated link: no answer
            time.sleep(0.05)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def slow_upstream() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", worker_port()), _Slow)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    _Slow.downloading.clear()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def fake_kubectl(tmp_path: Path) -> Path:
    script = tmp_path / "fake-kubectl"
    script.write_text(f"#!{sys.executable}\n{FAKE_KUBECTL}")
    script.chmod(0o755)
    return script


def _status(supervisor: ForwardSupervisor, item: str) -> ForwardStatus:
    return next(s for s in supervisor.statuses() if s.name == item)


def _supervisor(
    tmp_path: Path, kubectl: Path, remote: int, failure_threshold: int = 1
) -> ForwardSupervisor:
    shortcut = UiShortcut(
        id="api",
        label="API",
        target="service/api",
        local_port=worker_port(),
        remote_port=remote,
        health=HealthProbe(
            type="http",
            path="/healthz",
            # The probe hangs for the whole download, so it fails there with
            # any timeout; 1 s lets an idle probe answer on a loaded runner
            # (0.2 s timed some out and restarted an idle forward).
            interval=0.3,
            timeout=1.0,
            failure_threshold=failure_threshold,
            startup_grace=5.0,
        ),
        restart=RestartPolicy(backoff_initial=0.1, backoff_max=0.5, max_restarts=20),
    )
    return ForwardSupervisor(
        kubeconfig=tmp_path / "kubeconfig",
        context="lab",
        kubectl=str(kubectl),
        shortcuts=[shortcut],
        namespace="demo",
        relay=True,
    )


def test_the_relay_passes_bytes_on_counts_them_and_survives_a_new_target() -> None:
    def echo_server() -> tuple[socket.socket, int]:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)

        def serve() -> None:
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                with conn:
                    while data := conn.recv(65536):
                        conn.sendall(data)

        threading.Thread(target=serve, daemon=True).start()
        return listener, int(listener.getsockname()[1])

    first, first_port = echo_server()
    second, second_port = echo_server()
    relay = PortRelay(worker_port()).start()
    try:
        assert relay.busy() is False
        relay.target = first_port
        with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as conn:
            conn.sendall(b"hello")
            assert conn.recv(5) == b"hello"
            assert relay.busy() is True  # open and moving bytes
        assert _wait_for(lambda: relay.active == 0)
        assert relay.busy() is False
        assert relay.stats() == {"active_connections": 0, "bytes": 10, "busy": False}
        relay.target = second_port  # kubectl restarted on another inner port
        with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as conn:
            conn.sendall(b"again")
            assert conn.recv(5) == b"again"
        assert relay.connections == 2
    finally:
        relay.close()
        first.close()
        second.close()
    with socket.socket() as probe:  # closing the relay stops listening
        assert probe.connect_ex(("127.0.0.1", relay.port)) != 0


def test_a_busy_forward_is_not_restarted_while_its_probe_times_out(
    tmp_path: Path, fake_kubectl: Path, slow_upstream: ThreadingHTTPServer
) -> None:
    # Two failures in a row restart the forward: a single idle probe that
    # timed out on a loaded runner restarted it as the download began, and
    # cut it. A probe behind moving bytes reads busy whatever the threshold.
    supervisor = _supervisor(
        tmp_path, fake_kubectl, slow_upstream.server_port, failure_threshold=2
    )
    try:
        supervisor.quick_start("api")
        assert _wait_for(lambda: _status(supervisor, "api").health == "healthy")
        local = _status(supervisor, "api").local_port
        pid = _status(supervisor, "api").pid
        seen: set[str] = set()
        received: list[int] = []

        def download() -> None:
            with urlopen(f"http://127.0.0.1:{local}/download", timeout=30) as reply:
                received.append(len(reply.read()))

        worker = threading.Thread(target=download)
        worker.start()
        while worker.is_alive():
            seen.add(_status(supervisor, "api").health)
            time.sleep(0.05)
        worker.join()
        assert received == [CHUNKS * 1024], "the download was cut"
        assert "busy" in seen, seen
        status = _status(supervisor, "api")
        assert status.restarts == 0 and status.restart_reasons == ()
        assert status.pid == pid
        assert status.traffic is not None and status.traffic["bytes"] >= CHUNKS * 1024
        assert _wait_for(lambda: _status(supervisor, "api").health == "healthy")
    finally:
        supervisor.close()


def test_a_restart_is_reported_with_its_reason(
    tmp_path: Path, fake_kubectl: Path, slow_upstream: ThreadingHTTPServer
) -> None:
    supervisor = _supervisor(tmp_path, fake_kubectl, slow_upstream.server_port)
    try:
        supervisor.quick_start("api")
        assert _wait_for(lambda: _status(supervisor, "api").health == "healthy")
        local = _status(supervisor, "api").local_port
        _Slow.downloading.set()  # the health endpoint hangs, nothing moves
        assert _wait_for(lambda: _status(supervisor, "api").restarts >= 1)
        reasons = _status(supervisor, "api").restart_reasons
        assert reasons and "health probe failed" in reasons[-1]["reason"]
        assert reasons[-1]["at"]
        row = next(
            item for item in supervisor.shortcuts_status() if item["id"] == "api"
        )
        assert row["restart_reasons"] == list(reasons)
        # The relay kept the local port across the restart.
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", local)) == 0
    finally:
        _Slow.downloading.clear()
        supervisor.close()


def test_a_probe_that_waited_while_bytes_moved_is_not_a_failure(
    tmp_path: Path, fake_kubectl: Path
) -> None:
    """The transfer ended just before the probe timed out: the probe waited
    behind it, which says nothing about the forward being broken."""
    from types import SimpleNamespace

    supervisor = _supervisor(tmp_path, fake_kubectl, worker_port())
    try:
        managed = supervisor._managed(supervisor._shortcut_forward("api"))
        managed.relay = SimpleNamespace(busy=lambda: False, last_byte=100.0)  # type: ignore[assignment]
        with supervisor._lock:
            supervisor._record_probe_locked(managed, "timed out", since=99.0)
        assert managed.health == "busy" and managed.restart_history == []
        with supervisor._lock:  # nothing moved since this probe started
            supervisor._record_probe_locked(managed, "timed out", since=101.0)
        # A failure (threshold 1): the forward is restarted.
        assert [item["reason"] for item in managed.restart_history] == [
            "health probe failed 1x: timed out"
        ]
    finally:
        supervisor.close()
