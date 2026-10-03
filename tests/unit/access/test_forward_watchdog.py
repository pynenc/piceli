"""A killed ``piceli access`` leaves no ``kubectl port-forward`` behind (0.14.7).

Reproduces the 0.14.6 gap: ``piceli access ui`` killed with SIGKILL left its
``kubectl`` holding 127.0.0.1:8790 (the forwards run in their own session, so
nothing stopped them). Real processes, no cluster and no real kubectl: a fake
``kubectl`` binds the forward's local port; the command under test runs in
its own session and is killed with SIGKILL (the parent only), then every
fake ``kubectl`` it started must be gone and its ports free. Everything a
test starts is killed when it ends.
"""

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

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="process groups and signals are POSIX"
)

REPO = Path(__file__).resolve().parents[3]

_FAKE_KUBECTL = """import os
import socket
import sys
import time
from pathlib import Path

args = sys.argv[1:]
port = int(args[args.index("port-forward") + 2].split(":", 1)[0])
Path(os.environ["FAKE_PIDS"], str(os.getpid())).write_text(str(port))
with socket.socket() as server:
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(8)
    while True:
        connection, _ = server.accept()
        connection.close()
"""

# A parent that starts one forward-like child in its own session, registers
# it with the watchdog, prints both pids, then waits to be killed.
_PARENT = """import subprocess
import sys
import time

from piceli.k8s.forward_watchdog import ParentWatchdog

watchdog = ParentWatchdog(grace=1.0)
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(300)"],
    start_new_session=True,
)
watchdog.add(child.pid)
print(child.pid, watchdog.pid, flush=True)
time.sleep(300)
"""

# `piceli access ENV --cluster infra.py:cluster --ui`, as run from a shell,
# with the cluster reads replaced (live Services, the UI's launch token).
_ACCESS = """import os
import sys

import piceli.k8s.cli.access as access
import piceli.k8s.cli.ui_forward as ui_forward
import piceli.k8s.env_access as env_access
from piceli.k8s.cli import app


def services(target):
    return [
        {"metadata": {"name": "web"}, "spec": {"ports": [{"port": 80}]}},
        {"metadata": {"name": "api"}, "spec": {"ports": [{"port": 8080}]}},
    ]


def no_reader(**_kwargs):
    raise RuntimeError("no cluster in this test")


env_access._live_services = services
access._workload_reader = no_reader
ui_forward._launch_reader = lambda kubeconfig, context: "launch-token-for-tests"
ui_forward.PORT = int(os.environ["FAKE_UI_PORT"])
sys.argv = ["piceli", *sys.argv[1:]]
app()
"""


# A composition whose cluster declares the in-cluster UI.
_INFRA = """from piceli.envs import Environment, Stack
from piceli.infra import Cluster, Component, Source, Ui

name = "shop"
cluster = Cluster(
    "my-cluster",
    api="https://127.0.0.1:6443",
    credentials="my-cluster",
    ui=Ui(access="forward"),
)
shop = Source("https://example.com/shop.git", name="shop")
web = Component("web", source=shop)
environments = [
    Environment(
        "main",
        namespace="shop-main",
        stack=Stack("full", [web]),
        cluster=cluster,
        follow={shop: "main"},
    )
]
"""


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _accepting(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")  # a zombie runs nothing


def _wait(condition: object, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():  # type: ignore[operator]
            return True
        time.sleep(0.1)
    return bool(condition())  # type: ignore[operator]


@contextmanager
def _session(
    argv: list[str], env: dict[str, str], cwd: Path = REPO
) -> Iterator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
        env=env,
    )
    try:
        yield process
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()


def _kill(pids: list[int]) -> None:
    for pid in pids:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)


def test_the_watchdog_stops_registered_groups_when_its_parent_is_killed() -> None:
    pids: list[int] = []
    with _session([sys.executable, "-c", _PARENT], dict(os.environ)) as parent:
        assert parent.stdout is not None
        child, watchdog = (int(value) for value in parent.stdout.readline().split())
        pids += [child, watchdog]
        try:
            assert _alive(child) and _alive(watchdog)
            os.kill(parent.pid, signal.SIGKILL)  # the parent only
            parent.wait(timeout=10)
            assert _wait(lambda: not _alive(child), 10), "the child outlived its parent"
            assert _wait(lambda: not _alive(watchdog), 10), "the watchdog stayed"
        finally:
            _kill(pids)


def test_a_watchdog_closed_by_its_parent_signals_nothing() -> None:
    from piceli.k8s.forward_watchdog import ParentWatchdog

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        start_new_session=True,
    )
    try:
        watchdog = ParentWatchdog(grace=0.5)
        watchdog.add(child.pid)
        watchdog.remove(child.pid)  # stopped by its supervisor itself
        helper = watchdog.pid
        watchdog.close()
        assert helper is not None and not _alive(helper)
        assert child.poll() is None, "a forgotten group was signalled"
    finally:
        child.kill()
        child.wait(timeout=5)


def test_a_killed_access_with_ui_leaves_no_kubectl_and_frees_its_ports(
    tmp_path: Path,
) -> None:
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(f"#!{sys.executable}\n{_FAKE_KUBECTL}")
    kubectl.chmod(0o755)
    kubeconfig = tmp_path / "owner.kubeconfig"
    kubeconfig.write_text(
        "apiVersion: v1\nkind: Config\n"
        "clusters: [{name: c, cluster: {server: 'https://192.0.2.1'}}]\n"
        "users: [{name: u, user: {}}]\n"
        "contexts: [{name: owner, context: {cluster: c, user: u}}]\n"
    )
    pids_dir = tmp_path / "pids"
    pids_dir.mkdir()
    env = {
        **os.environ,
        "PICELI_PROFILES_DIR": str(tmp_path / "profiles"),
        "PICELI_SERVICE_ACCOUNT_DIR": str(tmp_path / "no-sa"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "KUBECONFIG": str(tmp_path / "no-such-kubeconfig"),
        "FAKE_PIDS": str(pids_dir),
        "FAKE_UI_PORT": str(_free_port()),
    }
    env.pop("PICELI_IN_CLUSTER", None)
    saved = subprocess.run(
        [sys.executable, "-m", "piceli", "login", "my-cluster", "--kubeconfig",
         str(kubeconfig), "--context", "owner"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert saved.returncode == 0, saved.stderr[-500:]
    (tmp_path / "infra.py").write_text(_INFRA)
    argv = [
        sys.executable, "-c", _ACCESS, "access", "main",
        "--cluster", "infra.py:cluster",
        "--ui", "--kubectl", str(kubectl), "--json",
    ]  # fmt: skip
    started: list[int] = []
    try:
        with _session(argv, env, cwd=tmp_path) as access:
            assert access.stdout is not None
            line = access.stdout.readline()
            event = json.loads(line)
            assert event.get("event") == "started", line
            assert event["ui"].startswith(f"http://127.0.0.1:{env['FAKE_UI_PORT']}/")
            ports = [item["local_port"] for item in event["forwards"]]
            assert int(env["FAKE_UI_PORT"]) in ports and len(ports) == 3
            assert _wait(lambda: all(_accepting(port) for port in ports), 20)
            started = [int(path.name) for path in pids_dir.iterdir()]
            assert len(started) == 3 and all(_alive(pid) for pid in started)

            os.kill(access.pid, signal.SIGKILL)  # piceli only, as a crash does
            access.wait(timeout=10)
            assert _wait(lambda: not any(_alive(pid) for pid in started), 15), (
                "a kubectl port-forward outlived its piceli"
            )
            assert _wait(lambda: not any(_accepting(port) for port in ports), 10)
    finally:
        _kill(started)
