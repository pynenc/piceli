from dataclasses import dataclass
from enum import Enum
from typing import Optional

from piceli.k8s.config import kubeconfig
from piceli.k8s.utils import utils_object


class Orgins(Enum):
    """Represents the origin of a Kubernetes object"""

    TEMPLATE = "template"
    """The object was created from a template"""
    YAML = "yaml"
    """The object was created from a YAML file"""
    K8S_LIB = "k8s_lib"
    """The object was created from the Kubernetes open source library"""


class ObjectOrigin:
    """Represents the origin of a Kubernetes object."""


@dataclass
class OriginYAML(ObjectOrigin):
    """The object was created from a YAML file"""

    path: str


@dataclass
class OriginJSON(ObjectOrigin):
    """The object was created from a JSON file"""

    path: str


@dataclass
class OriginTemplate(ObjectOrigin):
    """The object was created from a template"""

    module: str
    name: str


@dataclass
class OriginK8sLib(ObjectOrigin):
    """The object was created from the Kubernetes open source library"""

    module: str
    name: str


@dataclass
class OriginCluster(ObjectOrigin):
    """The object was created from a cluster"""

    ctx: Optional[kubeconfig.KubeConfig]
    namespace: Optional[str]


@dataclass(eq=True, frozen=True)
class K8sObjectIdentifier:
    name: str
    kind: str
    namespace: Optional[str] = None

    def __hash__(self) -> int:
        return hash((self.name, self.kind, self.namespace))


@dataclass
class K8sObject:
    """Represents a Kubernetes object"""

    spec: dict
    origin: ObjectOrigin

    def __post_init__(self) -> None:
        self._name = self.spec["metadata"]["name"]
        self._group, self._version = utils_object.get_object_group_and_version(
            self.spec
        )
        self.api_name = utils_object.get_api_name(self._group, self._version)
        self._namespace = self.spec["metadata"].get("namespace")

    @property
    def identifier(self) -> K8sObjectIdentifier:
        """Object identifier."""
        return K8sObjectIdentifier(self._name, self.kind, self.namespace)

    @property
    def unnamespaced_id(self) -> K8sObjectIdentifier:
        """Object identifier without namespace."""
        return K8sObjectIdentifier(self._name, self.kind, None)

    @property
    def name(self) -> str:
        return self._name

    @property
    def group(self) -> str:
        return self._group

    @property
    def version(self) -> str:
        return self._version

    @property
    def kind(self) -> str:
        return self.spec["kind"]

    @property
    def namespace(self) -> Optional[str]:
        return self._namespace

    def __str__(self) -> str:
        return f"{self.kind} {self.name} ({self.origin})"
