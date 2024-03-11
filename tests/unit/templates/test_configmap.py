from kubernetes import client

from piceli.k8s import templates
from tests.unit.templates import yaml_utils


def test_config_map() -> None:
    """test configmap"""
    configmap = templates.ConfigMap(name="test-cm", data={"KEY0": "VALUE0"})
    objects = configmap.get()
    assert len(objects) == 1
    assert isinstance(objects[0], client.V1ConfigMap)
    configmap_dict = client.ApiClient().sanitize_for_serialization(objects[0])
    assert configmap_dict == yaml_utils.get_yaml_dict("configmap.yml")
