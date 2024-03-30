from kubernetes import client

from piceli.k8s.templates.auxiliary import replica_manager
from piceli.k8s.templates.deployable import base
from piceli.k8s.templates.deployable import volume as volume_lib


class StatefulSet(replica_manager.ReplicaManager, base.Deployable):
    """
    Represents a Kubernetes StatefulSet object, extending ReplicaManager.

    A StatefulSet is used for managing stateful applications. It manages the deployment and scaling
    of a set of Pods and provides guarantees about the ordering and uniqueness of these Pods. Like
    Deployments, StatefulSets maintain a sticky identity for each of their Pods. This class provides
    a simplified interface for defining and managing StatefulSets, adding functionalities for volume
    management on top of the common replica management features.

    :param int replicas: Overrides ReplicaManager's default, setting the default number of replicas to 2 for StatefulSets.

    Inherits all other attributes from ReplicaManager.
    """

    replicas: int = 2

    def get_replica_manager(self) -> client.V1StatefulSet:
        """gets the Job definition"""
        pvc_templates = []
        for container in self.containers:
            for volume in container.volumes or []:
                if isinstance(volume, volume_lib.VolumeMountPVCTemplate):
                    pvc_templates.append(volume.pvc_template.get_template())
        pod_template = self.get_pod_spec()
        return client.V1StatefulSet(
            metadata=client.V1ObjectMeta(name=self.name, labels=self.labels),
            spec=client.V1StatefulSetSpec(
                replicas=self.replicas,
                template=pod_template,
                selector=client.V1LabelSelector(
                    match_labels=pod_template.metadata.labels
                ),
                volume_claim_templates=pvc_templates,
                service_name=self.name,
            ),
        )
