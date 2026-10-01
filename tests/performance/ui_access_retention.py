"""Long-running local UI access retention fixture with no Kubernetes context.

The fake Kubernetes API supplies one Service. A temporary executable behaves
only like a loopback ``kubectl port-forward`` listener. Every access session is
started and stopped through the real AccessService and ForwardSupervisor.
The release gate runs for 1,800 seconds; ``--smoke`` is a short development run.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.services.access import AccessService
from piceli.services.contracts import AccessStartRequest
from piceli.services.query import QueryService
from piceli.services.registration import Registration
from piceli.testing import fake_cluster, manifest
from piceli.testing.fake_api import FakeCluster

_FAKE_FORWARD = """import socket
import sys

args = sys.argv[1:]
if "port-forward" not in args or "--address" not in args:
    sys.exit(2)
if args[args.index("--address") + 1] != "127.0.0.1":
    sys.exit(2)
port = int(args[args.index("port-forward") + 2].split(":", 1)[0])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(8)
    while True:
        connection, _ = server.accept()
        with connection:
            connection.settimeout(1)
            try:
                connection.recv(1)
            except TimeoutError:
                pass
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _fd_count() -> int | None:
    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(directory))
        except OSError:
            continue
    return None


def _rss_bytes() -> int | None:
    try:
        if sys.platform.startswith("linux"):
            pages = int(Path("/proc/self/statm").read_text().split()[1])
            return pages * os.sysconf("SC_PAGE_SIZE")
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return int(result.stdout.strip()) * 1024
    except (OSError, ValueError, subprocess.SubprocessError, IndexError):
        return None


def _access_threads() -> set[int | None]:
    return {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith(
            ("piceli-ui-access", "piceli-port-forward-", "piceli-forward-probe")
        )
    }


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _fake_kubectl(directory: Path) -> Path:
    path = directory / "kubectl-test-double"
    path.write_text(f"#!{sys.executable}\n{_FAKE_FORWARD}")
    path.chmod(0o700)
    return path


def _cycle(access: AccessService, request: AccessStartRequest) -> int:
    ready = access.start("shop", request)
    assert ready.state == "ready", ready
    assert ready.endpoint == f"127.0.0.1:{request.local_port}"
    # The process identifier comes from the real supervisor, not a mock.
    owned = access._sessions[ready.id]
    status = owned.supervisor.statuses()[0]
    assert status.pid is not None and status.state == "running", status
    pid = status.pid
    stopped = access.stop("shop", ready.id)
    assert stopped.state == "stopped" and stopped.endpoint is None
    assert not _pid_alive(pid), f"forward process {pid} survived stop"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", request.local_port))
    return pid


def _discard_fake_request_history(cluster: FakeCluster) -> None:
    # The test double records every HTTP request for assertion-oriented tests.
    # This soak does not inspect that history; keeping it would measure the
    # test server's unbounded journal instead of AccessService retention.
    api = cluster.api
    with api.lock:
        api.requests.clear()


def run(
    seconds: int,
    *,
    smoke: bool,
    session_goal: int | None = None,
    profile: bool = False,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="piceli-ui-access-retention-") as root:
        directory = Path(root)
        with fake_cluster() as cluster:
            service = manifest("Service", "api")
            service["spec"] = {
                "selector": {"app": "api"},
                "ports": [{"name": "http", "port": 8080, "targetPort": 8080}],
            }
            cluster.api.put(service)
            query = QueryService(
                [
                    Registration(
                        id="shop",
                        name="Shop",
                        target=KubeconfigTarget(
                            kubeconfig=cluster.kubeconfig(directory / "kubeconfig"),
                            context="fake",
                            namespace=cluster.namespace,
                            transport="loopback-http",
                        ),
                    )
                ]
            )
            access = AccessService(query, kubectl=_fake_kubectl(directory))
            selected = next(
                resource
                for resource in query.resources("shop").items
                if resource.identity.kind == "Service"
            )
            request = AccessStartRequest(
                resource_id=selected.id,
                resource_uid=selected.identity.uid or "",
                local_port=_free_port(),
                remote_port=8080,
            )
            before_threads = _access_threads()
            tracemalloc.start()
            try:
                # Warm import, HTTP client, subprocess and probe caches before
                # recording steady memory. The stopped history is bounded by
                # production code, not by this fixture.
                for _ in range(4):
                    _cycle(access, request)
                    _discard_fake_request_history(cluster)
                gc.collect()
                initial_allocated, _ = tracemalloc.get_traced_memory()
                initial_snapshot = tracemalloc.take_snapshot() if profile else None
                initial_rss = _rss_bytes()
                initial_fds = _fd_count()
                started = time.monotonic()
                deadline = started + seconds
                cycles = 0
                high_water_sessions = 0
                while (
                    cycles < session_goal
                    if session_goal is not None
                    else time.monotonic() < deadline
                ):
                    _cycle(access, request)
                    _discard_fake_request_history(cluster)
                    cycles += 1
                    high_water_sessions = max(
                        high_water_sessions, len(access._sessions)
                    )
                terminal_sessions = len(access._sessions)
                access.close()
                query.close()
                _discard_fake_request_history(cluster)
                for _ in range(50):
                    if _access_threads() <= before_threads:
                        break
                    time.sleep(0.1)
                gc.collect()
                retained_allocated, peak_allocated = tracemalloc.get_traced_memory()
                if initial_snapshot is not None:
                    final_snapshot = tracemalloc.take_snapshot()
                    for allocation in final_snapshot.compare_to(
                        initial_snapshot, "lineno"
                    )[:20]:
                        print(f"retained allocation: {allocation}", file=sys.stderr)
                retained_rss = _rss_bytes()
                retained_fds = _fd_count()
                result: dict[str, object] = {
                    "seconds": round(time.monotonic() - started, 2),
                    "cycles": cycles,
                    "forward_processes_checked": cycles + 4,
                    "forward_processes_alive_after_stop": 0,
                    "terminal_sessions": terminal_sessions,
                    "peak_sessions": high_water_sessions,
                    "access_threads_before": len(before_threads),
                    "access_threads_after": len(_access_threads()),
                    "fds_before": initial_fds,
                    "fds_after": retained_fds,
                    "allocated_initial_mib": round(initial_allocated / 2**20, 2),
                    "allocated_retained_mib": round(retained_allocated / 2**20, 2),
                    "allocated_peak_mib": round(peak_allocated / 2**20, 2),
                    "rss_initial_mib": (
                        round(initial_rss / 2**20, 2)
                        if initial_rss is not None
                        else None
                    ),
                    "rss_retained_mib": (
                        round(retained_rss / 2**20, 2)
                        if retained_rss is not None
                        else None
                    ),
                }
                print(json.dumps(result, sort_keys=True), flush=True)
                assert cycles >= 1
                if not smoke:
                    assert cycles > 128, f"only {cycles} access sessions exercised"
                    assert terminal_sessions <= 128, (
                        f"unbounded terminal history: {terminal_sessions} sessions"
                    )
                assert _access_threads() <= before_threads, (
                    "access thread survived close"
                )
                if initial_fds is not None and retained_fds is not None:
                    assert retained_fds <= initial_fds + 4, "file descriptor leak"
                assert retained_allocated - initial_allocated < 16 * 2**20
                if initial_rss is not None and retained_rss is not None:
                    assert retained_rss - initial_rss < 64 * 2**20
                return result
            finally:
                access.close()
                query.close()
                tracemalloc.stop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument("--smoke", action="store_true", help="run for five seconds")
    parser.add_argument(
        "--profile", action="store_true", help="print retained allocation sites"
    )
    parser.add_argument(
        "--sessions",
        type=int,
        help="run this many sessions instead of a timed soak (at least 129)",
    )
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error("--seconds must be 1..3600")
    if args.sessions is not None and (args.smoke or args.sessions < 129):
        parser.error(
            "--sessions must be at least 129 and cannot be combined with --smoke"
        )
    run(
        5 if args.smoke else args.seconds,
        smoke=args.smoke,
        session_goal=args.sessions,
        profile=args.profile,
    )


if __name__ == "__main__":
    main()
