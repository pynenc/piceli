from unittest import mock

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app
from piceli.k8s.k8s_objects.base import K8sObject
from piceli.k8s.ops.deploy.deployment_graph import DeploymentGraph
from piceli.k8s.ops.deploy.strategy_auto import StrategyAuto

runner = CliRunner()


@pytest.fixture
def deployment_graph(k8s_objects: list[K8sObject]) -> DeploymentGraph:
    strategy = StrategyAuto()
    return strategy.build_deployment_graph(k8s_objects)


def test_plan_without_validation(k8s_objects: list[K8sObject]) -> None:
    with mock.patch("piceli.k8s.ops.loader.load_all", return_value=k8s_objects):
        result = runner.invoke(app, ["deploy", "plan"])
        assert result.exit_code == 0
        assert "Kubernetes Deployment Plan" in result.stdout
        for k8s_object in k8s_objects:
            assert k8s_object.kind in result.stdout
            assert k8s_object.name in result.stdout


def test_plan_with_validation_success(
    k8s_objects: list[K8sObject],
    deployment_graph: DeploymentGraph,
) -> None:
    with mock.patch(
        "piceli.k8s.ops.loader.load_all", return_value=k8s_objects
    ), mock.patch(
        "piceli.k8s.ops.deploy.strategy_auto.StrategyAuto.build_deployment_graph",
        return_value=deployment_graph,
    ):
        result = runner.invoke(app, ["deploy", "plan", "--validate"])
        assert result.exit_code == 0
        assert "Validation successful" in result.stdout
        assert "Kubernetes Deployment Plan" in result.stdout


def test_plan_with_validation_failure(k8s_objects: list[K8sObject]) -> None:
    class MockFailingGraph:
        def validate(self) -> None:
            raise ValueError("Mock validation failure")

    with mock.patch(
        "piceli.k8s.ops.loader.load_all", return_value=k8s_objects
    ), mock.patch(
        "piceli.k8s.ops.deploy.strategy_auto.StrategyAuto.build_deployment_graph",
        return_value=MockFailingGraph(),
    ):
        result = runner.invoke(app, ["deploy", "plan", "--validate"])
        assert result.exit_code == 0
        assert "Mock validation failure" in result.stdout
        assert "Validation error" in result.stdout
