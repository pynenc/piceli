from unittest.mock import MagicMock

import pytest

from piceli.k8s.exceptions import api_exceptions
from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.object_manager.base import ObjectManager
from piceli.k8s.ops.deploy import deployment_graph
from piceli.k8s.ops.deploy.deployment_executor import DeploymentExecutor


@pytest.mark.asyncio
async def test_deploy_node_creates_nonexistent(
    object_managers: list[ObjectManager],
    not_found_api_op_exception: api_exceptions.ApiOperationException,
) -> None:
    # Setup
    ctx = ClientContext()
    namespace = "test-namespace"
    graph = deployment_graph.DeploymentGraph()
    executor = DeploymentExecutor(graph)

    node = deployment_graph.ObjectNode(deploying_object=object_managers[0])
    graph.add_node(node)

    node.deploying_object.read = MagicMock(side_effect=not_found_api_op_exception)  # type: ignore
    node.deploying_object.create = MagicMock()

    # Act
    await executor.apply_node(node, ctx, namespace)

    # Assert
    node.deploying_object.create.assert_called_once()
    assert node.deployment_status == deployment_graph.DeploymentStatus.DONE


@pytest.mark.asyncio
async def test_rollback_node_deletes_created(
    object_managers: list[ObjectManager],
) -> None:
    # Setup
    ctx = ClientContext()
    namespace = "test-namespace"
    graph = deployment_graph.DeploymentGraph()
    executor = DeploymentExecutor(graph)

    node = deployment_graph.ObjectNode(deploying_object=object_managers[0])
    node.deployment_status = deployment_graph.DeploymentStatus.DONE
    graph.add_node(node)

    node.deploying_object.delete = MagicMock()  # type: ignore

    # Act
    await executor.rollback_node(node, ctx, namespace)

    # Assert
    node.deploying_object.delete.assert_called_once()


@pytest.mark.asyncio
async def test_deploy_with_dependencies(
    object_managers: list[ObjectManager],
    not_found_api_op_exception: api_exceptions.ApiOperationException,
) -> None:
    # Setup
    ctx = ClientContext()
    namespace = "test-namespace"
    graph = deployment_graph.DeploymentGraph()
    executor = DeploymentExecutor(graph)

    # Creating a simple dependency chain obj1 -> obj2
    obj1, obj2 = object_managers[:2]
    node1 = deployment_graph.ObjectNode(deploying_object=obj1)
    node2 = deployment_graph.ObjectNode(deploying_object=obj2)
    graph.add_node(node1)
    graph.add_node(node2)
    graph.add_dependency(node2.identifier, node1.identifier)

    obj1.read = MagicMock(side_effect=not_found_api_op_exception)  # type: ignore
    obj1.create = MagicMock()
    obj1.wait = MagicMock()  # type: ignore
    obj2.read = MagicMock(side_effect=not_found_api_op_exception)  # type: ignore
    obj2.create = MagicMock()

    # Act
    await executor.deploy(ctx, namespace)  # This should deploy all nodes

    # Assert
    obj1.read.assert_called_once()
    obj1.create.assert_called_once()
    obj1.wait.assert_called_once()
    obj1.read.assert_called_once()
    obj2.create.assert_called_once()
    assert node2.deployment_status == deployment_graph.DeploymentStatus.DONE
