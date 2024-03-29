import json
from typing import TYPE_CHECKING, Annotated, NamedTuple

import typer
from rich.console import Console
from rich.rule import Rule
from rich.table import Table

from piceli.k8s.cli import common
from piceli.k8s.exceptions import api_exceptions
from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.object_manager.factory import ManagerFactory, ObjectManager
from piceli.k8s.ops import loader
from piceli.k8s.ops.compare import object_comparer

if TYPE_CHECKING:
    from piceli.k8s.cli.context import ContextObject


def print_new_objects(console: Console, new_objects: list[ObjectManager]) -> None:
    """Prints a table listing all new Kubernetes objects that will be created."""
    if new_objects:
        table = Table(
            title="New Kubernetes Objects",
            show_header=True,
            header_style="bold magenta",
        )
        table.add_column("Kind", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Version", style="yellow")
        table.add_column("Group", style="red")

        for obj in new_objects:
            k8s_obj = obj.k8s_object
            table.add_row(k8s_obj.kind, k8s_obj.name, k8s_obj.version, k8s_obj.group)

        console.print(table)
    else:
        console.print("No new Kubernetes objects to be created.", style="yellow")


class ObjCompareResult(NamedTuple):
    desired_obj: ObjectManager
    compared_result: object_comparer.CompareResult


def print_summary_of_changes(
    console: Console, compare_results: list[ObjCompareResult]
) -> None:
    """Prints a summary table of changes required for each Kubernetes object."""
    if not compare_results:
        console.print("No changes required in any Kubernetes object.", style="yellow")
        return
    # Create a summary table
    table = Table(
        title="Kubernetes Objects Deployment Summary",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Kind", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Update Action", style="yellow")

    # Populate the table
    for obj_compare_result in compare_results:
        desired_obj = obj_compare_result.desired_obj.k8s_object
        result = obj_compare_result.compared_result
        table.add_row(desired_obj.kind, desired_obj.name, result.action_description)

    # Print the summary table
    console.print(table)


def print_compared_specs(
    console: Console, obj_compare_result: ObjCompareResult
) -> None:
    """Prints a detailed comparison for a single Kubernetes object."""
    result = obj_compare_result.compared_result

    # Prepare JSON representations of existing and desired specs
    existing_json = json.dumps(result.existing_spec, sort_keys=True, indent=2)
    desired_json = json.dumps(result.desired_spec, sort_keys=True, indent=2)

    # Create and print the comparison table
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Existing Object", style="red")
    table.add_column("Desired Object", style="green")
    table.add_row(existing_json, desired_json)
    console.print(table)


def print_differences(console: Console, obj_compare_result: ObjCompareResult) -> None:
    """Prints a table detailing the differences for a Kubernetes object."""
    differences = obj_compare_result.compared_result.differences

    # Create a table for the differences with line separators between rows
    table = Table(
        title="Differences Summary",
        show_header=True,
        header_style="bold magenta",
        show_lines=True,
    )
    table.add_column("Path", style="cyan", no_wrap=True)
    table.add_column("Type", style="yellow")
    table.add_column("Existing", style="red")
    table.add_column("Desired", style="green")

    # Helper function to add differences to the table, with color coding for type
    def add_differences_to_table(
        diff_type: str, differences: list[object_comparer.PathComparison], color: str
    ) -> None:
        for diff in differences:
            # Convert complex structures to JSON strings for better readability
            existing = (
                json.dumps(diff.existing, sort_keys=True, indent=2)
                if isinstance(diff.existing, (dict, list))
                else str(diff.existing)
            )
            desired = (
                json.dumps(diff.desired, sort_keys=True, indent=2)
                if isinstance(diff.desired, (dict, list))
                else str(diff.desired)
            )
            table.add_row(
                str(diff.path), f"[{color}]{diff_type}[/{color}]", existing, desired
            )

    # Add each type of difference to the table, with specific colors
    add_differences_to_table("Considered", differences.considered, "green")
    add_differences_to_table("Ignored", differences.ignored, "yellow")
    add_differences_to_table("Defaults", differences.defaults, "blue")

    # Print the table
    console.print(table)


def print_compare_results(
    console: Console,
    obj_compare_results: list[ObjCompareResult],
    hide_no_action_detail: bool,
) -> None:
    """Prints detailed comparison results for each Kubernetes object."""
    print_summary_of_changes(console, obj_compare_results)
    for obj_compare_result in obj_compare_results:
        compare_result = obj_compare_result.compared_result
        if hide_no_action_detail and compare_result.no_action_needed:
            continue
        k8s_object = obj_compare_result.desired_obj.k8s_object
        title = (
            f"{k8s_object.kind} {k8s_object.name} - {compare_result.action_description}"
        )
        console.print(Rule(f"[bold cyan] {title}", align="center", style="bold blue"))
        print_compared_specs(console, obj_compare_result)
        print_differences(console, obj_compare_result)


def detail(
    ctx: typer.Context,
    hide_no_action_detail: Annotated[
        bool,
        typer.Option(
            ...,
            "--hide-no-action",
            "-hna",
            help="Hide the comparison details when no action is needed.",
            is_flag=True,
            show_default=True,
        ),
    ] = False,
) -> None:
    """
    Analize the required changes to deploy the specified kubernetes object model

    Note: The command options are shared among commands and should be specified at the root level.
    """
    console = Console()
    common.print_command_name(console, "Deployment Detailed Analysis")
    ctx_obj: "ContextObject" = ctx.obj
    common.print_ctx_options(console, ctx_obj)

    k8s_objects = loader.load_all(
        module_name=ctx_obj.module_name,
        module_path=ctx_obj.module_path,
        folder_path=ctx_obj.folder_path,
        sub_elements=ctx_obj.sub_elements,
    )
    client_ctx = ClientContext()
    compare_results, new_objects = [], []
    for k8s_obj in k8s_objects:
        desired_obj = ManagerFactory.get_manager(k8s_obj)
        try:
            existing_obj = desired_obj.read(client_ctx, ctx_obj.namespace)
            compare_result = object_comparer.determine_update_action(
                desired_obj.k8s_object, existing_obj
            )
            compare_results.append(ObjCompareResult(desired_obj, compare_result))
        except api_exceptions.ApiOperationException as ex:
            if not ex.not_found:
                raise
            new_objects.append(desired_obj)

    print_new_objects(console, new_objects)
    print_compare_results(console, compare_results, hide_no_action_detail)
