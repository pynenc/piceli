from unittest import mock

from typer.testing import CliRunner

from piceli.k8s.cli import app
from piceli.k8s.k8s_objects.base import K8sObject

runner = CliRunner()


def test_plan_without_validation(k8s_objects: list[K8sObject]) -> None:
    with mock.patch("piceli.k8s.ops.loader.load_all", return_value=k8s_objects):
        result = runner.invoke(app, ["deploy", "plan", "--cluster-id", "kind-local"])
        assert result.exit_code == 0
        assert "Kubernetes Deployment Plan" in result.stdout
        for k8s_object in k8s_objects:
            assert k8s_object.kind in result.stdout
            assert k8s_object.name in result.stdout


def test_plan_with_validation_success(
    k8s_objects: list[K8sObject],
) -> None:
    with mock.patch("piceli.k8s.ops.loader.load_all", return_value=k8s_objects):
        result = runner.invoke(
            app, ["deploy", "plan", "--cluster-id", "kind-local", "--validate"]
        )
        assert result.exit_code == 0
        assert "Validation successful" in result.stdout
        assert "Kubernetes Deployment Plan" in result.stdout


def test_plan_with_validation_failure(k8s_objects: list[K8sObject]) -> None:
    with mock.patch(
        "piceli.k8s.ops.loader.load_all", return_value=k8s_objects
    ), mock.patch(
        "piceli.k8s.ops.plan.DeploymentComposition",
        side_effect=ValueError("Mock validation failure"),
    ):
        result = runner.invoke(
            app, ["deploy", "plan", "--cluster-id", "kind-local", "--validate"]
        )
        assert result.exit_code == 0
        assert "Mock validation failure" in result.stdout
        assert "Validation error" in result.stdout


def test_plan_requires_explicit_cluster_binding() -> None:
    result = runner.invoke(app, ["deploy", "plan"])
    assert result.exit_code != 0
    assert "--cluster-id" in result.output
