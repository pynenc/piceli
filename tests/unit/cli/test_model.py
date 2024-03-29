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
