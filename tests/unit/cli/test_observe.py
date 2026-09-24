import errno
import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from piceli.k8s.cli.observe import app


runner = CliRunner()


def test_forward_preferences_are_saved_and_emitted_as_shell_free_json(
    tmp_path: Path,
) -> None:
    preferences = tmp_path / "preferences.json"
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    saved = runner.invoke(
        app,
        [
            "forward-save",
            "--user",
            "jose",
            "--name",
            "kabuki",
            "--namespace",
            "demo",
            "--target",
            "service/kabuki",
            "--local-port",
            "18080",
            "--remote-port",
            "3000",
            "--preferences",
            str(preferences),
        ],
    )
    assert saved.exit_code == 0
    command = runner.invoke(
        app,
        [
            "forward-command",
            "--user",
            "jose",
            "--name",
            "kabuki",
            "--kubeconfig",
            str(kubeconfig),
            "--preferences",
            str(preferences),
        ],
    )
    assert command.exit_code == 0
    assert json.loads(command.stdout)[-2:] == ["--address", "127.0.0.1"]


def test_logs_command_emits_bounded_argv(tmp_path: Path) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    result = runner.invoke(
        app,
        [
            "logs-command",
            "--namespace",
            "demo",
            "--target",
            "deployment/api",
            "--tail",
            "25",
            "--kubeconfig",
            str(kubeconfig),
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)[-2:] == ["deployment/api", "--tail=25"]


def test_serve_reports_an_occupied_port_without_a_traceback(tmp_path: Path) -> None:
    archive = tmp_path / "session.json"
    archive.write_text("{}")
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    with patch(
        "piceli.k8s.cli.observe._archive",
        return_value=object(),
    ), patch(
        "piceli.k8s.cli.observe.KubernetesDynamicInventoryReader",
        return_value=object(),
    ), patch(
        "piceli.k8s.cli.observe.LocalObserveServer",
        side_effect=OSError(errno.EADDRINUSE, "Address already in use"),
    ):
        result = runner.invoke(
            app,
            [
                "serve",
                "--archive",
                str(archive),
                "--kubeconfig",
                str(kubeconfig),
                "--port",
                "9876",
            ],
        )
    assert result.exit_code != 0
    assert "already in use" in result.stdout
    assert "Traceback" not in result.stdout
