import json
import stat
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from piceli.k8s.observe import (
    InventoryReader,
    InventoryReport,
    ObservationRef,
    ObservedObject,
    PortForward,
    PreferenceStore,
    ForwardSupervisor,
    UserPreferences,
    archive_resources,
    kubectl_logs_command,
    observe_session,
    run_port_forward,
)
from piceli.k8s.observe_server import LocalObserveServer


class Archive:
    session_id = "a" * 32

    def to_dict(self) -> dict[str, object]:
        return {
            "composition": [
                {
                    "resources": [
                        {
                            "resource": {
                                "api_version": "v1",
                                "kind": "Service",
                                "namespace": "demo",
                                "name": "api",
                            }
                        }
                    ]
                }
            ]
        }


class Reader(InventoryReader):
    def get(self, ref: ObservationRef) -> ObservedObject | None:
        if ref.name == "api":
            return ObservedObject(
                ref, uid="one", phase="Ready", images=("demo@sha256:abc",)
            )
        return None

    def list(self, api_version: str, kind: str, namespace: str):
        if (api_version, kind, namespace) == ("v1", "Service", "demo"):
            yield ObservedObject(
                ObservationRef("v1", "Service", "demo", "api"), uid="one"
            )
            yield ObservedObject(
                ObservationRef("v1", "Service", "demo", "outside"), uid="two"
            )


def test_observation_marks_declared_and_undeclared_objects_without_manifests() -> None:
    archive = Archive()
    assert archive_resources(archive) == (
        ObservationRef("v1", "Service", "demo", "api"),
    )
    report = observe_session(archive, Reader(), include_common_types=False)
    assert report.declared[0].state == "present"
    assert [item.ref.name for item in report.undeclared] == ["outside"]
    encoded = json.dumps(report.to_dict())
    assert "private_bindings" not in encoded
    assert "manifest" not in encoded


def test_preferences_are_owner_only_and_build_loopback_command(tmp_path: Path) -> None:
    store = PreferenceStore(tmp_path / "observe.json")
    forward = PortForward("kabuki", "demo", "service/kabuki", 18080, 3000)
    store.replace_user(UserPreferences("jose", (forward,)))
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    loaded = store.load()["jose"].forwards[0]
    assert loaded.command(
        kubectl="kubectl", kubeconfig=Path("/tmp/kube"), context="lab"
    ) == [
        "kubectl",
        "--kubeconfig",
        "/tmp/kube",
        "--context",
        "lab",
        "--namespace",
        "demo",
        "port-forward",
        "service/kabuki",
        "18080:3000",
        "--address",
        "127.0.0.1",
    ]


def test_port_forward_runner_rejects_non_loopback_commands() -> None:
    with pytest.raises(ValueError, match="loopback"):
        run_port_forward(
            ["kubectl", "port-forward", "service/api", "80:80", "--address", "0.0.0.0"]
        )


def test_forward_supervisor_owns_only_saved_loopback_processes(tmp_path: Path) -> None:
    store = PreferenceStore(tmp_path / "observe.json")
    store.replace_user(
        UserPreferences("jose", (PortForward("api", "demo", "service/api", 18080, 80),))
    )
    supervisor = ForwardSupervisor(
        preferences=store,
        user="jose",
        kubeconfig=tmp_path / "kubeconfig",
        kubectl="definitely-not-kubectl",
    )
    supervisor.restore()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not supervisor.statuses()[0].error:
            time.sleep(0.01)
        status = supervisor.statuses()[0]
        assert status.name == "api"
        assert status.state in {"backoff", "failed"}
        supervisor.stop("api")
        assert supervisor.statuses()[0].state == "backoff"
    finally:
        supervisor.close()


def test_log_command_is_bounded_and_shell_free() -> None:
    assert kubectl_logs_command(
        kubectl="kubectl",
        kubeconfig=Path("/tmp/kube"),
        context="lab",
        namespace="demo",
        target="deployment/api",
        tail=50,
        previous=True,
    ) == [
        "kubectl",
        "--kubeconfig",
        "/tmp/kube",
        "--context",
        "lab",
        "--namespace",
        "demo",
        "logs",
        "deployment/api",
        "--tail=50",
        "--previous",
    ]
    with pytest.raises(ValueError, match="tail"):
        kubectl_logs_command(
            kubectl="kubectl",
            kubeconfig=Path("/tmp/kube"),
            context=None,
            namespace="demo",
            target="pod/api",
            tail=10_001,
        )


def test_local_rest_server_exposes_only_read_only_loopback_data(tmp_path: Path) -> None:
    store = PreferenceStore(tmp_path / "observe.json")
    server = LocalObserveServer(
        ("127.0.0.1", 0),
        lambda: InventoryReport("a" * 32, (), ()),
        store,
    )
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/v1/status") as response:
            assert json.loads(response.read())["session_id"] == "a" * 32
    finally:
        thread.join(timeout=1)
        server.server_close()

    html_server = LocalObserveServer(
        ("127.0.0.1", 0), lambda: InventoryReport("a" * 32, (), ()), store
    )
    html_thread = threading.Thread(target=html_server.handle_request)
    html_thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{html_server.server_port}/") as response:
            assert b"Piceli Observe" in response.read()
    finally:
        html_thread.join(timeout=1)
        html_server.server_close()

    store.replace_user(
        UserPreferences(
            "alice", (PortForward("alice-forward", "demo", "service/a", 18001, 80),)
        )
    )
    store.replace_user(
        UserPreferences(
            "bob", (PortForward("bob-forward", "demo", "service/b", 18002, 80),)
        )
    )
    scoped_server = LocalObserveServer(
        ("127.0.0.1", 0),
        lambda: InventoryReport("a" * 32, (), ()),
        store,
        user="bob",
    )
    scoped_thread = threading.Thread(target=scoped_server.handle_request)
    scoped_thread.start()
    try:
        with urlopen(
            f"http://127.0.0.1:{scoped_server.server_port}/v1/preferences"
        ) as response:
            assert json.loads(response.read()) == {
                "users": [
                    {
                        "user": "bob",
                        "forwards": [
                            {
                                "name": "bob-forward",
                                "namespace": "demo",
                                "target": "service/b",
                                "local_port": 18002,
                                "remote_port": 80,
                            }
                        ],
                    }
                ]
            }
    finally:
        scoped_thread.join(timeout=1)
        scoped_server.server_close()

    with pytest.raises(ValueError, match="loopback"):
        LocalObserveServer(
            ("0.0.0.0", 0), lambda: InventoryReport("a" * 32, (), ()), store
        )


def test_forward_supervisor_shortcuts_and_dynamic_management(tmp_path: Path) -> None:
    store = PreferenceStore(tmp_path / "observe.json")
    supervisor = ForwardSupervisor(
        preferences=store,
        user="tester",
        kubeconfig=tmp_path / "kubeconfig",
        kubectl="definitely-not-kubectl",
    )
    supervisor.restore()
    try:
        # Check shortcuts status
        scs = supervisor.shortcuts_status(namespace="test-ns")
        assert len(scs) == 4
        ids = {s["id"] for s in scs}
        assert "kabuki" in ids
        assert "monitor" in ids
        assert "poet" in ids
        assert "shibuya" in ids

        # Quick start known shortcut
        supervisor.quick_start("kabuki", namespace="test-ns")
        kabuki_status = [s for s in supervisor.statuses() if s.name == "kabuki"][0]
        assert kabuki_status.local_port == 3000
        assert kabuki_status.target == "service/ih-kabuki"
        assert kabuki_status.namespace == "test-ns"

        # Add custom forward
        custom = PortForward("custom-fwd", "test-ns", "service/custom", 9090, 8080)
        supervisor.add_or_update(custom, persist=True)
        assert any(s.name == "custom-fwd" for s in supervisor.statuses())

        # Remove custom forward
        supervisor.remove("custom-fwd", persist=True)
        assert not any(s.name == "custom-fwd" for s in supervisor.statuses())
    finally:
        supervisor.close()

def test_local_rest_server_favicon_returns_no_content(tmp_path: Path) -> None:
    store = PreferenceStore(tmp_path / "observe.json")
    server = LocalObserveServer(
        ("127.0.0.1", 0), lambda: InventoryReport("a" * 32, (), ()), store
    )
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    try:
        req = Request(f"http://127.0.0.1:{server.server_port}/favicon.ico")
        with urlopen(req) as response:
            assert response.status == 204
    finally:
        thread.join(timeout=1)
        server.server_close()
