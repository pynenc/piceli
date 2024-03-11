from typing import Optional

from kubernetes import client
from pydantic import NonNegativeInt, model_validator

from piceli.k8s.constants import policies
from piceli.k8s.templates.auxiliary import pod
from piceli.k8s.templates.auxiliary.labels import Labels
from piceli.k8s.templates.deployable import base


class Job(pod.Pod, base.Deployable):
    """k8s_client.Kubernetes Job"""

    cleanup_after_seconds: Optional[NonNegativeInt] = None
    backoff_limit: Optional[NonNegativeInt] = None
    labels: Optional[Labels] = None
    # API: ClassVar[str] = "batch"
    # API_FUNC: ClassVar[str] = "job"

    @model_validator(mode="before")
    def check_restart_policy(cls, values: dict) -> dict:
        restart_policy = values.get("restart_policy", policies.RestartPolicy.NEVER)
        if restart_policy == policies.RestartPolicy.ALWAYS:
            # From k8s error: "spec.template.spec.restartPolicy: Required value: valid values: "OnFailure", "Never""
            # Job does not support Retry Policy Always
            # https://kubernetes.io/docs/concepts/workloads/controllers/job/#single-job-starts-controller-pod
            raise ValueError("Job does not support RestartPolicy.ALWAYS")
        return values

    def get(self) -> list[client.V1Job]:
        """gets the Job definition"""
        obj = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(name=self.name, labels=self.labels),
            spec=client.V1JobSpec(
                backoff_limit=self.backoff_limit,
                template=self.get_pod_spec(),
                ttl_seconds_after_finished=self.cleanup_after_seconds,
            ),
        )
        return [obj]

    # def wait(self, k8s: k8s_client.Kubernetes) -> None:
    #     self._wait(
    #         k8s=k8s,
    #         func=k8s.batch_api.list_namespaced_job,
    #         args=(DEFAULT_NAMESPACE,),
    #         condition=k8s_client.WaitConditionJob.COMPLETE,
    #     )
