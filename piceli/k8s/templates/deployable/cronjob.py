from kubernetes import client

from piceli.k8s.constants import policies
from piceli.k8s.templates.auxiliary import crontab
from piceli.k8s.templates.deployable import job


class CronJob(job.Job):
    "Kuberentes cronjob definition"

    schedule: crontab.CronTab
    # API: ClassVar[str] = "batch"
    # API_FUNC: ClassVar[str] = "cron_job"

    def get(self) -> list[client.V1CronJob]:
        """gets the CronJob definition"""
        if not self.schedule:
            raise ValueError("Schedule must be specified")
        obj = client.V1CronJob(
            api_version="batch/v1",
            kind="CronJob",
            metadata=client.V1ObjectMeta(name=self.name, labels=self.labels),
            spec=client.V1CronJobSpec(
                schedule=self.schedule,
                job_template=client.V1JobTemplateSpec(
                    spec=client.V1JobSpec(
                        backoff_limit=self.backoff_limit, template=self.get_pod_spec()
                    )
                ),
                concurrency_policy=policies.ConcurrencyPolicy.ALLOW.value,
            ),
        )
        return [obj]
