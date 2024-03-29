import pytest

from piceli.k8s.k8s_objects.base import K8sObject, OriginYAML


@pytest.fixture
def k8s_objects() -> list[K8sObject]:
    return [
        K8sObject(
            spec={
                "apiVersion": "batch/v1",
                "kind": "CronJob",
                "metadata": {"name": "cronjob_name"},
            },
            origin=OriginYAML("some/path/cronjob.yml"),
        ),
        K8sObject(
            spec={
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "job_x"},
            },
            origin=OriginYAML("some/path/job.yml"),
        ),
    ]
