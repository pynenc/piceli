from piceli.k8s.utils import utils_object


def test_get_object_group_and_version(resource_dict: dict) -> None:
    group, version = utils_object.get_object_group_and_version(resource_dict)
    assert group == "Batch"
    assert version == "v1"


def test_get_api_name(resource_dict: dict) -> None:
    api_name = utils_object.get_api_name("Batch", "v1")
    assert api_name == "BatchV1Api"
