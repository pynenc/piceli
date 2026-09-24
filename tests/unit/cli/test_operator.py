import errno
import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from piceli.k8s.cli.operator import app
from piceli.k8s.observe_server import LocalObserveServer

runner = CliRunner()


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_operator_serve_starts_and_stops_without_a_cluster(tmp_path: Path) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    ui_config = tmp_path / "ui.toml"
    ui_config.write_text(
        '[[shortcuts]]\nid = "web"\nlabel = "Web"\ntarget = "service/web"\n'
        "local_port = 3000\nremote_port = 3000\n"
    )
    captured: list[LocalObserveServer] = []

    def fake_serve_forever(self: LocalObserveServer, *_args: object) -> None:
        captured.append(self)

    port = _free_port()
    with (
        patch(
            "piceli.k8s.cli.operator.KubernetesDynamicInventoryReader",
            return_value=object(),
        ),
        patch.object(LocalObserveServer, "serve_forever", fake_serve_forever),
    ):
        result = runner.invoke(
            app,
            [
                "serve",
                "--kubeconfig",
                str(kubeconfig),
                "--namespace",
                "demo",
                "--preferences",
                str(tmp_path / "prefs.json"),
                "--user",
                "tester",
                "--port",
                str(port),
                "--ui-config",
                str(ui_config),
            ],
        )
    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert json.loads(result.stdout) == {
        "address": f"http://127.0.0.1:{port}",
        "operator": True,
    }
    (server,) = captured
    assert server.namespace == "demo"
    assert [item.id for item in server.ui_config.shortcuts] == ["web"]
    assert server.supervisor.shortcuts_status()[0]["namespace"] == "demo"


def test_operator_serve_reports_an_occupied_port_without_a_traceback(
    tmp_path: Path,
) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    with (
        patch(
            "piceli.k8s.cli.operator.KubernetesDynamicInventoryReader",
            return_value=object(),
        ),
        patch(
            "piceli.k8s.cli.operator.LocalObserveServer",
            side_effect=OSError(errno.EADDRINUSE, "Address already in use"),
        ),
    ):
        result = runner.invoke(
            app,
            [
                "serve",
                "--kubeconfig",
                str(kubeconfig),
                "--preferences",
                str(tmp_path / "prefs.json"),
                "--port",
                "9876",
            ],
        )
    assert result.exit_code != 0
    assert "already in use" in result.output
    assert "Traceback" not in result.output


def test_operator_serve_rejects_invalid_ui_config(tmp_path: Path) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    ui_config = tmp_path / "ui.toml"
    ui_config.write_text('[[shortcuts]]\nid = "Bad Id"\n')
    result = runner.invoke(
        app,
        ["serve", "--kubeconfig", str(kubeconfig), "--ui-config", str(ui_config)],
    )
    assert result.exit_code != 0
