from typing import Optional

from kubernetes import client
from pydantic import BaseModel

from piceli.k8s.templates.auxiliary import names
from piceli.k8s.templates.auxiliary.labels import Labels
from piceli.k8s.templates.deployable import base


class ServicePort(BaseModel):
    """Service Port"""

    name: str
    port: int
    target_port: int

    def get(self) -> client.V1ServicePort:
        """get the k8s object to apply"""
        return client.V1ServicePort(
            port=self.port, target_port=self.target_port, name=self.name
        )


class Service(base.Deployable):
    """k8s_client.Kubernetes Service"""

    name: names.Name
    ports: list[ServicePort]
    selector: dict
    labels: Optional[Labels] = None

    # API: ClassVar[str] = "core"
    # API_FUNC: ClassVar[str] = "service"

    def get(self) -> list[client.V1Service]:
        ports = [p.get() for p in self.ports]
        obj = client.V1Service(
            api_version="v1",
            kind="Service",
            metadata=client.V1ObjectMeta(name=self.name, labels=self.labels),
            spec=client.V1ServiceSpec(
                ports=ports, type="ClusterIP", selector=self.selector
            ),
        )
        return obj

    # def wait(self, k8s: k8s_client.Kubernetes) -> None:
    #     log.info("Waiting for service %s", self.name)
    #     for event in k8s.watch.stream(
    #         k8s.core_api.list_namespaced_endpoints,
    #         DEFAULT_NAMESPACE,
    #         field_selector=f"metadata.name={self.name}",
    #         timeout_seconds=WAIT_TIMEOUT,
    #     ):
    #         details = []
    #         for subset in getattr(event["object"], "subsets", []) or []:
    #             for address in subset.addresses or []:
    #                 details.append(
    #                     f"Endpoint({address.ip} --> {address.target_ref.kind} {address.target_ref.name})"
    #                 )
    #         if details:
    #             k8s.watch.stop()
    #             log.info(
    #                 "Done, found endpoints for service %s : %s", self.name, details
    #             )
    #             return
    #     raise RuntimeError(f"Service {self.name} is not available")
