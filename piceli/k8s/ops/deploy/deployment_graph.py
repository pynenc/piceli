from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import Iterable

from piceli.k8s.k8s_objects.base import K8sObject, K8sObjectIdentifier
from piceli.k8s.object_manager.base import ObjectManager


class DeploymentStatus(StrEnum):
    PENDING = auto()
    NO_ACTION_NEEDED = auto()
    DONE = auto()
    FAILED = auto()
    ROLLED_BACK = auto()


@dataclass
class ObjectNode:
    deploying_object: ObjectManager
    dependencies: set[K8sObjectIdentifier] = field(default_factory=set)
    previous_object: K8sObject | None = None
    deployment_status: DeploymentStatus = DeploymentStatus.PENDING

    @property
    def identifier(self) -> K8sObjectIdentifier:
        return self.deploying_object.k8s_object.identifier

    @property
    def kind(self) -> str:
        return self.deploying_object.k8s_object.kind


@dataclass
class DeploymentGraph:
    nodes: dict[K8sObjectIdentifier, ObjectNode] = field(default_factory=dict)
    out_of_model_objects: list[ObjectManager] = field(default_factory=list)

    def add_node(self, node: ObjectNode) -> None:
        self.nodes[node.identifier] = node

    def add_dependency(
        self, from_identifier: K8sObjectIdentifier, to_identifier: K8sObjectIdentifier
    ) -> None:
        if to_identifier not in self.nodes:
            raise ValueError(f"Dependency {to_identifier} does not exist in the graph")
        if from_identifier not in self.nodes:
            raise ValueError(f"Node {from_identifier} does not exist in the graph")
        if from_identifier == to_identifier:
            return None
        self.nodes[from_identifier].dependencies.add(to_identifier)

    def add_dependencies(
        self,
        from_identifiers: Iterable[K8sObjectIdentifier],
        to_identifiers: Iterable[K8sObjectIdentifier],
    ) -> None:
        for from_identifier in from_identifiers:
            for to_identifier in to_identifiers:
                self.add_dependency(from_identifier, to_identifier)

    def validate(self) -> None:
        visited: set[K8sObjectIdentifier] = set()
        stack: set[K8sObjectIdentifier] = set()

        def visit(node_id: K8sObjectIdentifier) -> None:
            if node_id in stack:
                raise ValueError(f"Cycle detected: {node_id} is in {stack}")
            if node_id not in visited:
                stack.add(node_id)
                visited.add(node_id)
                for next_node_id in self.nodes[node_id].dependencies:
                    visit(next_node_id)
                stack.remove(node_id)

        for node_id in self.nodes:
            visit(node_id)

    def traverse_graph(self) -> list[list[ObjectNode]]:
        """Organize nodes into levels for parallel deployment."""
        levels_dict: dict[int, list[ObjectNode]] = defaultdict(list)
        node_to_level: dict[K8sObjectIdentifier, int] = {}

        queue = deque(
            [(node, 0) for node in self.nodes.values() if not node.dependencies]
        )

        while queue:
            current_node, current_level = queue.popleft()

            levels_dict[current_level].append(current_node)
            node_to_level[current_node.identifier] = current_level

            for node_id, node in self.nodes.items():
                if current_node.identifier in node.dependencies:
                    # This node depends on the current node; assign it to the next level if not already assigned
                    # or if it's been assigned to a higher level
                    new_level = current_level + 1
                    if (
                        node_id not in node_to_level
                        or node_to_level[node_id] > new_level
                    ):
                        node_to_level[node_id] = new_level
                        queue.append((node, new_level))

        return [levels_dict[level] for level in sorted(levels_dict)]
