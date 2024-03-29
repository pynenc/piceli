from unittest import mock

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app
from piceli.k8s.cli.deploy import detail
from piceli.k8s.k8s_objects.base import K8sObject, OriginYAML
from piceli.k8s.object_manager.factory import ObjectManager
from piceli.k8s.ops.compare import object_comparer

runner = CliRunner()


@pytest.fixture
def k8s_object() -> K8sObject:
    return K8sObject(
        spec={"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "test-pod"}},
        origin=OriginYAML("path/to/pod.yaml"),
    )


@pytest.fixture
def obj_compare_result(k8s_object: K8sObject) -> detail.ObjCompareResult:
    desired_obj = mock.Mock(spec=ObjectManager)
    desired_obj.k8s_object = k8s_object
    compare_result = object_comparer.CompareResult(
        desired_spec={"metadata": {"name": "test-pod"}},
        existing_spec={},
        update_action=object_comparer.UpdateAction.NEEDS_REPLACEMENT,
        differences=object_comparer.Differences(
            considered=[
                object_comparer.PathComparison(
                    object_comparer.Path.from_string("metadata,name"), "test-pod", None
                )
            ],
            ignored=[],
            defaults=[],
        ),
    )
    return detail.ObjCompareResult(
        desired_obj=desired_obj, compared_result=compare_result
    )


def test_detail_with_changes(
    k8s_object: K8sObject, obj_compare_result: detail.ObjCompareResult
) -> None:
    with mock.patch(
        "piceli.k8s.ops.loader.load_all", return_value=[k8s_object]
    ), mock.patch(
        "piceli.k8s.object_manager.factory.ManagerFactory.get_manager"
    ) as mock_get_manager, mock.patch(
        "piceli.k8s.ops.compare.object_comparer.determine_update_action",
        side_effect=lambda x, y: obj_compare_result.compared_result,
    ):
        mock_get_manager.return_value = obj_compare_result.desired_obj

        result = runner.invoke(app, ["deploy", "detail"])

        assert result.exit_code == 0
        assert "Deployment Detailed Analysis" in result.stdout
        assert k8s_object.kind in result.stdout
        assert k8s_object.name in result.stdout
        assert "Requires replacement" in result.stdout


def test_detail_with_no_changes(k8s_object: K8sObject) -> None:
    compare_result_no_changes = object_comparer.CompareResult(
        desired_spec={"metadata": {"name": "test-pod"}},
        existing_spec={"metadata": {"name": "test-pod"}},
        update_action=object_comparer.UpdateAction.EQUALS,
        differences=object_comparer.Differences(),
    )

    obj_compare_result_no_changes = detail.ObjCompareResult(
        desired_obj=mock.Mock(spec=ObjectManager, k8s_object=k8s_object),
        compared_result=compare_result_no_changes,
    )

    with mock.patch(
        "piceli.k8s.ops.loader.load_all", return_value=[k8s_object]
    ), mock.patch(
        "piceli.k8s.object_manager.factory.ManagerFactory.get_manager",
        return_value=obj_compare_result_no_changes.desired_obj,
    ), mock.patch(
        "piceli.k8s.ops.compare.object_comparer.determine_update_action",
        return_value=compare_result_no_changes,
    ):
        # check no changes: shows no action needed and differences
        result = runner.invoke(app, ["deploy", "detail"])
        assert result.exit_code == 0
        assert "No action needed" in result.stdout
        assert "Differences Summary" in result.stdout
        # check flag to hide summary on no action needed
        result = runner.invoke(app, ["deploy", "detail", "-hna"])
        assert result.exit_code == 0
        assert "No action needed" in result.stdout
        assert "Differences Summary" not in result.stdout


def test_detail_with_error(k8s_object: K8sObject) -> None:
    with mock.patch(
        "piceli.k8s.ops.loader.load_all", return_value=[k8s_object]
    ), mock.patch(
        "piceli.k8s.object_manager.factory.ManagerFactory.get_manager"
    ) as mock_get_manager:
        mock_get_manager.side_effect = Exception("cannot get manager")

        result = runner.invoke(app, ["deploy", "detail"])

        assert result.exit_code == 1
        assert "cannot get manager" in str(result.exception)


def test_detail_no_kubernetes_objects_found() -> None:
    with mock.patch("piceli.k8s.ops.loader.load_all", return_value=[]):
        result = runner.invoke(app, ["deploy", "detail"])

        assert result.exit_code == 0
        assert "No new Kubernetes objects to be created." in result.stdout
        assert "No changes required in any Kubernetes object." in result.stdout
