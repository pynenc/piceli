from typing import Optional

from kubernetes import client

from piceli.k8s.templates.auxiliary import names
from piceli.k8s.templates.auxiliary.labels import Labels
from piceli.k8s.templates.deployable import base


class ConfigMap(base.Deployable):
    """Config Map"""

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
