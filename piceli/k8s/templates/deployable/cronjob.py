from kubernetes import client

from piceli.k8s.constants import policies
from piceli.k8s.templates.auxiliary import crontab
from piceli.k8s.templates.deployable import job


class CronJob(job.Job):
    """
    Defines a Kubernetes CronJob for scheduled job execution.

    Extends the functionality of the `Job` class to include scheduling,
    allowing jobs to be executed at predefined times or intervals.

    :param crontab.CronTab schedule: The schedule on which the job should run, specified in CronTab format.

    Inherits `name`, `labels`, `backoff_limit`, and pod spec configuration from the `Job` class.
    """

    schedule: crontab.CronTab

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
