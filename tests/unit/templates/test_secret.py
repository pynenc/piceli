from kubernetes import client

from piceli.k8s import constants, templates
from tests.unit.templates import yaml_utils


def test_secret_opaque() -> None:
    """test secret"""
    secret = templates.Secret(
        name="test-secret",
        secret_type=constants.SecretType.OPAQUE,
        string_data={"KEY0": "VALUE0"},
    )
    objects = secret.get()
    assert len(objects) == 1
    assert isinstance(objects[0], client.V1Secret)
    secret_dict = client.ApiClient().sanitize_for_serialization(objects[0])
    # on this test the yaml and dict contains stringData with the data dict without encode
    # when we do apply, this will change from stringData to data and will be encoded
    # so in the cluster:
    # kubectl get secrets test-secret -o yaml
    # will return data with fields encoded
    assert secret_dict == yaml_utils.get_yaml_dict("secret_opaque.yml")


def test_secret_json() -> None:
    """test secret"""
    docker_auth = (
        "eyJhdXRocyI6eyJnY3IuaW8iOnsiYXV0aCI6ImRtVnllVk5sWTNWeVpWQmhjM009In19fQ=="
    )
    secret = templates.Secret.get_docker_json_secret(
        "docker-secret", docker_auth=docker_auth
    )
    objects = secret.get()
    assert len(objects) == 1
    assert isinstance(objects[0], client.V1Secret)
    secret_dict = client.ApiClient().sanitize_for_serialization(objects[0])
    assert secret_dict == yaml_utils.get_yaml_dict("secret_json.yml")
