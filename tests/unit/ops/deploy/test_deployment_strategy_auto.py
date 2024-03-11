from piceli.k8s.k8s_objects.base import K8sObject
from piceli.k8s.ops.deploy import strategy_auto


def test_classify_k8s_objects_by_deployment_level(resources: list[K8sObject]) -> None:
    classified = strategy_auto.classify_k8s_objects_by_deployment_level(resources)

    # Validate that the objects are classified into the correct number of levels
    # Assuming every level is represented
    assert len(classified) == len(strategy_auto.DEPLOYMENT_LEVELS)

    # Check that each kind of object is classified into the correct level
    for level, kinds in strategy_auto.DEPLOYMENT_LEVELS.items():
        for kind in kinds:
            if classified_objects := classified.get(level):
                # Count how many objects of this kind are in the classified_objects for this level
                count_kind = sum(1 for obj in classified_objects if obj.kind == kind)
                # Now, assert based on expected counts per kind in your test resources
                expected_count = sum(1 for obj in resources if obj.kind == kind)
                assert (
                    count_kind == expected_count
                ), f"Mismatch for {kind} at level {level}"
            else:
                raise AssertionError(
                    f"No objects found at level {level}, expected {kind}"
                )


def test_strategy_auto_build_graph(resources: list[K8sObject]) -> None:
    strategy = strategy_auto.StrategyAuto()
    graph = strategy.build_deployment_graph(resources)

    # Verify that all nodes are present
    assert len(graph.nodes) == len(resources)

    # Verify dependencies between nodes
    for node in graph.nodes.values():
        expected_level = strategy_auto.KIND_TO_DEPLOYMENT_LEVEL.get(node.kind, -1)
        for dep_node in node.dependencies:
            dep_level = strategy_auto.KIND_TO_DEPLOYMENT_LEVEL.get(dep_node.kind, -1)
            assert (
                dep_level < expected_level
            )  # Dependency must be of a lower deployment level

    # check that except namespace all other objects are dependent on others
    # this is for this specific deployment.yml that contains all the objects including namespace
    # but in real world scenario, the deployment would not contain all the kinds
    for node in graph.nodes.values():
        if node.kind != "Namespace":
            assert len(node.dependencies) > 0, f"{node.kind} should depend in others"
