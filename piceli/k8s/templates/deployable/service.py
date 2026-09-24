from kubernetes import client
from pydantic import BaseModel

from piceli.k8s.templates.auxiliary import names
from piceli.k8s.templates.auxiliary.labels import Labels
from piceli.k8s.templates.deployable import base


class ServicePort(BaseModel):
    """
    Represents a single port exposed by a Kubernetes Service.

    Attributes:
        name (str): The name of the service port.
        port (int): The port number exposed by the service.
        target_port (int): The target port on the pod(s) to which traffic is forwarded.
    """

    name: str
    port: int
    target_port: int

    def get(self) -> client.V1ServicePort:
        """get the k8s object to apply"""
        return client.V1ServicePort(
            port=self.port, target_port=self.target_port, name=self.name
        )


class Service(base.Deployable):
    """
    Facilitates the definition and deployment of Kubernetes Service objects.

    Services enable networking access to a set of pods, based on labels and selectors,
    defining logical sets of pods and policies to access them.

    :param names.Name name: The name of the service, unique within the namespace.
    :param list[ServicePort] ports: List of ports that the service will expose.
    :param dict selector: Defines how the service selects pods to include in its set.
    :param Optional[Labels] labels: Custom labels to organize and select services within Kubernetes.
    """

    name: names.Name
    ports: list[ServicePort]
    selector: dict
    labels: Labels | None = None

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
        return [obj]
