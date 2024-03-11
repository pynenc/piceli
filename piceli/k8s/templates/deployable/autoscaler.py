from typing import Optional

from kubernetes import client
from pydantic import Field, PositiveInt

from piceli.k8s.templates.auxiliary import names, resource_request
from piceli.k8s.templates.auxiliary.labels import Labels
from piceli.k8s.templates.deployable import base


class HorizontalPodAutoscaler(base.Deployable):
    """HorizontalPodAutoscaler"""

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
    """VerticalPodAutoscaler"""

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
