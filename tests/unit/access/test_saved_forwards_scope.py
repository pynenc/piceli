"""Saved forwards are scoped to a cluster and restored only on request (B5).

Model access declarations are started by ``operator serve --access`` like
``piceli access --dashboard`` (B10). No test starts a real process: ``Popen``
is replaced, and every preference file lives under ``tmp_path`` (``HOME`` too).
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.observe import app as observe_app
from piceli.k8s.cli.operator import app as operator_app
from piceli.k8s.observe import (
    ForwardScope,
    ForwardSupervisor,
    PortForward,
    PreferenceStore,
    UserPreferences,
    forward_scope,
)
from piceli.k8s.observe_server import LocalObserveServer
from tests.unit.access.test_access_status import write_spec

runner = CliRunner()


def _kubeconfig(path: Path, server: str = "https://127.0.0.1:6443") -> Path:
    path.write_text(
        "apiVersion: v1\nkind: Config\n"
        f'clusters: [{{name: c, cluster: {{server: "{server}"}}}}]\n'
        "users: [{name: u, user: {token: not-a-real-token}}]\n"
        "contexts: [{name: lab, context: {cluster: c, user: u}}]\n"
    )
    return path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated HOME: the default preference store lives under it."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("USER", "tester")
    return root


def test_scope_is_the_context_and_a_digest_of_the_server(tmp_path: Path) -> None:
    one = forward_scope(_kubeconfig(tmp_path / "a"), "lab")
    assert one is not None and one.context == "lab"
    assert one.cluster.startswith("sha256:") and len(one.cluster) == 23
    # The same cluster through another file matches; the path is not stored.
    assert forward_scope(_kubeconfig(tmp_path / "b"), "lab") == one
    other = forward_scope(_kubeconfig(tmp_path / "c", "https://10.0.0.1"), "lab")
    assert other is not None and other != one
    assert "6443" not in json.dumps(one.public_dict())
    assert forward_scope(tmp_path / "a", "missing") is None
    assert forward_scope(tmp_path / "absent", "lab") is None
    with pytest.raises(ValueError):
        ForwardScope(context="lab", cluster="https://127.0.0.1")


def test_store_round_trips_scope_and_reads_legacy_entries(tmp_path: Path) -> None:
    scope = ForwardScope("lab", "sha256:" + "a" * 16)
    store = PreferenceStore(tmp_path / "observe.json")
    scoped = PortForward("web", "demo", "service/web", 18080, 80, scope=scope)
    legacy = PortForward("api", "demo", "service/api", 18081, 80)
    store.replace_user(UserPreferences("jose", (legacy, scoped)))
    raw = json.loads(store.path.read_text())
    assert "scope" not in raw["users"][0]["forwards"][0]  # older readers still work
    assert store.load()["jose"].forwards == (legacy, scoped)
    assert scoped.matches(scope, "demo") and scoped.matches(scope, None)
    assert not scoped.matches(scope, "prod")
    assert not legacy.matches(scope, "demo")
    assert not scoped.matches(None, "demo")


def test_restore_starts_only_forwards_saved_for_this_cluster(tmp_path: Path) -> None:
    scope = ForwardScope("lab", "sha256:" + "a" * 16)
    elsewhere = ForwardScope("lab", "sha256:" + "b" * 16)
    store = PreferenceStore(tmp_path / "observe.json")
    ports = [_free_port() for _ in range(4)]
    store.replace_user(
        UserPreferences(
            "jose",
            (
                PortForward("mine", "demo", "service/a", ports[0], 80, scope=scope),
                PortForward("legacy", "demo", "service/b", ports[1], 80),
                PortForward(
                    "other", "demo", "service/c", ports[2], 80, scope=elsewhere
                ),
                PortForward("prod", "prod", "service/d", ports[3], 80, scope=scope),
            ),
        )
    )
    supervisor = ForwardSupervisor(
        preferences=store,
        user="jose",
        kubeconfig=tmp_path / "kubeconfig",
        context="lab",
        namespace="demo",
        scope=scope,
    )
    with patch("piceli.k8s.observe.subprocess.Popen") as spawn:
        spawn.return_value.poll.return_value = None
        try:
            assert supervisor.restore() == ("mine",)
            assert [item.name for item in supervisor.statuses()] == ["mine"]
            (call,) = spawn.call_args_list
            assert "service/a" in call.args[0]
            matching, other = supervisor.saved_forwards()
            assert [item.name for item in matching] == ["mine"]
            assert sorted(item.name for item in other) == ["legacy", "other", "prod"]
        finally:
            supervisor.close()


def test_a_forward_added_from_the_dashboard_is_saved_with_the_scope(
    tmp_path: Path,
) -> None:
    scope = ForwardScope("lab", "sha256:" + "a" * 16)
    store = PreferenceStore(tmp_path / "observe.json")
    supervisor = ForwardSupervisor(
        preferences=store,
        user="jose",
        kubeconfig=tmp_path / "kubeconfig",
        context="lab",
        scope=scope,
    )
    try:
        supervisor.add_or_update(PortForward("web", "demo", "service/web", 1234, 80))
    finally:
        supervisor.close()
    (saved,) = store.load()["jose"].forwards
    assert saved.scope == scope


def _serve(app: Any, args: list[str]) -> tuple[Any, list[LocalObserveServer], Any]:
    captured: list[LocalObserveServer] = []

    def fake_serve_forever(self: LocalObserveServer, *_args: object) -> None:
        captured.append(self)

    with (
        patch(
            "piceli.k8s.cli.operator.KubernetesDynamicInventoryReader",
            return_value=object(),
        ),
        patch(
            "piceli.k8s.cli.observe.KubernetesDynamicInventoryReader",
            return_value=object(),
        ),
        patch("piceli.k8s.cli.observe._archive", return_value=MagicMock()),
        patch.object(LocalObserveServer, "serve_forever", fake_serve_forever),
        patch("piceli.k8s.observe.subprocess.Popen") as spawn,
    ):
        spawn.return_value.poll.return_value = None
        result = runner.invoke(app, args)
    return result, captured, spawn


def _forwards_started(spawn: Any) -> list[list[str]]:
    """``kubectl port-forward`` argv lists (``lsof``/``ps`` lookups excluded)."""
    return [
        call.args[0]
        for call in spawn.call_args_list
        if call.args and "port-forward" in call.args[0]
    ]


def _save_forwards(store_path: Path, kubeconfig: Path, namespace: str) -> None:
    scope = forward_scope(kubeconfig, "lab")
    elsewhere = ForwardScope("prod-cluster", "sha256:" + "f" * 16)
    PreferenceStore(store_path).replace_user(
        UserPreferences(
            "tester",
            (
                PortForward(
                    "here", namespace, "service/web", _free_port(), 80, scope=scope
                ),
                PortForward("legacy", namespace, "service/api", _free_port(), 80),
                PortForward(
                    "prod", "prod", "service/db", _free_port(), 5432, scope=elsewhere
                ),
            ),
        )
    )


@pytest.mark.parametrize("explicit_preferences", [True, False])
def test_operator_serve_never_starts_saved_forwards_implicitly(
    tmp_path: Path, home: Path, explicit_preferences: bool
) -> None:
    kubeconfig = _kubeconfig(tmp_path / "kubeconfig")
    store_path = (
        tmp_path / "prefs.json"
        if explicit_preferences
        else home / ".config" / "piceli" / "observe.json"
    )
    _save_forwards(store_path, kubeconfig, "demo")
    extra = ["--preferences", str(store_path)] if explicit_preferences else []
    args = [
        "serve",
        "--kubeconfig",
        str(kubeconfig),
        "--context",
        "lab",
        "--namespace",
        "demo",
        "--port",
        str(_free_port()),
        *extra,
    ]
    result, (server,), spawn = _serve(operator_app, args)
    assert result.exit_code == 0, result.output
    spawn.assert_not_called()
    assert server.supervisor is not None and server.supervisor.statuses() == ()

    result, (server,), spawn = _serve(operator_app, [*args, "--restore-forwards"])
    assert result.exit_code == 0, result.output
    (call,) = spawn.call_args_list
    assert "service/web" in call.args[0] and "service/db" not in call.args[0]
    assert [item.name for item in server.supervisor.statuses()] == ["here"]
    assert "restored 1 saved forward(s): here" in result.stderr
    assert "left 2 saved forward(s) alone" in result.stderr


def test_observe_serve_restores_only_with_the_flag(tmp_path: Path, home: Path) -> None:
    kubeconfig = _kubeconfig(tmp_path / "kubeconfig")
    archive = tmp_path / "session.json"
    archive.write_text("{}")
    store_path = tmp_path / "prefs.json"
    _save_forwards(store_path, kubeconfig, "demo")
    args = [
        "serve",
        "--archive",
        str(archive),
        "--kubeconfig",
        str(kubeconfig),
        "--context",
        "lab",
        "--namespace",
        "demo",
        "--preferences",
        str(store_path),
        "--user",
        "tester",
        "--port",
        str(_free_port()),
    ]
    result, (server,), spawn = _serve(observe_app, args)
    assert result.exit_code == 0, result.output
    spawn.assert_not_called()
    assert server.preference_scope == forward_scope(kubeconfig, "lab")
    result, _, spawn = _serve(observe_app, [*args, "--restore-forwards"])
    assert result.exit_code == 0, result.output
    assert len(spawn.call_args_list) == 1


def test_forward_save_scopes_with_kubeconfig_and_context(tmp_path: Path) -> None:
    kubeconfig = _kubeconfig(tmp_path / "kubeconfig")
    store = tmp_path / "prefs.json"
    base = [
        "forward-save",
        "--user",
        "jose",
        "--namespace",
        "demo",
        "--target",
        "service/web",
        "--local-port",
        "18080",
        "--remote-port",
        "80",
        "--preferences",
        str(store),
    ]
    result = runner.invoke(
        observe_app,
        [*base, "--name", "web", "--kubeconfig", str(kubeconfig), "--context", "lab"],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(observe_app, [*base, "--name", "bad", "--context", "lab"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "invalid-forward-preference"
    (saved,) = PreferenceStore(store).load()["jose"].forwards
    assert saved.scope == forward_scope(kubeconfig, "lab")
    listed = runner.invoke(
        observe_app,
        ["forward-list", "--user", "jose", "--preferences", str(store)],
    )
    assert json.loads(listed.stdout)["forwards"][0]["scope"]["context"] == "lab"


def test_operator_serve_access_starts_the_declared_forwards(tmp_path: Path) -> None:
    spec = write_spec(tmp_path, local=_free_port(), upstream=80)
    args = [
        "serve",
        "--kubeconfig",
        str(tmp_path / "kubeconfig"),
        "--context",
        "demo",
        "--namespace",
        "shop",
        "--preferences",
        str(tmp_path / "prefs.json"),
        "--port",
        str(_free_port()),
        "--access",
        str(spec),
    ]
    result, (server,), spawn = _serve(operator_app, args)
    assert result.exit_code == 0, result.output
    started = sorted(
        argv[argv.index("port-forward") + 1] for argv in _forwards_started(spawn)
    )
    assert started == ["service/api", "service/web"]

    result, (server,), spawn = _serve(operator_app, [*args, "--no-start-access"])
    assert result.exit_code == 0, result.output
    spawn.assert_not_called()


def test_operator_serve_access_refuses_a_taken_required_port(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        taken = listener.getsockname()[1]
        spec = write_spec(tmp_path, local=taken, upstream=80)
        result, captured, spawn = _serve(
            operator_app,
            [
                "serve",
                "--kubeconfig",
                str(tmp_path / "kubeconfig"),
                "--context",
                "demo",
                "--namespace",
                "shop",
                "--preferences",
                str(tmp_path / "prefs.json"),
                "--port",
                str(_free_port()),
                "--access",
                str(spec),
            ],
        )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "access-port-conflict"
    assert _forwards_started(spawn) == []
    assert captured == []


def test_supervisor_does_not_call_a_foreign_listener_healthy(tmp_path: Path) -> None:
    """A probe answered by someone else's process is a conflict, not healthy (B8)."""
    from piceli.k8s.port_owner import PortOwner

    port = _free_port()
    supervisor = ForwardSupervisor(
        kubeconfig=tmp_path / "kubeconfig",
        context="lab",
        shortcuts=(),
    )
    forward = PortForward("web", "demo", "service/web", port, 80)
    with (
        patch("piceli.k8s.observe.subprocess.Popen") as spawn,
        patch.object(ForwardSupervisor, "_stop_process") as stop,
        socket.socket() as listener,
    ):
        spawn.return_value.poll.return_value = None
        spawn.return_value.pid = 111
        try:
            supervisor.add_or_update(forward, persist=False)
            supervisor.start("web")
            listener.bind(("127.0.0.1", port))
            listener.listen(1)
            supervisor._forwards["web"].next_probe = 0.0  # probe now
            with patch.object(
                ForwardSupervisor,
                "_owner",
                return_value=PortOwner(port, 999, "other-project dev"),
            ):
                supervisor.tick()
            (status,) = supervisor.statuses()
            assert status.health == "conflict"
            assert status.state == "failed"
            stop.assert_called_once()
            # Our own process answering is healthy.
            listener.close()
            supervisor.start("web")
            with socket.socket() as again:
                again.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                again.bind(("127.0.0.1", port))
                again.listen(1)
                supervisor._forwards["web"].next_probe = 0.0
                with patch.object(
                    ForwardSupervisor, "_owner", return_value=PortOwner(port, 111)
                ):
                    supervisor.tick()
                assert supervisor.statuses()[0].health == "healthy"
        finally:
            supervisor.close()
