from typing import Optional

from kubernetes import client
from pydantic import Field, PositiveInt

from piceli.k8s.templates.auxiliary import names, resource_request
from piceli.k8s.templates.auxiliary.labels import Labels
from piceli.k8s.templates.deployable import base


class HorizontalPodAutoscaler(base.Deployable):
    """
    **Defines a Horizontal Pod Autoscaler (HPA) for Kubernetes.**

    :param names.Name name: The name of the Horizontal Pod Autoscaler.
    :param str target_kind: The kind of the target to scale (e.g., Deployment, ReplicaSet).
    :param str target_name: The name of the target to scale.
    :param PositiveInt min_replicas: The minimum number of replicas.
    :param PositiveInt max_replicas: The maximum number of replicas.
    :param int target_cpu_utilization_percentage: The target CPU utilization percentage to trigger scaling, between 1 and 100.
    :param Optional[Labels] labels: Optional labels to apply to the HPA object.

    The `HorizontalPodAutoscaler` class facilitates the creation of an HPA resource in Kubernetes,
    allowing applications to automatically scale in or out based on specified CPU utilization metrics.

    ### Usage

    The HPA object created by this class is intended to be used with the piceli to apply
    the autoscaling behavior to a specified deployment or other scalable workload.

    ### Examples
    ```{code-block} python
    hpa = HorizontalPodAutoscaler(
        name="example-hpa",
        target_kind="Deployment",
        target_name="example-deployment",
        min_replicas=1,
        max_replicas=10,
        target_cpu_utilization_percentage=80
    )
    ```
    This example demonstrates how to define an HPA targeting a Kubernetes Deployment
    to maintain CPU utilization at or below 80%.
    """

    name: names.Name
    target_kind: str
    target_name: str
    min_replicas: PositiveInt
    max_replicas: PositiveInt
    target_cpu_utilization_percentage: int = Field(ge=1, le=100)
    labels: Optional[Labels] = None

    def get(self) -> list[client.V2HorizontalPodAutoscaler]:
        obj = client.V2HorizontalPodAutoscaler(
            kind="HorizontalPodAutoscaler",
            metadata=client.V1ObjectMeta(name=self.name, labels=self.labels),
            spec=client.V2HorizontalPodAutoscalerSpec(
                scale_target_ref=client.V2CrossVersionObjectReference(
                    kind=self.target_kind,
                    name=self.target_name,
                ),
                min_replicas=self.min_replicas,
                max_replicas=self.max_replicas,
                # target_cpu_utilization_percentage=target_cpu_utilization_percentage,
                metrics=[
                    client.V2MetricSpec(
                        type="Resource",
                        resource=client.V2ResourceMetricSource(
                            name="cpu",
                            target=client.V2MetricTarget(
                                type="Utilization",
                                average_utilization=self.target_cpu_utilization_percentage,
                            ),
                        ),
                    )
                ],
            ),
        )
        return [obj]


class VerticalPodAutoscaler(base.Deployable):
    """
    **Defines a Vertical Pod Autoscaler (VPA) for Kubernetes.**

    :param names.Name name: The name of the Vertical Pod Autoscaler.
    :param str target_kind: The kind of the target for the VPA (e.g., Deployment, StatefulSet).
    :param str target_name: The name of the target for the VPA.
    :param Optional[str] container_name: Optional name of the specific container to target within the pod.
    :param resource_request.Resources min_allowed: The minimum resources allowed for each container.
    :param resource_request.Resources max_allowed: The maximum resources allowed for each container.
    :param bool control_cpu: Indicates whether the CPU resource should be managed.
    :param bool control_memory: Indicates whether the memory resource should be managed.

    The `VerticalPodAutoscaler` class enables automatic adjustment of pod resources (CPU and memory)
    based on usage, potentially leading to better resource utilization and application performance.

    ### Usage

    The VPA object produced is designed to interact with the Kubernetes VPA resource,
    guiding the automatic resource allocation for pods within a specified target workload.

    ### Examples
    ```{code-block} python
    vpa = VerticalPodAutoscaler(
        name="example-vpa",
        target_kind="Deployment",
        target_name="example-deployment",
        min_allowed=resource_request.Resources(cpu="250m", memory="512Mi"),
        max_allowed=resource_request.Resources(cpu="2", memory="2Gi"),
        control_cpu=True,
        control_memory=True
    )
    ```
    This snippet creates a VPA that automatically adjusts CPU and memory requests
    within specified limits for a Kubernetes Deployment.
    """

    name: names.Name
    target_kind: str
    target_name: str
    container_name: Optional[str]
    min_allowed: resource_request.Resources
    max_allowed: resource_request.Resources
    control_cpu: bool
    control_memory: bool

    def get(self) -> list[dict]:
        """Creates the K8s VPA spec"""
        spec: dict = {
            "kind": "VerticalPodAutoscaler",
            "metadata": {"name": self.name},
            "spec": {
                "targetRef": {
                    "apiVersion": "apps/v1",
                    "kind": self.target_kind,
                    "name": self.target_name,
                },
            },
        }

        update_policy: dict = {"updateMode": "Auto"}
        if not self.control_cpu and not self.control_memory:
            raise ValueError(
                "at least one of control_cpu or control_memory must be True"
            )
        if not self.control_cpu or not self.control_memory:
            update_policy["controlledResources"] = (
                ["cpu"] if self.control_cpu else ["memory"]
            )
        if self.min_allowed or self.max_allowed:
            container_policy: dict = (
                {"containerName": self.container_name} if self.container_name else {}
            )
            if self.min_allowed:
                container_policy["minAllowed"] = {
                    k: v
                    for k, v in self.min_allowed.to_dict().items()
                    if k != "ephemeral-storage"
                }
            if self.max_allowed:
                container_policy["maxAllowed"] = {
                    k: v
                    for k, v in self.max_allowed.to_dict().items()
                    if k != "ephemeral-storage"
                }
            spec["spec"]["resourcePolicy"] = {"containerPolicies": [container_policy]}
        spec["spec"]["updatePolicy"] = update_policy
        return [spec]

    # TODO: apply should use kind of the dictionary to map to the methods that apply,get,delete the VPA

    # def apply(
    #     self,
    #     k8s: k8s_client.Kubernetes,
    #     async_req: bool = False,
    #     dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    # ) -> ApplyResult:
    #     """Applied the VPA"""

    #     group = "autoscaling.k8s.io"
    #     version = "v1"
    #     plural = "verticalpodautoscalers"

    #     vpa = self.get()
    #     vpa["apiVersion"] = f"{group}/{version}"
    #     try:
    #         return k8s.custom_api.patch_namespaced_custom_object(
    #             group,
    #             version,
    #             DEFAULT_NAMESPACE,
    #             plural,
    #             self.name,
    #             vpa,
    #             async_req=async_req,
    #             dry_run=dry_run.value,
    #         )
    #     except ApiException as ex:
    #         if json.loads(ex.body).get("reason") == "NotFound":
    #             return k8s.custom_api.create_namespaced_custom_object(
    #                 group,
    #                 version,
    #                 DEFAULT_NAMESPACE,
    #                 plural,
    #                 vpa,
    #                 async_req=async_req,
    #                 dry_run=dry_run.value,
    #             )
    #         raise
