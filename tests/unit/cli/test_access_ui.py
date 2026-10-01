"""``piceli access ui``: explicit credentials, one URL line, owned forward only.

No cluster: the launch reader and the forward supervisor are replaced.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s import access as access_module
from piceli.k8s import observe as observe_module
from piceli.k8s.cli import app, ui_forward
from piceli.profiles import save_profile

runner = CliRunner()
TOKEN = "launch-" + "x" * 40


class Supervisor:
    instances: list[Supervisor] = []

    def __init__(self, **options: Any) -> None:
        self.options = options
        self.started: list[tuple[str, str | None]] = []
        self.closed = False
        Supervisor.instances.append(self)

    def quick_start(self, shortcut_id: str, namespace: str | None = None) -> None:
        self.started.append((shortcut_id, namespace))

    def shortcuts_status(self, namespace: str | None = None) -> list[dict[str, Any]]:
        return [{"id": "ui", "state": "failed", "health": "failed", "error": "gave up"}]

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "lab.kubeconfig"
    config.write_text(
        textwrap.dedent(
            """
            apiVersion: v1
            kind: Config
            clusters: [{name: lab, cluster: {server: "https://192.0.2.1:6443"}}]
            users: [{name: lab, user: {token: not-used}}]
            contexts: [{name: lab, context: {cluster: lab, user: lab}}]
            """
        )
    )
    monkeypatch.setenv("PICELI_PROFILES_DIR", str(tmp_path / "profiles"))
    save_profile("my-cluster", config, "lab")
    reads: list[tuple[Path, str]] = []

    def reader(kubeconfig: Path, context: str) -> str:
        reads.append((kubeconfig, context))
        return TOKEN

    monkeypatch.setattr(ui_forward, "_launch_reader", reader)
    monkeypatch.setattr(observe_module, "ForwardSupervisor", Supervisor)
    monkeypatch.setattr(ui_forward.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(access_module, "port_conflicts", lambda shortcuts, **_: [])
    Supervisor.instances.clear()
    (tmp_path / "infra.py").write_text(
        textwrap.dedent(
            """
            from piceli.infra import Cluster, Ui

            my_cluster = Cluster(
                "my-cluster", api="https://192.0.2.1:6443",
                credentials="my-cluster", ui=Ui(access="forward"),
            )
            bare = Cluster("bare", api="https://192.0.2.1:6443", credentials="my-cluster")
            not_a_cluster = 1
            """
        )
    )
    monkeypatch.chdir(tmp_path)
    return config


def test_profile_forwards_the_service_and_prints_one_url(lab: Path) -> None:
    result = runner.invoke(app, ["access", "ui", "--profile", "my-cluster", "--json"])
    assert result.exit_code == 1  # the fake forward gave up at once
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [event["event"] for event in events] == ["started", "status", "stopped"]
    assert events[0]["url"] == f"http://127.0.0.1:8790/?token={TOKEN}"
    assert result.stdout.count(TOKEN) == 1 and TOKEN not in result.stderr
    (supervisor,) = Supervisor.instances
    assert supervisor.options["context"] == "lab"
    assert supervisor.options["kubeconfig"] == lab.absolute()
    (shortcut,) = supervisor.options["shortcuts"]
    assert shortcut.target == "service/piceli-ui"
    assert (shortcut.local_port, shortcut.remote_port) == (8790, 8790)
    assert supervisor.started == [("ui", "piceli-system")]
    assert supervisor.closed


def test_cluster_module_uses_its_credentials_profile(lab: Path) -> None:
    result = runner.invoke(app, ["access", "ui", "--cluster", "infra.py:my_cluster"])
    assert f"Piceli UI: http://127.0.0.1:8790/?token={TOKEN}" in result.stdout
    assert TOKEN not in result.stderr
    assert Supervisor.instances[0].options["context"] == "lab"


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        ([], "access-ui-target-required"),
        (
            ["--profile", "my-cluster", "--cluster", "infra.py:my_cluster"],
            "access-ui-target-required",
        ),
        (["--cluster", "infra.py:not_a_cluster"], "access-ui-cluster-invalid"),
        (["--cluster", "missing.py:x"], "access-ui-cluster-invalid"),
        (["--cluster", "infra.py:bare"], "access-ui-not-declared"),
        (["--profile", "unknown"], "profile-not-found"),
    ],
)
def test_refusals(lab: Path, argv: list[str], code: str) -> None:
    result = runner.invoke(app, ["access", "ui", *argv])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == code
    assert not Supervisor.instances


def test_not_ready_and_port_conflict_start_nothing(
    lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def not_ready(kubeconfig: Path, context: str) -> str:
        raise ui_forward.LaunchError(code="access-ui-not-ready")

    monkeypatch.setattr(ui_forward, "_launch_reader", not_ready)
    result = runner.invoke(app, ["access", "ui", "--profile", "my-cluster"])
    assert json.loads(result.stdout)["reason"] == "access-ui-not-ready"
    monkeypatch.setattr(ui_forward, "_launch_reader", lambda *_: TOKEN)

    class Taken:
        def describe(self) -> str:
            return "port 8790 is held by pid 1"

        def to_dict(self) -> dict[str, Any]:
            return {"port": 8790, "holder": "other"}

    monkeypatch.setattr(
        access_module, "port_conflicts", lambda shortcuts, **_: [Taken()]
    )
    result = runner.invoke(app, ["access", "ui", "--profile", "my-cluster"])
    assert json.loads(result.stdout)["reason"] == "access-port-conflict"
    assert TOKEN not in result.stdout + result.stderr
    assert not Supervisor.instances


def test_forward_serve_binds_loopback_and_never_prints_its_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    from piceli.gitops import install

    account = tmp_path / "serviceaccount"
    account.mkdir()
    (account / "ca.crt").write_text("ca")
    (account / "token").write_text("not-a-real-token")
    monkeypatch.setattr(ui_forward, "SERVICE_ACCOUNT_DIR", account)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "192.0.2.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    published: list[str] = []
    served: dict[str, Any] = {}

    class Session:
        client = object()

    class Connect:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> Session:
            return Session()

        def __exit__(self, *exc: object) -> None:
            return None

    def run(server: Any, **options: Any) -> None:
        served.update(options, app=server)

    monkeypatch.setattr(install, "connect", Connect)
    monkeypatch.setattr(
        ui_forward,
        "publish_token",
        lambda client, token, namespace: published.append(token),
    )
    monkeypatch.setattr(uvicorn, "run", run)
    result = runner.invoke(app, ["ui", "forward-serve"])
    assert result.exit_code == 0, result.output
    token = served["app"].state.security.launch_token
    assert served["host"] == "127.0.0.1" and served["port"] == 8790
    assert served["app"].state.security.origin == "http://127.0.0.1:8790"
    assert published == [token, ""]
    assert token not in result.stdout + result.stderr
    assert "piceli access ui" in result.stderr
