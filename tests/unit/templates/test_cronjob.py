from kubernetes import client

from piceli.k8s import constants, templates
from tests.unit.templates import yaml_utils


def test_cronjob() -> None:
    """test cronjob"""
    cronjob = templates.CronJob(
        name="test-cronjob",
        template_labels={"pod_name": "test-cronjob"},
        labels={"cronjob_name": "test-cronjob"},
        containers=[
            templates.Container(
                name="test-cronjob",
                image="docker-image",
                image_pull_policy=constants.ImagePullPolicy.ALWAYS,
                command=["python", "--version"],
                env={"K0": "V0"},
            )
        ],
        service_account=templates.ServiceAccount(name="test-sa", roles=[]),
        automount_service_account_token=True,
        schedule=templates.crontab.daily_at_x(hour=6, minute=0),
        image_pull_secrets=["docker-registry-credentials"],
    )
    objects = cronjob.get()
    assert len(objects) == 1
    assert isinstance(objects[0], client.V1CronJob)
    cronjob_dict = client.ApiClient().sanitize_for_serialization(objects[0])
    assert cronjob_dict == yaml_utils.get_yaml_dict("cronjob.yml")
