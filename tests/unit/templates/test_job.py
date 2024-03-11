from kubernetes import client

from piceli.k8s import templates
from tests.unit.templates import yaml_utils


def test_job() -> None:
    """test job"""
    job = templates.Job(
        name="test-job",
        image_pull_secrets=["docker-registry-credentials"],
        backoff_limit=1,
        containers=[
            templates.Container(
                name="test-job",
                command=["python", "--version"],
                image="docker-image",
                env={"K0": "V0"},
                liveness_command=[
                    "sh",
                    "-c",
                    "test $(expr $(date +%s) - $(cat /tmp/health_check)) -lt 60",
                ],
                resources=templates.Resources(
                    cpu="100m", memory="250Mi", ephemeral_storage="11Mi"
                ),
            )
        ],
        template_labels={"pod_name": "test-job"},
        labels={"job_name": "test-job"},
    )
    objects = job.get()
    assert len(objects) == 1
    assert isinstance(objects[0], client.V1Job)
    job_dict = client.ApiClient().sanitize_for_serialization(objects[0])
    assert job_dict == yaml_utils.get_yaml_dict("job.yml")
