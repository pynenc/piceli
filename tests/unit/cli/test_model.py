from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

from piceli.k8s.cli import app
from piceli.k8s.k8s_objects.base import K8sObject

runner = CliRunner()


def test_list(k8s_objects: list[K8sObject]) -> None:
    # Patch loader.load_all to return the mock objects
    with mock.patch("piceli.k8s.ops.loader.load_all", return_value=k8s_objects):
        # Invoke the Typer CLI runner to execute the list command
        result = runner.invoke(app, ["model", "list"])

        # Check results
        assert result.exit_code == 0
        for obj in k8s_objects:
            assert obj.name in result.stdout
            if obj.namespace:
                assert obj.namespace in result.stdout
            assert str(obj.origin)[0:30] in result.stdout


def test_list_module_path_end_to_end(tmp_path: Path) -> None:
    """model list with --module-path loads (executes) the module and lists its objects"""
    module_file = tmp_path / "my_app.py"
    module_file.write_text(
        "from piceli.k8s import templates\n"
        "\n"
        "web = templates.Deployment(\n"
        '    name="web-frontend",\n'
        "    containers=[\n"
        "        templates.Container(\n"
        '            name="web",\n'
        '            image="nginx",\n'
        '            ports=[templates.Port(name="http", port=80)],\n'
        "        )\n"
        "    ],\n"
        "    create_service=True,\n"
        ")\n"
    )
    result = runner.invoke(
        app,
        ["--module-path", str(module_file), "model", "list"],
        env={"COLUMNS": "250"},
    )
    assert result.exit_code == 0, result.stdout
    assert "No Kubernetes objects found" not in result.stdout
    rows = [line for line in result.stdout.splitlines() if "web-frontend" in line]
    kinds = {kind for kind in ("Deployment", "Service") for row in rows if kind in row}
    assert kinds == {"Deployment", "Service"}
