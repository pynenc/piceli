from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Union, overload

from kubernetes import client
from kubernetes.utils.quantity import parse_quantity


@dataclass(frozen=True)
class Resources:
    """
    Represents Kubernetes resources, facilitating arithmetic operations and validation.

    Attributes:
        :param Optional[str] memory: The amount of memory allocated for the resource. Specified in Kubernetes quantity string format (e.g., '200Mi', '1Gi').
        :param Optional[str] cpu: The amount of CPU allocated for the resource. Specified in Kubernetes quantity string format (e.g., '100m' for 100 millicpu).
        :param Optional[str] ephemeral_storage: The amount of ephemeral storage allocated for the resource. Specified in Kubernetes quantity string format.

    The class supports creating `Resources` objects from dictionaries, converting between string and numerical representations, and performing arithmetic operations on resource quantities. These capabilities are essential for managing resource allocations in Kubernetes deployments.
    """

    memory: Optional[str] = None
    cpu: Optional[str] = None
    ephemeral_storage: Optional[str] = None

    @classmethod
    def from_dict(cls, resources: dict[str, str]) -> "Resources":
        """
        Creates a `Resources` object from a dictionary of string quantities.

        :param dict[str, str] resources: A dictionary where keys are resource types ('memory', 'cpu', 'ephemeral-storage') and values are quantities in string format.
        :return: A `Resources` object with the specified resources.
        """
        return cls(
            **{
                k.replace("-", "_"): v
                for k, v in resources.items()
                if k in ["memory", "cpu", "ephemeral-storage", "ephemeral_storage"]
            }
        )

    @classmethod
    def from_quantity_dict(
        cls, resources: dict[str, int | float | None]
    ) -> "Resources":
        """
        Creates a `Resources` object from a dictionary of quantities, converting them to string format.

        :param dict[str, int | float | None] resources: A dictionary where keys are resource types and values are quantities in kilo, mega, etc.
        :return: A `Resources` object with resources converted to Kubernetes-compatible string representations.
        """
        return cls(
            **{
                k.replace("-", "_"): Resources.bytes_to_str(v, k)
                for k, v in resources.items()
                if k in ["memory", "cpu", "ephemeral-storage", "ephemeral_storage"]
                and v is not None
                and v != 0
            }
        )

    def to_dict(self) -> dict[str, str]:
        """creates a dict from a Resources object"""
        return {
            k.replace("_", "-"): v
            for k, v in self.__dict__.items()
            if k in ["memory", "cpu", "ephemeral_storage"]
        }

    def to_quantity_dict(self) -> dict[str, Optional[float]]:
        """creates a dict from a Resources object containing the quantity in k"""
        return {
            k.replace("_", "-"): float(parse_quantity(v)) if v else None
            for k, v in self.__dict__.items()
            if k in ["memory", "cpu", "ephemeral_storage"]
        }

    def __post_init__(self) -> None:
        _resources: dict[str, float] = {}
        if self.memory:
            _resources["memory"] = float(parse_quantity(self.memory))
        if self.cpu:
            _resources["cpu"] = float(parse_quantity(self.cpu))
        if self.ephemeral_storage:
            _resources["ephemeral_storage"] = float(
                parse_quantity(self.ephemeral_storage)
            )
        object.__setattr__(self, "_resources", _resources)

    def get_k8s_request(self) -> dict:
        """returns a dict compatible with kubernetes client"""
        # Note different between self.ephemeral_storage and key "ephemeral-storage"
        return {
            "memory": self.memory or "250Mi",
            "cpu": self.cpu or "100m",
            "ephemeral-storage": self.ephemeral_storage or "11Mi",
        }

    def get_quantity(self, resource: str) -> int | float:
        """gets the quantity in k from a resource (cpu can be a float)"""
        return object.__getattribute__(self, "_resources").get(resource, 0)

    @staticmethod
    def _bytes_to_str(num: float, base: int, suffix: str) -> str:
        for unit in ["", "K", "M", "G", "T", "P"]:
            if abs(num) < base:
                return f"{num}{unit}{suffix}"
            num /= base
        return f"{num}E{suffix}"

    @staticmethod
    def _cores_to_str(num: float) -> str:
        if num < 0.001:
            return "1m"
        if num < 1:
            return f"{num*1000}m"
        return f"{num}"

    @staticmethod
    def bytes_to_str(num: float, resource: str) -> str:
        """gets a human readable string representation"""
        if resource == "cpu":
            return Resources._cores_to_str(num)
        # mem and ephemeral in power of 2 representation by default
        return Resources._bytes_to_str(num, 1024, "i")

    @property
    def resources(self) -> list[str]:
        """gets a list of the resources specified in this object"""
        return list(object.__getattribute__(self, "_resources").keys())

    def __add__(self, other: "Resources") -> "Resources":
        kwargs: dict[str, str] = {}
        for arg in set(self.resources).union(set(other.resources)):
            # if arg specified in any of Resources add both (0 by default), otherwise keep it None
            if arg in self.resources or arg in other.resources:
                kwargs[arg] = self.bytes_to_str(
                    self.get_quantity(arg) + other.get_quantity(arg), arg
                )
        return Resources(**kwargs)

    def __sub__(self, other: "Resources") -> "Resources":
        kwargs: dict[str, str] = {}
        for arg in set(self.resources).union(set(other.resources)):
            # if arg specified in any of Resources add both (0 by default), otherwise keep it None
            if arg in self.resources or arg in other.resources:
                kwargs[arg] = self.bytes_to_str(
                    self.get_quantity(arg) - other.get_quantity(arg), arg
                )
        return Resources(**kwargs)

    def __abs__(self) -> "Resources":
        kwargs: dict[str, str] = {}
        for arg in set(self.resources):
            kwargs[arg] = self.bytes_to_str(abs(self.get_quantity(arg)), arg)
        return Resources(**kwargs)

    def __mul__(self, other: float | int) -> "Resources":
        if isinstance(other, (float, int)):
            if other == 0:
                return Resources()
            kwargs: dict[str, str] = {}
            for arg in set(self.resources):
                if arg == "cpu":
                    kwargs[arg] = self.bytes_to_str(
                        float(self.get_quantity(arg)) * other, arg
                    )
                else:
                    kwargs[arg] = self.bytes_to_str(
                        int(self.get_quantity(arg)) * other, arg
                    )
            return Resources(**kwargs)
        return NotImplemented

    @overload
    def __truediv__(self, other: float | int) -> "Resources":
        """divide each resource by the float argument"""

    @overload
    def __truediv__(self, other: "Resources") -> float:
        """this will return the ratio between the two Resources (based in the max ratio of any resource)"""

    def __truediv__(
        self, other: Union[float, int, "Resources"]
    ) -> Union["Resources", float]:
        if isinstance(other, (float, int)):
            kwargs: dict[str, str] = {}
            for arg in set(self.resources):
                if arg == "cpu":
                    kwargs[arg] = self.bytes_to_str(
                        float(self.get_quantity(arg)) / other, arg
                    )
                else:
                    kwargs[arg] = self.bytes_to_str(
                        int(self.get_quantity(arg)) / other, arg
                    )
            return Resources(**kwargs)
        if isinstance(other, Resources):
            result = 0.0
            for arg in set(self.resources).union(set(other.resources)):
                # if arg specified in any of Resources add both (0 by default), otherwise keep it None
                if arg in self.resources and arg in other.resources:
                    result = max(
                        result,
                        float(self.get_quantity(arg)) / float(other.get_quantity(arg)),
                    )
            return result
        return NotImplemented

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Resources):
            return False
        if self.resources != other.resources:
            return False
        for resource in self.resources:
            if self.get_quantity(resource) != other.get_quantity(resource):
                return False
        return True

    def any_lower(self, other: "Resources") -> bool:
        """if any of the values is lower it will return True"""
        if not isinstance(other, Resources):
            return NotImplemented
        for arg in set(self.resources).union(set(other.resources)):
            # if arg specified in any of Resources add both (0 by default), otherwise keep it None
            if arg in self.resources or arg in other.resources:
                if self.get_quantity(arg) < other.get_quantity(arg):
                    return True
        return False

    def any_greater(self, other: "Resources") -> bool:
        """if any of the values is greater it will return True"""
        if not isinstance(other, Resources):
            return NotImplemented
        for arg in set(self.resources).union(set(other.resources)):
            # if arg specified in any of Resources add both (0 by default), otherwise keep it None
            if arg in self.resources or arg in other.resources:
                if self.get_quantity(arg) > other.get_quantity(arg):
                    return True
        return False

    def get_cpu_memory_ratio(self) -> float:
        """returns the ratio between cpu and memory (CPU:memory ratio (vCPU:GiB)"""
        if self.get_quantity("cpu") == 0 or self.get_quantity("memory") == 0:
            return 0.0
        return float(
            self.get_quantity("memory") / 1073741824 / self.get_quantity("cpu")
        )


@dataclass
class ContainerResourcesData:
    """Resources of a kubernetes container"""

    container_name: str
    requested_resources: Resources
    used_resources: Resources

    @property
    def max_usage(self) -> Optional[float]:
        """returns the max usage of the pod max(used_cpu/requested_cpu, used_memory/requested_memory)"""
        if (
            self.used_resources == Resources()
            or self.requested_resources == Resources()
        ):
            return None
        requested_dict = self.requested_resources.to_quantity_dict()
        used_dict = self.used_resources.to_quantity_dict()
        max_usage = 0.0
        for resource, requested in requested_dict.items():
            if resource == "ephemeral-storage":
                continue
            if not isinstance(requested, (int, float)):
                continue
            if used := used_dict.get(resource):
                if not isinstance(used, (int, float)) and used == 0:
                    continue
                max_usage = max(max_usage, used / requested)
        return max_usage

    def __str__(self) -> str:
        return (
            f"Container {self.container_name} resources(use/req): "
            + f"cpu({self.used_resources.cpu}/{self.requested_resources.cpu}), "
            + f"mem({self.used_resources.memory}/{self.requested_resources.memory}), "
            + f"eph({self.used_resources.ephemeral_storage}/{self.requested_resources.ephemeral_storage}), "
        )


@dataclass
class ResourcesData:
    """Resources of a kubernetes pod"""

    pod_name: str
    labels: dict[str, str]
    node_name: Optional[str]
    pod_status: Optional[str]
    pod_status_reason: Optional[str]
    pod_status_message: Optional[str]
    last_update: Optional[datetime]
    containers: dict[str, ContainerResourcesData] = field(default_factory=dict)

    @property
    def max_usage(self) -> Optional[float]:
        """returns the max usage of the pod max(used_cpu/requested_cpu, used_memory/requested_memory)"""
        max_usage = 0.0
        for container in self.containers.values():
            if container.max_usage:
                max_usage = max(max_usage, container.max_usage)
        return max_usage

    @property
    def summary(self) -> list[str]:
        """Returns a list of string summarizing pod data"""
        summary = [
            f"Pod {self.pod_name} (status: {self.pod_status}, max_usage: {self.max_usage})"
        ]
        for container in self.containers.values():
            summary.append(f"  {container}")
        return summary


@dataclass
class NodeResources:
    """Resources of a kubernetes node"""

    node_name: str
    allocatable: Resources
    capacity: Resources
    allocatable_pods: int
    capacity_pods: int

    @classmethod
    def from_node_status(cls, node: client.V1Node) -> "NodeResources":
        """creates a ClusterResources object from a list of kubernetes nodes"""
        node_name = node.metadata.name
        allocatable = Resources.from_dict(node.status.allocatable)
        capacity = Resources.from_dict(node.status.capacity)
        capacity_pods = int(node.status.capacity["pods"])
        allocatable_pods = int(node.status.allocatable["pods"])
        return cls(node_name, allocatable, capacity, allocatable_pods, capacity_pods)


@dataclass
class ClusterResources:
    """Resources of a kubernetes cluster"""

    total_allocatable: Resources
    total_capacity: Resources
    total_allocatable_pods: int
    total_capacity_pods: int
    nodes: list[NodeResources]
    pods_map: dict[str, ResourcesData]

    @property
    def pods(self) -> list[ResourcesData]:
        """returns a list of pods"""
        return list(self.pods_map.values())

    def get_pod(self, pod_name: str) -> ResourcesData | None:
        """returns a pod by name"""
        for pod in self.pods:
            if pod.labels["pod_name"] == pod_name:
                return pod
        for pod_id in self.pods_map:
            if pod_id.startswith(pod_name):
                return self.pods_map[pod_id]
        return None

    @classmethod
    def from_cluster_info(
        cls,
        nodes: list[client.V1Node],
        pods: list[client.V1Pod],
        pods_metrics: list[dict],
    ) -> "ClusterResources":
        """creates a ClusterResources object from a list of kubernetes nodes"""
        nodes_resources = []
        total_allocatable = Resources()
        total_capacity = Resources()
        total_allocatable_pods = 0
        total_capacity_pods = 0
        for node in nodes:
            nodes_resources.append(NodeResources.from_node_status(node))
            total_allocatable += nodes_resources[-1].allocatable
            total_capacity += nodes_resources[-1].capacity
            total_allocatable_pods += nodes_resources[-1].allocatable_pods
            total_capacity_pods += nodes_resources[-1].capacity_pods
        pods_map: dict[str, ResourcesData] = {}
        for pod in pods:
            pods_map[pod.metadata.name] = ResourcesData(
                pod_name=pod.metadata.name,
                labels=pod.metadata.labels,
                pod_status=pod.status.phase,
                pod_status_reason=pod.status.reason,
                pod_status_message=pod.status.message,
                node_name=pod.spec.node_name,
                last_update=None,
            )
            last_update = (
                pod.metadata.deletion_timestamp or pod.metadata.creation_timestamp
            )
            if pod.status.conditions:
                last_update = max(
                    last_update,
                    max(
                        c.last_transition_time
                        for c in pod.status.conditions
                        if c.last_transition_time
                    ),
                )
            for container_status in pod.status.container_statuses or []:
                if container_status.state.terminated:
                    last_update = max(
                        last_update, container_status.state.terminated.finished_at
                    )
                    if container_status.state.terminated.reason == "OOMKilled":
                        pods_map[pod.metadata.name].pod_status = "OOMKilled"
                        pods_map[pod.metadata.name].pod_status_reason = "OOMKilled"
            for container in pod.spec.containers:
                pods_map[pod.metadata.name].containers[
                    container.name
                ] = ContainerResourcesData(
                    container_name=container.name,
                    requested_resources=Resources.from_dict(
                        container.resources.requests
                    ),
                    used_resources=Resources(),
                )
            pods_map[pod.metadata.name].last_update = last_update
        for pod_metrics in pods_metrics:
            # we may have filter pods by labes, then we ignore other pods metrics
            if _pod := pods_map.get(pod_metrics["metadata"]["name"]):
                for container in pod_metrics["containers"]:
                    if (c_name := container["name"]) in _pod.containers:
                        _pod.containers[c_name].used_resources = Resources.from_dict(
                            container["usage"]
                        )
                    else:
                        _pod.containers[c_name] = ContainerResourcesData(
                            container_name=c_name,
                            requested_resources=Resources(),
                            used_resources=Resources.from_dict(container["usage"]),
                        )
                pods_map[_pod.pod_name] = _pod

        return cls(
            total_allocatable,
            total_capacity,
            total_allocatable_pods,
            total_capacity_pods,
            nodes_resources,
            pods_map,
        )
