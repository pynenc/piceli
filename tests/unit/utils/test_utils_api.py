from kubernetes import client

from piceli.k8s.utils import utils_api

RESOURCE_JSON = '{"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "tasker-scheduler"}, "spec": {"template": {"metadata": {"name": "tasker-scheduler"}, "spec": {"containers": [{"command": ["sh", "-c", "echo \'scheduler\' && sleep 30"], "image": "busybox", "name": "tasker-scheduler"}], "restartPolicy": "Never"}}}}'


def test_get_available_api_methods() -> None:
    api_methods = utils_api.get_available_api_methods(client.BatchV1Api, "job")
    assert api_methods == [
        "create_namespaced_job",
        # "delete_collection_namespaced_job",
        "delete_namespaced_job",
        "list_namespaced_job",
        "patch_namespaced_job",
        "read_namespaced_job",
        "replace_namespaced_job",
    ]


def test_is_namespaced() -> None:
    api_methods = ["create_namespaced_job", "delete_collection_namespaced_job", "..."]
    assert utils_api.is_namespaced(api_methods) is True
    api_methods = ["create_job", "delete_job", "..."]
    assert utils_api.is_namespaced(api_methods) is False
    api_methods = ["create_namespaced_job", "create_job"]
    assert utils_api.is_namespaced(api_methods) is True


def test_build_api_method_name() -> None:
    method = "create"
    namespaced = True
    kind = "job"
    assert (
        utils_api.build_api_method_name(method, namespaced, kind)
        == "create_namespaced_job"
    )
    namespaced = False
    assert utils_api.build_api_method_name(method, namespaced, kind) == "create_job"
    method = "list"
    namespaced = True
    assert (
        utils_api.build_api_method_name(method, namespaced, kind)
        == "list_namespaced_job"
    )
    namespaced = False
    assert utils_api.build_api_method_name(method, namespaced, kind) == "list_job"


def test_get_api_func_ending() -> None:
    kind = "CronJob"
    assert utils_api.get_api_func_ending(kind) == "cron_job"
