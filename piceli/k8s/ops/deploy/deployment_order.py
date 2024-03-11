# This module should handle the creation and manipulation of the graph,
# including adding nodes and edges (dependencies), identifying cycles (if any),
# and ordering nodes for deployment based on dependencies.
from collections import defaultdict

from piceli.k8s.k8s_objects.base import K8sObject


def classify_k8s_objects(k8s_objects: list[K8sObject]) -> dict[str, list[K8sObject]]:
    """
    Classifies the Kubernetes objects in the specified list by type.
    """
    classified_objects: dict[str, list[K8sObject]] = defaultdict(list)
    for k8s_object in k8s_objects:
        classified_objects[k8s_object.kind].append(k8s_object)
    return classified_objects
