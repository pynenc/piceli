from abc import ABC, abstractmethod

from piceli.k8s.k8s_objects.base import K8sObject
from piceli.k8s.ops.deploy.deployment_graph import DeploymentGraph


class DeploymentStrategy(ABC):
    @abstractmethod
    def build_deployment_graph(self, k8s_objects: list[K8sObject]) -> DeploymentGraph:
        pass
