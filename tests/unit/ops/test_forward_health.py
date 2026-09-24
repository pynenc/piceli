"""Access profiles: health-probed, bounded port-forward supervision (no cluster).

The supervised "kubectl" here is a tiny local TCP proxy script: it parses the
``LOCAL:REMOTE`` argument of the real port-forward argv and relays
``127.0.0.1:LOCAL`` to ``127.0.0.1:REMOTE``. Like ``kubectl port-forward``
it keeps running when its upstream dies and just drops every new
connection, which is exactly the failure the health probe must catch.
"""

from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from piceli.k8s.cli.observe import app
from piceli.k8s.observe import (
    ForwardStatus,
    ForwardSupervisor,
    InventoryReport,
    PreferenceStore,
    local_port_in_use,
    preflight_shortcuts,
    probe_endpoint,
)
from piceli.k8s.observe_server import LocalObserveServer
from piceli.k8s.ui_config import (
    HealthProbe,
    RestartPolicy,
    UiShortcut,
    legacy_health,
    load_access_profile,
    load_ui_config,
)

REPO = Path(__file__).resolve().parents[3]

FAKE_KUBECTL = textwrap.dedent(
    """\
    import re, socket, sys, threading

    ports = next(a for a in sys.argv[1:] if re.fullmatch(r"\\d+:\\d+", a))
    local, remote = (int(p) for p in ports.split(":"))

    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def handle(client):
        try:
            upstream = socket.create_connection(("127.0.0.1", remote), timeout=2)
        except OSError:
            client.close()  # like kubectl: accept locally, drop on upstream error
            return
        threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
        pump(upstream, client)

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", local))
    listener.listen(64)
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()
    """
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class _Healthz(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self) -> None:
        self.send_response(self.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args: object) -> None:
        pass


class Upstream:
    """A restartable local HTTP server standing in for the in-cluster service."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), _Healthz)
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.start()

    def kill(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            assert self._thread is not None
            self._thread.join(timeout=5)
            self._server = None


@pytest.fixture
def fake_kubectl(tmp_path: Path) -> Path:
    script = tmp_path / "fake-kubectl"
    script.write_text(f"#!{sys.executable}\n{FAKE_KUBECTL}")
    script.chmod(0o755)
    return script


@pytest.fixture
def upstream() -> Iterator[Upstream]:
    server = Upstream(_free_port())
    server.start()
    yield server
    server.kill()


# ------------------------------------------------------------------ models --


def test_health_probe_and_restart_policy_validate() -> None:
    probe = HealthProbe(type="http", path="/healthz", expect_status=(200, 204))
    assert probe.public_dict()["expect_status"] == [200, 204]
    assert "path" not in HealthProbe().public_dict()
    with pytest.raises(ValidationError):
        HealthProbe(expect_status=(500, 200))
    with pytest.raises(ValidationError):
        HealthProbe(path="relative")
    with pytest.raises(ValidationError):
        HealthProbe(failure_threshold=0)
    with pytest.raises(ValidationError):
        RestartPolicy(backoff_initial=5, backoff_max=1)
    policy = RestartPolicy(backoff_initial=1, backoff_max=5)
    assert [policy.delay(n) for n in (1, 2, 3, 4, 5)] == [1, 2, 4, 5, 5]


def test_shortcut_health_path_is_legacy_shorthand() -> None:
    base = {
        "id": "web",
        "label": "Web",
        "target": "service/web",
        "local_port": 3000,
        "remote_port": 80,
    }
    assert UiShortcut(**base).probe == HealthProbe()
    legacy = UiShortcut(**base, health_path="/")
    assert legacy.probe == legacy_health("/")
    assert legacy.probe.expect_status == (200, 499)
    with pytest.raises(ValidationError, match="either health_path"):
        UiShortcut(**base, health_path="/", health=HealthProbe())
    with pytest.raises(ValidationError):
        UiShortcut(**base, health={"type": "grpc"})


def test_access_profile_is_the_ui_config_format(tmp_path: Path) -> None:
    profile = tmp_path / "access.toml"
    profile.write_text(
        textwrap.dedent(
            """
            [[shortcuts]]
            id = "api"
            label = "API"
            target = "service/api"
            namespace = "demo"
            local_port = 18080
            remote_port = 8000
            required = false

              [shortcuts.health]
              type = "http"
              path = "/healthz"
              expect_status = [200, 299]
              interval = 2.0
              failure_threshold = 2

              [shortcuts.restart]
              max_restarts = 3
            """
        )
    )
    config = load_access_profile(profile)
    assert config == load_ui_config(profile)
    shortcut = config.shortcuts[0]
    assert shortcut.required is False
    assert shortcut.probe.path == "/healthz"
    assert shortcut.probe.failure_threshold == 2
    assert shortcut.restart.max_restarts == 3


def test_shipped_example_profile_loads() -> None:
    config = load_ui_config(REPO / "examples" / "ui-config.toml")
    probes = {shortcut.id: shortcut.probe for shortcut in config.shortcuts}
    assert probes["api"].type == "http"


# ------------------------------------------------------------------ probes --


def test_tcp_probe_detects_a_forward_that_drops_connections() -> None:
    port = _free_port()
    assert probe_endpoint(port, HealthProbe()) == "connection refused"

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    stop = threading.Event()

    def accept_and_close() -> None:
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            conn.close()

    thread = threading.Thread(target=accept_and_close)
    thread.start()
    try:
        outcome = probe_endpoint(listener.getsockname()[1], HealthProbe(settle=1.0))
        assert outcome in {"connection closed by forward", "connection reset"}
    finally:
        stop.set()
        thread.join()
        listener.close()


def test_http_probe_checks_the_expected_status_range(upstream: Upstream) -> None:
    ok = HealthProbe(type="http", path="/healthz", expect_status=(200, 299))
    assert probe_endpoint(upstream.port, ok) is None
    assert probe_endpoint(upstream.port, HealthProbe(settle=0.1)) is None
    strict = HealthProbe(type="http", expect_status=(204, 204))
    assert probe_endpoint(upstream.port, strict) == "http status 200 outside 204-204"


# --------------------------------------------------------------- preflight --


def _shortcut(sid: str, port: int, **extra: object) -> UiShortcut:
    return UiShortcut.model_validate(
        {
            "id": sid,
            "label": sid,
            "target": f"service/{sid}",
            "local_port": port,
            "remote_port": 80,
            **extra,
        }
    )


def test_port_conflict_preflight_is_a_clear_error() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    busy = listener.getsockname()[1]
    try:
        assert local_port_in_use(busy)
        free = _free_port()
        assert not local_port_in_use(free)
        plan = preflight_shortcuts(
            [
                _shortcut("web", free),
                _shortcut("busy", busy),
                _shortcut("optional", busy + 0, required=False, namespace="x"),
                _shortcut("nons", _free_port()),
            ],
            namespace=None,
        )
    finally:
        listener.close()
    assert plan.start == ()
    joined = "\n".join(plan.errors)
    assert "web: no namespace" in joined
    assert "nons: no namespace" in joined
    assert "optional: local port" in joined  # duplicate declaration of `busy`

    plan = preflight_shortcuts(
        [
            _shortcut("web", 1111),
            _shortcut("busy", 2222),
            _shortcut("optional", 3333, required=False),
        ],
        namespace="demo",
        in_use=lambda port: port in {2222, 3333},
    )
    assert plan.start == ("web",)
    assert plan.external == ("optional",)
    assert plan.errors == (
        "busy: local port 2222 is already in use by another process; stop it or "
        "change local_port",
    )


def test_supervisor_reports_conflict_without_spawning(
    tmp_path: Path, fake_kubectl: Path
) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    supervisor = ForwardSupervisor(
        kubeconfig=tmp_path / "kubeconfig",
        kubectl=str(fake_kubectl),
        shortcuts=[_shortcut("web", port)],
        namespace="demo",
    )
    try:
        supervisor.quick_start("web")
        status = supervisor.statuses()[0]
        assert status.health == "conflict"
        assert status.pid is None
        assert status.restarts == 0
        assert "occupied" in (status.error or "")
    finally:
        supervisor.close()
        listener.close()


# --------------------------------------------------------------- supervisor --


def _status(supervisor: ForwardSupervisor, name: str) -> ForwardStatus:
    return next(item for item in supervisor.statuses() if item.name == name)


def test_killed_upstream_restarts_forward_within_one_probe_interval(
    tmp_path: Path, fake_kubectl: Path, upstream: Upstream
) -> None:
    interval, timeout = 1.0, 0.5
    shortcut = UiShortcut(
        id="api",
        label="API",
        target="service/api",
        local_port=_free_port(),
        remote_port=upstream.port,
        health=HealthProbe(
            type="http",
            path="/healthz",
            interval=interval,
            timeout=timeout,
            failure_threshold=1,
            startup_grace=5.0,
        ),
        restart=RestartPolicy(backoff_initial=0.2, backoff_max=1.0, max_restarts=20),
    )
    supervisor = ForwardSupervisor(
        kubeconfig=tmp_path / "kubeconfig",
        kubectl=str(fake_kubectl),
        shortcuts=[shortcut],
        namespace="demo",
    )
    try:
        supervisor.quick_start("api")
        assert _wait_for(lambda: _status(supervisor, "api").health == "healthy")
        healthy = _status(supervisor, "api")
        assert healthy.state == "running"
        assert healthy.reachable is True
        assert healthy.last_probe_at is not None
        first_pid = healthy.pid
        assert first_pid is not None

        # The forward process stays alive, but every connection now fails.
        upstream.kill()
        killed_at = time.monotonic()
        assert _wait_for(lambda: _status(supervisor, "api").restarts >= 1)
        elapsed = time.monotonic() - killed_at
        # Next due probe (<= interval) + its own timeout + one supervisor tick.
        assert elapsed <= interval + timeout + 0.5, elapsed
        restarted = _status(supervisor, "api")
        assert restarted.health in {"restarting", "starting"}
        assert "health probe failed" in (restarted.last_error or "")
        assert _wait_for(lambda: not _pid_alive_or_zombie(first_pid), timeout=5)

        # Once the upstream is back, a fresh forward process becomes healthy.
        upstream.start()
        assert _wait_for(lambda: _status(supervisor, "api").health == "healthy")
        assert _status(supervisor, "api").pid not in {None, first_pid}
    finally:
        supervisor.close()
    assert all(item.pid is None for item in supervisor.statuses())


def _pid_alive_or_zombie(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    )
    stat = result.stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def test_restarts_are_bounded(tmp_path: Path) -> None:
    exits = tmp_path / "exits"
    exits.write_text(f"#!{sys.executable}\nimport sys; sys.exit(3)\n")
    exits.chmod(0o755)
    shortcut = _shortcut(
        "web",
        _free_port(),
        restart={"backoff_initial": 0.05, "backoff_max": 0.1, "max_restarts": 2},
    )
    supervisor = ForwardSupervisor(
        kubeconfig=tmp_path / "kubeconfig",
        kubectl=str(exits),
        shortcuts=[shortcut],
        namespace="demo",
    )
    try:
        supervisor.quick_start("web")
        assert _wait_for(lambda: _status(supervisor, "web").state == "failed")
        status = _status(supervisor, "web")
        assert status.health == "failed"
        assert status.restarts == 2
        assert (status.error or "").startswith("gave up after 2 restarts")
        # An explicit start resets the budget.
        supervisor.quick_start("web")
        assert _status(supervisor, "web").state != "failed"
    finally:
        supervisor.close()


def test_rest_status_exposes_health(
    tmp_path: Path, fake_kubectl: Path, upstream: Upstream
) -> None:
    shortcut = _shortcut(
        "api",
        _free_port(),
        health={"type": "tcp", "interval": 0.5, "startup_grace": 5},
    )
    shortcut = shortcut.model_copy(update={"remote_port": upstream.port})
    supervisor = ForwardSupervisor(
        preferences=PreferenceStore(tmp_path / "observe.json"),
        user="tester",
        kubeconfig=tmp_path / "kubeconfig",
        kubectl=str(fake_kubectl),
        shortcuts=[shortcut],
        namespace="demo",
    )
    server = LocalObserveServer(
        ("127.0.0.1", 0),
        lambda: InventoryReport("a" * 32, (), ()),
        PreferenceStore(tmp_path / "observe.json"),
        supervisor,
        "tester",
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    def get(path: str) -> dict[str, list[dict[str, object]]]:
        request = Request(
            f"http://127.0.0.1:{server.server_port}{path}",
            headers={"X-Piceli-Local-Token": server.local_token},
        )
        with urlopen(request) as response:
            value: dict[str, list[dict[str, object]]] = json.load(response)
            return value

    try:
        supervisor.quick_start("api")
        assert _wait_for(lambda: _status(supervisor, "api").health == "healthy")
        forwards = get("/v1/forwards")["forwards"]
        assert forwards[0]["health"] == "healthy"
        assert forwards[0]["probe"] == {
            "type": "tcp",
            "interval": 0.5,
            "timeout": 2.0,
            "failure_threshold": 3,
        }
        assert forwards[0]["last_probe_at"]
        shortcuts = get("/v1/shortcuts")["shortcuts"]
        assert shortcuts[0]["health"] == "healthy"
        assert shortcuts[0]["required"] is True
        assert "restarts" in shortcuts[0] and "last_error" in shortcuts[0]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        supervisor.close()


def test_dashboard_page_renders_health_without_inline_handlers() -> None:
    from piceli.k8s.observe_server import _PAGE_HTML

    assert "function healthBadge" in _PAGE_HTML
    assert "health-fwd-" in _PAGE_HTML
    assert " onclick=" not in _PAGE_HTML


# --------------------------------------------------------------------- CLI --


def test_cli_apply_refuses_an_occupied_required_port(tmp_path: Path) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    profile = tmp_path / "access.toml"
    profile.write_text(
        f'[[shortcuts]]\nid = "web"\nlabel = "Web"\ntarget = "service/web"\n'
        f"local_port = {port}\nremote_port = 80\n"
    )
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("{}")
    try:
        result = CliRunner().invoke(
            app,
            [
                "forwards",
                "apply",
                "--profile",
                str(profile),
                "--kubeconfig",
                str(kubeconfig),
                "--namespace",
                "demo",
                "--kubectl",
                "/nonexistent/kubectl",
            ],
        )
    finally:
        listener.close()
    assert result.exit_code == 2
    assert "already in use" in result.output


def test_cli_status_probes_declared_endpoints(
    tmp_path: Path, upstream: Upstream
) -> None:
    down = _free_port()
    profile = tmp_path / "access.toml"
    profile.write_text(
        textwrap.dedent(
            f"""
            [[shortcuts]]
            id = "up"
            label = "Up"
            target = "service/up"
            local_port = {upstream.port}
            remote_port = 80
            health = {{ type = "http", path = "/healthz" }}

            [[shortcuts]]
            id = "down"
            label = "Down"
            target = "service/down"
            local_port = {down}
            remote_port = 80
            required = false
            """
        )
    )
    result = CliRunner().invoke(app, ["forwards", "status", "--profile", str(profile)])
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    by_id = {item["id"]: item for item in value["forwards"]}
    assert value["ok"] is True
    assert by_id["up"]["health"] == "healthy"
    assert by_id["down"]["health"] == "unhealthy"
    assert by_id["down"]["error"] == "connection refused"


def test_cli_apply_supervises_until_interrupted(
    tmp_path: Path, fake_kubectl: Path, upstream: Upstream
) -> None:
    local = _free_port()
    profile = tmp_path / "access.toml"
    profile.write_text(
        textwrap.dedent(
            f"""
            [[shortcuts]]
            id = "api"
            label = "API"
            target = "service/api"
            local_port = {local}
            remote_port = {upstream.port}

              [shortcuts.health]
              type = "http"
              path = "/healthz"
              interval = 0.5
            """
        )
    )
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("{}")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "piceli",
            "observe",
            "forwards",
            "apply",
            "--profile",
            str(profile),
            "--kubeconfig",
            str(kubeconfig),
            "--namespace",
            "demo",
            "--kubectl",
            str(fake_kubectl),
            "--poll",
            "0.2",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO,
    )
    events: list[dict[str, object]] = []
    try:
        assert process.stdout is not None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            events.append(json.loads(line))
            if events[-1].get("health") == "healthy":
                break
        assert events[0] == {"event": "started", "forwards": ["api"], "external": []}
        assert events[-1]["health"] == "healthy", events
        assert local_port_in_use(local)
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 0, err
    assert json.loads(out.strip().splitlines()[-1]) == {"event": "stopped"}
    assert _wait_for(lambda: not local_port_in_use(local), timeout=5)
