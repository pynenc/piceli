from typing import Generator
from unittest.mock import MagicMock, patch

import pytest

from piceli.k8s.cli.deploy import detail
from piceli.k8s.ops.compare import object_comparer


@pytest.fixture
def mock_console() -> Generator[MagicMock, None, None]:
    with patch("rich.console.Console") as mock:
        yield mock()


def test_print_new_objects(mock_console: MagicMock) -> None:
    new_objects = [MagicMock(), MagicMock()]
    new_objects[0].k8s_object.kind = "Deployment"
    new_objects[0].k8s_object.name = "my-deployment"
    new_objects[0].k8s_object.version = "v1"
    new_objects[0].k8s_object.group = "apps"

    new_objects[1].k8s_object.kind = "Service"
    new_objects[1].k8s_object.name = "my-service"
    new_objects[1].k8s_object.version = "v1"
    new_objects[1].k8s_object.group = ""

    # Test with no new objects
    detail.print_new_objects(mock_console, [])
    # Check that print was called with a specific message
    mock_console.print.assert_called_once_with(
        "No new Kubernetes objects to be created.", style="yellow"
    )
    mock_console.reset_mock()

    # Test with new objects
    detail.print_new_objects(mock_console, new_objects)  # type: ignore
    # Verify that print was called once, and check the argument type
    assert mock_console.print.call_count == 1
    args, kwargs = mock_console.print.call_args
    # Ensure the first argument (the only one in this case) is a Table instance
    assert isinstance(args[0], detail.Table)


@pytest.fixture
def compare_results_with_changes() -> list[detail.ObjCompareResult]:
    desired_obj_1 = MagicMock()
    desired_obj_1.k8s_object.kind = "Deployment"
    desired_obj_1.k8s_object.name = "my-deployment"
    compare_result_1 = MagicMock()
    compare_result_1.action_description = "Requires replacement"

    desired_obj_2 = MagicMock()
    desired_obj_2.k8s_object.kind = "Service"
    desired_obj_2.k8s_object.name = "my-service"
    compare_result_2 = MagicMock()
    compare_result_2.action_description = "Can be patched"

    return [
        detail.ObjCompareResult(
            desired_obj=desired_obj_1, compared_result=compare_result_1
        ),
        detail.ObjCompareResult(
            desired_obj=desired_obj_2, compared_result=compare_result_2
        ),
    ]


def test_print_summary_of_changes_with_changes(
    mock_console: MagicMock, compare_results_with_changes: list[detail.ObjCompareResult]
) -> None:
    detail.print_summary_of_changes(mock_console, compare_results_with_changes)
    # Check if console.print was called once and the first argument is an instance of Table
    mock_console.print.assert_called_once()
    args, _ = mock_console.print.call_args
    assert isinstance(args[0], detail.Table)


def test_print_summary_of_changes_without_changes(mock_console: MagicMock) -> None:
    detail.print_summary_of_changes(mock_console, [])
    mock_console.print.assert_called_once_with(
        "No changes required in any Kubernetes object.", style="yellow"
    )


@pytest.fixture
def obj_compare_result_with_specs() -> detail.ObjCompareResult:
    desired_obj = MagicMock()
    compare_result = MagicMock()
    compare_result.existing_spec = {
        "metadata": {"name": "existing-pod", "labels": {"app": "test"}}
    }
    compare_result.desired_spec = {
        "metadata": {"name": "desired-pod", "labels": {"app": "test-new"}}
    }
    return detail.ObjCompareResult(
        desired_obj=desired_obj, compared_result=compare_result
    )


def test_print_compared_specs(
    mock_console: MagicMock, obj_compare_result_with_specs: detail.ObjCompareResult
) -> None:
    detail.print_compared_specs(mock_console, obj_compare_result_with_specs)
    # Ensure console.print was called once and with a Table as argument
    mock_console.print.assert_called_once()
    args, _ = mock_console.print.call_args
    assert isinstance(args[0], detail.Table)

    # Further verify the contents of the table if necessary
    printed_table: detail.Table = args[0]
    expected_existing_json = detail.json.dumps(
        obj_compare_result_with_specs.compared_result.existing_spec,
        sort_keys=True,
        indent=2,
    )
    expected_desired_json = detail.json.dumps(
        obj_compare_result_with_specs.compared_result.desired_spec,
        sort_keys=True,
        indent=2,
    )
    assert list(printed_table.columns[0].cells)[0] == expected_existing_json
    assert list(printed_table.columns[1].cells)[0] == expected_desired_json


@pytest.fixture
def obj_compare_result_with_differences() -> detail.ObjCompareResult:
    path1 = object_comparer.Path.from_string("metadata,name")
    path2 = object_comparer.Path.from_string(
        "spec,template,spec,containers,name:example-container,image"
    )

    differences = object_comparer.Differences(
        considered=[
            object_comparer.PathComparison(path1, "existing-name", "desired-name")
        ],
        ignored=[object_comparer.PathComparison(path2, "nginx:latest", "nginx:stable")],
        defaults=[object_comparer.PathComparison(path1, None, "default-name")],
    )

    compare_result = object_comparer.CompareResult(
        desired_spec={},
        existing_spec={},
        update_action=object_comparer.UpdateAction.NEEDS_PATCH,
        differences=differences,
    )

    desired_obj = MagicMock(spec=detail.ObjectManager)
    desired_obj.k8s_object = MagicMock()

    return detail.ObjCompareResult(
        desired_obj=desired_obj, compared_result=compare_result
    )


def test_print_differences(
    mock_console: MagicMock,
    obj_compare_result_with_differences: detail.ObjCompareResult,
) -> None:
    detail.print_differences(mock_console, obj_compare_result_with_differences)

    # Verify console.print was called once
    mock_console.print.assert_called_once()

    # Extract the printed table
    args, kwargs = mock_console.print.call_args
    printed_table = args[0]

    assert isinstance(printed_table, detail.Table), "Expected a Table instance."

    # Verify the table contents
    assert len(printed_table.rows) == 3, "Expected 3 rows of differences."
    # You can further assert on the contents of each row to ensure correctness
    for cell, diff_type in zip(
        list(printed_table.columns[1].cells), ["Considered", "Ignored", "Defaults"]
    ):
        assert diff_type in cell, f"Expected difference type '{diff_type}' not found."


@pytest.fixture
def obj_compare_result_no_action_needed(
    obj_compare_result_with_specs: detail.ObjCompareResult,
) -> detail.ObjCompareResult:
    # Modify the fixture to simulate no action needed.
    obj_compare_result_with_specs.compared_result.update_action = (
        object_comparer.UpdateAction.EQUALS
    )
    return obj_compare_result_with_specs


def test_print_compare_results_action_needed(
    mock_console: MagicMock,
    obj_compare_result_with_differences: detail.ObjCompareResult,
) -> None:
    with patch("piceli.k8s.cli.deploy.detail.print_summary_of_changes"), patch(
        "piceli.k8s.cli.deploy.detail.print_compared_specs"
    ), patch("piceli.k8s.cli.deploy.detail.print_differences"):
        detail.print_compare_results(
            mock_console,
            [obj_compare_result_with_differences],
            hide_no_action_detail=False,
        )
        # Ensure the detailed comparison is printed for objects requiring action
        mock_console.print.assert_called()
        assert any(
            isinstance(arg[0][0], detail.Rule)
            for arg in mock_console.print.call_args_list
        ), "Expected a title Rule for each Kubernetes object."


def test_print_compare_results_no_action_hidden(
    mock_console: MagicMock,
    obj_compare_result_no_action_needed: detail.ObjCompareResult,
) -> None:
    with patch("piceli.k8s.cli.deploy.detail.print_summary_of_changes"), patch(
        "piceli.k8s.cli.deploy.detail.print_compared_specs"
    ), patch("piceli.k8s.cli.deploy.detail.print_differences"):
        detail.print_compare_results(
            mock_console,
            [obj_compare_result_no_action_needed],
            hide_no_action_detail=True,
        )
        # Ensure the detailed comparison is NOT printed for objects where no action is needed and details are hidden
        assert not any(
            isinstance(arg[0][0], detail.Rule)
            for arg in mock_console.print.call_args_list
        ), "Expected NO detailed comparison for objects with no action needed."


def test_print_compare_results_no_action_shown(
    mock_console: MagicMock,
    obj_compare_result_no_action_needed: detail.ObjCompareResult,
) -> None:
    with patch("piceli.k8s.cli.deploy.detail.print_summary_of_changes"), patch(
        "piceli.k8s.cli.deploy.detail.print_compared_specs"
    ), patch("piceli.k8s.cli.deploy.detail.print_differences"):
        detail.print_compare_results(
            mock_console,
            [obj_compare_result_no_action_needed],
            hide_no_action_detail=False,
        )
        # Ensure the detailed comparison is printed even for objects where no action is needed if details are not hidden
        mock_console.print.assert_called()
        assert any(
            isinstance(arg[0][0], detail.Rule)
            for arg in mock_console.print.call_args_list
        ), "Expected a title Rule for each Kubernetes object, even with no action needed."
