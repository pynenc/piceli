from collections import defaultdict

from piceli.k8s.k8s_objects.base import K8sObject, K8sObjectIdentifier
from piceli.k8s.object_manager.factory import ManagerFactory
from piceli.k8s.ops.deploy.deployment_graph import (
    DeploymentGraph,
    DeploymentStatus,
    ObjectNode,
)
from piceli.k8s.ops.deploy.strategy_base import DeploymentStrategy

DEPLOYMENT_LEVELS = {
    0: ["Namespace"],
    1: [
        "CustomResourceDefinition",
        "StorageClass",
        "Role",
        "ClusterRole",
        "ServiceAccount",
    ],
    2: ["RoleBinding", "ClusterRoleBinding"],
    3: ["Secret", "ConfigMap", "PersistentVolume"],
    4: ["PersistentVolumeClaim"],
    5: ["Deployment", "StatefulSet", "DaemonSet"],
    6: ["Service"],
    7: ["Job", "CronJob"],
    8: [
        "Ingress",
        "NetworkPolicy",
        "PodDisruptionBudget",
        "HorizontalPodAutoscaler",
        "VerticalPodAutoscaler",
    ],
}

KIND_TO_DEPLOYMENT_LEVEL = {
    kind: level for level, kinds in DEPLOYMENT_LEVELS.items() for kind in kinds
}


def classify_k8s_objects_by_deployment_level(
    k8s_objects: list[K8sObject],
) -> dict[int, list[K8sObject]]:
    """
    Classifies the Kubernetes objects in the specified list by their deployment level.
    """
    classified_objects: dict[int, list[K8sObject]] = defaultdict(list)
    for k8s_object in k8s_objects:
        # Look up the deployment level directly using the kind-to-level mapping
        level = KIND_TO_DEPLOYMENT_LEVEL.get(
            k8s_object.kind, -1
        )  # Use -1 or other default if kind is not found
        if level != -1:  # If level is found
            classified_objects[level].append(k8s_object)
    return classified_objects


class StrategyAuto(DeploymentStrategy):
    """
    Automatic strategy for building a deployment graph.

    This trategy classify the objects by its kind, eg: ConfigMap will run before Deployment.
    """

    def build_deployment_graph(self, k8s_objects: list[K8sObject]) -> DeploymentGraph:
        graph = DeploymentGraph()
        classified_objects = classify_k8s_objects_by_deployment_level(k8s_objects)

        previous_level_identifiers: set[K8sObjectIdentifier] = set()
        for level in sorted(classified_objects):
            current_level_identifiers = set()
            for obj in classified_objects[level]:
                node = ObjectNode(
                    deploying_object=ManagerFactory.get_manager(obj),
                    dependencies=set(),
                    deployment_status=DeploymentStatus.PENDING,
                )
                graph.add_node(node)
                current_level_identifiers.add(node.identifier)
            graph.add_dependencies(
                from_identifiers=current_level_identifiers,
                to_identifiers=previous_level_identifiers,
            )
            previous_level_identifiers = current_level_identifiers

        return graph
