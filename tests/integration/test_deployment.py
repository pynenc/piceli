import time
from typing import TYPE_CHECKING, Generator

import pytest
from kubernetes.client.exceptions import ApiException

from piceli.k8s.exceptions import api_exceptions
from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.k8s_objects.base import K8sObject, K8sObjectIdentifier
from piceli.k8s.ops import fetcher
from piceli.k8s.ops.compare import compare_op, path
from piceli.k8s.ops.deploy import deploy_op

# from kubernetes.utils.quantity import parse_quantity


if TYPE_CHECKING:
    from _pytest.fixtures import FixtureRequest


@pytest.fixture
def ctx() -> ClientContext:
    return ClientContext()


@pytest.fixture
def test_namespace(
    ctx: ClientContext, request: "FixtureRequest"
) -> Generator[str, None, None]:
    namespace_name = request.node.name.replace("_", "-")
    body = {"metadata": {"name": namespace_name}}
    try:
        ctx.core_api.read_namespace(name=namespace_name)
        ctx.core_api.delete_namespace(name=namespace_name)
        while True:
            ctx.core_api.read_namespace(name=namespace_name)
            print(f"Waiting for namespace {namespace_name} to be deleted")
            time.sleep(1)
    except ApiException as ex:
        api_op_ex = api_exceptions.ApiOperationException.from_api_exception(ex)
        if not api_op_ex.not_found:
            raise api_op_ex from ex
    finally:
        ctx.core_api.create_namespace(body=body)
        yield namespace_name
    try:
        ctx.core_api.delete_namespace(name=namespace_name, body={})
    except ApiException as e:
        print(f"Failed to delete namespace {namespace_name}: {str(e)}")


def test_deployment_and_update(
    test_namespace: str,
    ctx: ClientContext,
    resources: list[K8sObject],
    resources_update: list[K8sObject],
) -> None:
    ctx = ClientContext()
    ignore_kinds = ["Event", "Endpoints", "Pod", "ReplicaSet", "Job"]
    # get all objects of the initial namespace
    existing = fetcher.get_all_from_context(
        ctx, test_namespace, ignore_kinds=ignore_kinds
    )
    # deploy the resources to the namespace
    executor = deploy_op.deploy(ctx, resources, test_namespace)
    assert executor.is_done
    # get all objects of the namespace after deployment
    after_deployment = fetcher.get_all_from_context(
        ctx, test_namespace, ignore_kinds=ignore_kinds
    )
    # compare the objects before and after deployment
    initial_diffs = compare_op.compare_object_sets(existing, after_deployment)
    assert initial_diffs[test_namespace].removed == []
    assert initial_diffs[test_namespace].modified == {}
    # We only expect to find differences on the objects added on:
    # except on the storage class, which is a cluster-wide resource
    # the storage class will be added only on new minikube instances, not delete on every tests
    # so we can reuse if running tests in different namespaces in parallel
    assert len(initial_diffs[test_namespace].added) >= len(resources) - 1
    added_ids = {obj.unnamespaced_id for obj in initial_diffs[test_namespace].added}
    expected_ids = {
        obj.unnamespaced_id for obj in resources if obj.kind not in ["StorageClass"]
    }
    assert added_ids == expected_ids

    # update the resources in the namespace
    executor = deploy_op.deploy(ctx, resources_update, test_namespace)
    assert executor.is_done
    # get all objects of the namespace after update
    final_objects = fetcher.get_all_from_context(
        ctx, test_namespace, ignore_kinds=ignore_kinds
    )
    # compare the objects before and after update
    update_diffs = compare_op.compare_object_sets(after_deployment, final_objects)
    assert update_diffs[test_namespace].removed == []
    assert update_diffs[test_namespace].added == []
    # We only expect to find differences on the objects modified on:
    # - tests/integration/resources/deployment_update.yml
    # over original deployment
    # - tests/integration/resources/deployment.yml

    assert (
        len(update_diffs[test_namespace].modified) == 2
    )  # disabled PVC for ubuntu tests

    # check that one of the differences is a cronjob
    # the only required change is the cronjob schedule
    cronjob_diff = update_diffs[test_namespace].modified[
        K8sObjectIdentifier(name="example-cronjob", kind="CronJob", namespace=None)
    ]
    assert len(cronjob_diff.compare_result.differences.considered) == 1
    cronjob_considered_path_diff = cronjob_diff.compare_result.differences.considered[0]
    assert cronjob_considered_path_diff.path == path.Path.from_string("spec,schedule")
    assert cronjob_considered_path_diff.desired == "*/10 * * * *"
    assert cronjob_considered_path_diff.existing == "*/5 * * * *"
    # patch document should only contain the schedule change
    assert cronjob_diff.compare_result.patch_document() == {
        "spec": {"schedule": "*/10 * * * *"}
    }

    # This test doesn't work in github ci/cd, disabled for now
    # # check that one of the differences is a PVC
    # # the only required change is doubling the storage size from 0.1Gi to 0.2Gi
    # pvc_diff = update_diffs[test_namespace].modified[
    #     K8sObjectIdentifier(
    #         name="example-persistentvolumeclaim",
    #         kind="PersistentVolumeClaim",
    #         namespace=None,
    #     )
    # ]
    # assert len(pvc_diff.compare_result.differences.considered) == 1
    # pvc_considered_path_diff = pvc_diff.compare_result.differences.considered[0]
    # assert pvc_considered_path_diff.path == path.Path.from_string(
    #     "spec,resources,requests,storage"
    # )
    # desired_quantity = parse_quantity(pvc_considered_path_diff.desired)
    # existing_quantity = parse_quantity(pvc_considered_path_diff.existing)
    # assert desired_quantity == existing_quantity * 2
    # # patch document should only contain the storage size change
    # assert pvc_diff.compare_result.patch_document() == {
    #     "spec": {"resources": {"requests": {"storage": "214748364800m"}}}
    # }

    # check that one of them is a deployment
    # the only required change is the image
    deployment_diff = update_diffs[test_namespace].modified[
        K8sObjectIdentifier(
            name="example-deployment", kind="Deployment", namespace=None
        )
    ]
    assert len(deployment_diff.compare_result.differences.considered) == 1
    deployment_considered_path_diff = (
        deployment_diff.compare_result.differences.considered[0]
    )
    assert deployment_considered_path_diff.path == path.Path.from_string(
        "spec,template,spec,containers,name:example-container,image"
    )
    assert deployment_considered_path_diff.existing == "bash:4.4"
    assert deployment_considered_path_diff.desired == "ubuntu:latest"
    # patch document should only contain the image change
    assert deployment_diff.compare_result.patch_document() == {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "example-container", "image": "ubuntu:latest"}
                    ]
                }
            }
        }
    }
