from typing import Optional

from pydantic import BaseModel, Field
from typing_extensions import Annotated

from piceli.k8s.templates.auxiliary import names

_Port = Annotated[int, Field(gt=0, lt=65536)]


class Port(BaseModel):
    """
    Represents a network port configuration for a Kubernetes service or pod.

    Attributes:
        :param names.Name name: The name of the port. Used for identification and must be unique within the set of ports in the service or pod.
        :param _Port port: The port number that the service or pod exposes.
        :param Optional[_Port] target_port: The port on the container to which traffic should be forwarded. If unspecified, defaults to the value of `port`.

    This class is utilized across Piceli templates to define how services and pods are accessed within the cluster, playing a crucial role in the network communication setup for Kubernetes resources.
    """

    name: names.Name
    port: _Port
    target_port: Optional[_Port] = None
