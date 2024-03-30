from kubernetes import client

from piceli.k8s import constants, templates
from tests.unit.templates import yaml_utils

CRONJOB = templates.CronJob(
    name="test-cronjob",
    containers=[
        templates.Container(
            name="test-cronjob", image="docker-image", command=["python", "--version"]
        )
    ],
    schedule=templates.crontab.daily_at_x(hour=6, minute=0),
)


def test_get_role_from_deployable() -> None:
    """test service_account"""
    roles = templates.Role.from_deployable(CRONJOB)
    assert len(roles) == 1
    assert isinstance(roles[0], templates.Role)
    objects = roles[0].get()
    assert len(objects) == 1
    assert isinstance(objects[0], client.V1Role)
    role_dict = client.ApiClient().sanitize_for_serialization(objects[0])
    assert role_dict == yaml_utils.get_yaml_dict("role.yml")


def test_service_account_and_role_bindings() -> None:
    roles = templates.Role.from_deployable(CRONJOB)
    assert len(roles) == 1
    service_account = templates.ServiceAccount(name="test-sa", roles=roles)
    objects = service_account.get()
    assert len(objects) == 3
    assert isinstance(objects[0], client.V1ServiceAccount)
    assert isinstance(objects[1], client.V1Role)
    assert isinstance(objects[2], client.V1RoleBinding)
    service_account_dict = client.ApiClient().sanitize_for_serialization(objects[0])
    role_dict = client.ApiClient().sanitize_for_serialization(objects[1])
    role_binding_dict = client.ApiClient().sanitize_for_serialization(objects[2])
    assert role_dict == yaml_utils.get_yaml_dict("role.yml")
    assert service_account_dict == yaml_utils.get_yaml_dict("service_account.yml")
    assert role_binding_dict == yaml_utils.get_yaml_dict("role_binding.yml")


def test_read_only_role() -> None:
    """Test read only role"""
    roles = templates.Role.from_deployable(
        CRONJOB, constants.APIRequestVerb.get_read_only()
    )
    assert len(roles) == 1
    assert isinstance(roles[0], templates.Role)
    objects = roles[0].get()
    assert len(objects) == 1
    assert isinstance(objects[0], client.V1Role)
    role_dict = client.ApiClient().sanitize_for_serialization(objects[0])
    assert role_dict == yaml_utils.get_yaml_dict("role_readonly.yml")
