from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app
from piceli.k8s.cli.context import ContextObject
from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.ops.deploy.deployment_executor import (
    DeploymentExecutor,
    ExecutionStatus,
)
from piceli.k8s.ops.deploy.strategy_auto import StrategyAuto

runner = CliRunner()


@pytest.fixture
def ctx_object() -> ContextObject:
    """Fixture for creating a mock context object."""
    return ContextObject(
        module_name="test_module",
        module_path="/path/to/test/module",
        folder_path="/path/to/test/folder",
        sub_elements=True,
        namespace="test-namespace",
    )


@pytest.fixture
def deployment_executor_mock() -> DeploymentExecutor:
    """Fixture for creating a mock deployment executor."""
    mock = AsyncMock(spec=DeploymentExecutor)
    # Configure the mock as needed for your tests
    return mock


@pytest.fixture
def strategy_auto_mock(deployment_executor_mock: DeploymentExecutor) -> StrategyAuto:
    """Fixture for creating a mock strategy auto object."""
    mock = AsyncMock(spec=StrategyAuto)
    mock.build_deployment_graph.return_value = deployment_executor_mock
    return mock


@pytest.fixture
def client_context_mock() -> ClientContext:
    """Fixture for creating a mock client context."""
    return AsyncMock(spec=ClientContext)


@pytest.mark.asyncio
def test_run_command_success(
    ctx_object: ContextObject,
    strategy_auto_mock: DeploymentExecutor,
    client_context_mock: ClientContext,
    deployment_executor_mock: DeploymentExecutor,
) -> None:
    """Test that the run command executes successfully."""
    strategy_auto_mock = MagicMock(spec=StrategyAuto)
    deployment_graph_mock = MagicMock()
    deployment_graph_mock.validate.return_value = None
    strategy_auto_mock.build_deployment_graph.return_value = deployment_graph_mock
    deployment_executor_mock = MagicMock(spec=DeploymentExecutor)
    deployment_executor_mock.graph = MagicMock()
    deployment_executor_mock.status = ExecutionStatus.DONE
    deployment_executor_mock.deployed_nodes = []
    client_context_mock = MagicMock(spec=ClientContext)

    with patch("piceli.k8s.cli.ContextObject", return_value=ctx_object), patch(
        "piceli.k8s.ops.deploy.strategy_auto.StrategyAuto",
        return_value=strategy_auto_mock,
    ), patch(
        "piceli.k8s.k8s_client.client.ClientContext", return_value=client_context_mock
    ), patch(
        "piceli.k8s.ops.deploy.deployment_executor.DeploymentExecutor",
        return_value=deployment_executor_mock,
    ):
        result = runner.invoke(app, ["deploy", "run", "--create-namespace"])
        assert result.exit_code == 0
        assert "Deployment completed successfully" in result.stdout
