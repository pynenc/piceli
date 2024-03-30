from typing import Optional

from kubernetes import client

from piceli.k8s.templates.auxiliary import names
from piceli.k8s.templates.auxiliary.labels import Labels
from piceli.k8s.templates.deployable import base


class ConfigMap(base.Deployable):
    """
    Represents a Kubernetes ConfigMap object within Piceli.

    ConfigMaps allow you to decouple configuration artifacts from image content,
    enhancing container application portability. This class provides a streamlined
    interface to define and manage ConfigMaps in a Kubernetes cluster.

    :param names.Name name: The name of the ConfigMap, adhering to Kubernetes naming conventions.
    :param dict[str, str] data: A dictionary representing the data stored in the ConfigMap as key-value pairs.
    :param Optional[Labels] labels: Optional. Labels to apply to the ConfigMap for better organization and discoverability.
    """

    name: names.Name
    data: dict[str, str]
    labels: Optional[Labels] = None
    # API: ClassVar[str] = "core"
    # API_FUNC: ClassVar[str] = "config_map"

    def get(self) -> list[client.V1ConfigMap]:
        """get the k8s object to apply"""
        obj = client.V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=client.V1ObjectMeta(name=self.name, labels=self.labels),
            data=self.data,
        )
        return [obj]
