from kubernetes import client

from piceli.k8s.constants import strategies
from piceli.k8s.templates.auxiliary import replica_manager


class Deployment(replica_manager.ReplicaManager):
    """
    Represents a Kubernetes Deployment object, extending ReplicaManager.

    A Deployment provides declarative updates for Pods and ReplicaSets. This class simplifies
    the deployment and scaling of applications, allowing users to specify desired states for
    replicable pods within a Kubernetes cluster. Inherits common properties and behavior from
    the ReplicaManager class.

    Attributes:
        Inherits all attributes from ReplicaManager.
    """

    def get_replica_manager(self) -> client.V1Deployment:
        """gets the Job definition"""
        pod_template = self.get_pod_spec()
        return client.V1Deployment(
            api_version="apps/v1",
            kind="Deployment",
            metadata=client.V1ObjectMeta(name=self.name, labels=self.labels),
            spec=client.V1DeploymentSpec(
                replicas=self.replicas,
                template=pod_template,
                selector=client.V1LabelSelector(
                    match_labels=pod_template.metadata.labels
                ),
                strategy=client.V1DeploymentStrategy(
                    type=strategies.DeploymentStrategyType.RECREATE.value
                ),
            ),
        )
