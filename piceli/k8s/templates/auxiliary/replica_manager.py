from abc import ABC, abstractmethod
from functools import cached_property
from typing import ClassVar, Optional

from kubernetes import client
from pydantic import BaseModel

from piceli.k8s.constants import policies
from piceli.k8s.templates.auxiliary import pod, resource_request
from piceli.k8s.templates.auxiliary.labels import Labels
from piceli.k8s.templates.deployable import autoscaler, service


class HPA(BaseModel):
    """HPA specification for ReplicaManager"""

    min_replicas: int
    max_replicas: int
    target_cpu_utilization_percentage: int

    def get_hpa(
        self, name: str, target_kind: str
    ) -> autoscaler.HorizontalPodAutoscaler:
        """Creates the K8s HPA spec"""
        return autoscaler.HorizontalPodAutoscaler(
            name=name,
            target_kind=target_kind,
            target_name=name,
            min_replicas=self.min_replicas,
            max_replicas=self.max_replicas,
            target_cpu_utilization_percentage=self.target_cpu_utilization_percentage,
        )


class VPA(BaseModel):
    """VPA specification for ReplicaManager"""

    min_allowed: resource_request.Resources
    max_allowed: resource_request.Resources
    control_cpu: bool = True
    control_memory: bool = True

    def get_vpa(self, name: str, target_kind: str) -> autoscaler.VerticalPodAutoscaler:
        """Creates the K8s VPA spec for the deployment"""
        return autoscaler.VerticalPodAutoscaler(
            name=name,
            target_kind=target_kind,
            target_name=name,
            container_name=None,
            min_allowed=self.min_allowed,
            max_allowed=self.max_allowed,
            control_cpu=self.control_cpu,
            control_memory=self.control_memory,
        )


class ReplicaManager(ABC, pod.Pod):
    """Common definition for ReplicaSet and StatefulSet"""

    restart_policy: policies.RestartPolicy = policies.RestartPolicy.ALWAYS
    replicas: int = 1
    create_service: bool = False
    hpa: Optional[HPA] = None
    vpa: Optional[VPA] = None
    labels: Optional[Labels] = None
    # API: ClassVar[str] = "apps"
    KIND: ClassVar[str] = ""

    def __post_init__(self) -> None:
        if len(self.name) > 15:
            # deployment name can be longer
            # but we use same name for the service ports name, that is limited to 15
            raise ValueError(f"Deployment name:'{self.name}' too long (max 15 chars)")
        # if not self.KIND:
        #     raise ValueError("KIND must be specified")

    @property
    def ports(self) -> list[client.V1ServicePort]:
        """gets the ports used by the containers"""
        return [
            service.ServicePort(
                name=p.name, port=p.port, target_port=p.target_port or p.port
            )
            for c in self.containers
            if c.ports
            for p in c.ports
        ]

    @abstractmethod
    def get_replica_manager(self) -> client.V1Deployment | client.V1StatefulSet:
        """gets the ReplicaManager definition"""

    def get_service(self) -> service.Service:
        """Gets the service related to this Deployment"""
        if not self.ports:
            raise AttributeError(
                "To create a service is necessary to specify ports in at least one container"
            )
        return service.Service(
            name=self.name,
            ports=self.ports,
            selector=self.get_pod_spec().metadata.labels,
        )

    @cached_property
    def target_kind(self) -> str:
        """Gets the target kind for the HPA"""
        replica_manager = self.get_replica_manager()
        return replica_manager.kind

    def get_hpa(self) -> Optional[autoscaler.HorizontalPodAutoscaler]:
        """Gets the HPA related to this Deployment"""
        if self.hpa:
            return self.hpa.get_hpa(self.name, self.target_kind)
        return None

    def get_vpa(self) -> Optional[autoscaler.VerticalPodAutoscaler]:
        """Gets the VPA related to this Deployment"""
        if self.vpa:
            return self.vpa.get_vpa(self.name, self.target_kind)
        return None

    def get(
        self,
    ) -> list[
        client.V1Deployment
        | client.V1StatefulSet
        | client.V1Service
        | client.V2HorizontalPodAutoscaler
        | dict
    ]:
        """gets the Job definition"""
        objects = [self.get_replica_manager()]
        if self.create_service:
            objects.append(self.get_service().get())
        if hpa := self.get_hpa():
            objects.extend(hpa.get())
        if vpa := self.get_vpa():
            objects.extend(vpa.get())
        return objects
