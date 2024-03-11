from kubernetes import client

from piceli.k8s.templates.auxiliary import replica_manager
from piceli.k8s.templates.deployable import base
from piceli.k8s.templates.deployable import volume as volume_lib


class StatefulSet(replica_manager.ReplicaManager, base.Deployable):
    """Simplification of K8s sateteful set"""

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
