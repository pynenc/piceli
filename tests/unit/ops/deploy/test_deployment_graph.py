import pytest

from piceli.k8s.object_manager.base import ObjectManager
from piceli.k8s.ops.deploy.deployment_graph import DeploymentGraph, ObjectNode


def test_add_node(
    deployment_graph: DeploymentGraph, object_managers: list[ObjectManager]
) -> None:
    """Test adding nodes to the deployment graph."""
    for manager in object_managers:
        node = ObjectNode(deploying_object=manager)
        deployment_graph.add_node(node)
        assert node.identifier in deployment_graph.nodes
        assert deployment_graph.nodes[node.identifier] == node


def test_add_dependency(
    deployment_graph: DeploymentGraph, object_managers: list[ObjectManager]
) -> None:
    """Test adding a single dependency between two nodes."""
    node_a = ObjectNode(deploying_object=object_managers[0])
    node_b = ObjectNode(deploying_object=object_managers[1])
    deployment_graph.add_node(node_a)
    deployment_graph.add_node(node_b)

    deployment_graph.add_dependency(node_a.identifier, node_b.identifier)
    assert node_b.identifier in deployment_graph.nodes[node_a.identifier].dependencies


def test_add_dependencies(
    deployment_graph: DeploymentGraph, object_managers: list[ObjectManager]
) -> None:
    """Test adding multiple dependencies at once."""
    nodes = [ObjectNode(deploying_object=manager) for manager in object_managers]
    for node in nodes:
        deployment_graph.add_node(node)

    # Make node 0 depend on node 1 and 2
    deployment_graph.add_dependencies(
        from_identifiers=[nodes[0].identifier],
        to_identifiers=[nodes[1].identifier, nodes[2].identifier],
    )
    assert (
        nodes[1].identifier in deployment_graph.nodes[nodes[0].identifier].dependencies
    )
    assert (
        nodes[2].identifier in deployment_graph.nodes[nodes[0].identifier].dependencies
    )


def test_validate_valid_graph(object_managers: list[ObjectManager]) -> None:
    valid_graph = DeploymentGraph()
    for manager in object_managers:
        node = ObjectNode(deploying_object=manager)
        valid_graph.add_node(node)
    # Assuming object_managers has at least two items for this to make sense
    valid_graph.add_dependency(
        object_managers[0].k8s_object.identifier,
        object_managers[1].k8s_object.identifier,
    )
    try:
        valid_graph.validate()
    except ValueError:
        pytest.fail("validate() raised ValueError unexpectedly!")


def test_validate_graph_with_cycle(object_managers: list[ObjectManager]) -> None:
    graph_with_cycle = DeploymentGraph()
    for manager in object_managers:
        node = ObjectNode(deploying_object=manager)
        graph_with_cycle.add_node(node)
    # Create a cycle
    graph_with_cycle.add_dependency(
        object_managers[0].k8s_object.identifier,
        object_managers[1].k8s_object.identifier,
    )
    graph_with_cycle.add_dependency(
        object_managers[1].k8s_object.identifier,
        object_managers[0].k8s_object.identifier,
    )
    with pytest.raises(ValueError, match="Cycle detected"):
        graph_with_cycle.validate()


def test_traverse_graph(object_managers: list[ObjectManager]) -> None:
    graph = DeploymentGraph()

    # Create ObjectNodes from mock K8sObjects
    nodes = [ObjectNode(deploying_object=obj) for obj in object_managers]

    # Add nodes to the graph
    for node in nodes:
        graph.add_node(node)

    # Define dependencies: obj1 -> obj2 -> obj3 (obj1 depends on obj2, obj2 depends on obj3)
    graph.add_dependency(
        nodes[0].identifier, nodes[1].identifier
    )  # obj1 depends on obj2
    graph.add_dependency(
        nodes[1].identifier, nodes[2].identifier
    )  # obj2 depends on obj3

    # Traverse the graph
    levels = graph.traverse_graph()

    # Check that there are three levels due to the dependencies
    assert len(levels) == 3, "There should be three levels based on the dependencies"

    # Check that each level contains the correct node
    assert levels[0][0] == nodes[2], "The first level should contain obj3"
    assert levels[1][0] == nodes[1], "The second level should contain obj2"
    assert levels[2][0] == nodes[0], "The third level should contain obj1"


def test_traverse_graph_parallel_nodes(object_managers: list[ObjectManager]) -> None:
    graph = DeploymentGraph()
    # We only need the first three nodes for this test
    nodes = [ObjectNode(deploying_object=obj) for obj in object_managers[:3]]
    for node in nodes:
        graph.add_node(node)
    # Define dependencies: obj3 depends on obj1 and obj2, but obj1 and obj2 have no dependencies on each other
    graph.add_dependency(
        nodes[2].identifier, nodes[0].identifier
    )  # obj3 depends on obj1
    graph.add_dependency(
        nodes[2].identifier, nodes[1].identifier
    )  # obj3 depends on obj2

    # Traverse the graph
    levels = graph.traverse_graph()

    # Check that there are two levels
    assert len(levels) == 2, "There should be two levels based on the dependencies"
    # Check that the first level contains both obj1 and obj2 since they can be deployed in parallel
    assert (
        len(levels[0]) == 2
    ), "The first level should contain two nodes that can be deployed in parallel"
    assert nodes[0] in levels[0], "obj1 should be in the first level"
    assert nodes[1] in levels[0], "obj2 should be in the first level"
    # Check that the second level contains obj3, which depends on both obj1 and obj2
    assert levels[1][0] == nodes[2], "The second level should contain obj3"
