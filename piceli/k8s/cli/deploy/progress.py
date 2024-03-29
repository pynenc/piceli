import json

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from piceli.k8s.ops.compare import object_comparer
from piceli.k8s.ops.deploy import deployment_progress


def print_execution_progress(
    console: Console, progress: deployment_progress.ExecutionProgress
) -> None:
    # Define a title for the Panel based on the event type
    title_text = f"Execution Status: {progress.status.name}"
    title = Text(title_text, style="bold yellow")
    style = "bold green"

    # Build the main message
    if progress.event == deployment_progress.ExecutionEvent.START_DEPLOY:
        message: str | Text = "Starting the deployment process."
    elif progress.event == deployment_progress.ExecutionEvent.SUCCESS:
        message = "Deployment finished successfully."
    elif progress.event == deployment_progress.ExecutionEvent.ERROR:
        message = Text(
            f"Error encountered: {progress.exception}.\n{progress.traceback}",
            style="bold red",
        )
        style = "bold red"
    elif progress.event == deployment_progress.ExecutionEvent.START_ROLLBACK:
        message = "Starting the rollback process."
        style = "bold yellow"
    elif progress.event == deployment_progress.ExecutionEvent.ROLLED_BACK:
        message = "Rollback completed, cluster returned to initial status."
    console.print(Panel(message, title=title, style=style))


def print_graph_level_apply(
    console: Console, progress: deployment_progress.GraphLevelProgress
) -> None:
    table = Table(title=f"Applying Level {progress.level_id}", box=box.HORIZONTALS)
    table.add_column("Name", style="cyan")
    table.add_column("Kind", style="magenta")
    table.add_column("Group", style="green")
    table.add_column("Version", style="yellow")

    for node in progress.nodes:
        k8s_obj = node.deploying_object.k8s_object
        table.add_row(k8s_obj.name, k8s_obj.kind, k8s_obj.group, k8s_obj.version)

    console.print(table)


def print_graph_level_rollback(
    console: Console, progress: deployment_progress.GraphLevelProgress
) -> None:
    table = Table(title=f"Rollback Level {progress.level_id}", box=box.HORIZONTALS)
    table.add_column("Name", style="cyan")
    table.add_column("Kind", style="magenta")
    table.add_column("Status", style="red")

    for node in progress.nodes:
        k8s_obj = node.deploying_object.k8s_object
        prev_status = "Reverting to previous" if node.previous_object else "N/A"
        table.add_row(k8s_obj.name, k8s_obj.kind, prev_status)

    console.print(table)


def print_graph_level_success(
    console: Console, progress: deployment_progress.GraphLevelProgress
) -> None:
    table = Table(title=f"Completed Level {progress.level_id}", box=box.HORIZONTALS)
    table.add_column("Name", style="cyan")
    table.add_column("Kind", style="magenta")
    table.add_column("Status", style="green")

    for node in progress.nodes:
        k8s_obj = node.deploying_object.k8s_object
        table.add_row(k8s_obj.name, k8s_obj.kind, "Completed")

    console.print(table)


def print_level_start(console: Console, level_id: int) -> None:
    # Creates a distinct section header for the start of a level
    console.print(
        Rule(
            f"[bold cyan] Starting Level {level_id}", align="center", style="bold blue"
        )
    )


def print_level_end(console: Console, level_id: int, success: bool) -> None:
    # Concludes a section with a clear ending, indicating success or failure
    end_message = (
        "Level Completed Successfully" if success else "Level Ended with Errors"
    )
    end_style = "bold green" if success else "bold red"
    console.print(Rule(f"[{end_style}] {end_message}", align="center", style=end_style))


def print_graph_level_progress(
    console: Console, progress: deployment_progress.GraphLevelProgress
) -> None:
    if progress.event == deployment_progress.GraphLevelEvent.START_APPLY:
        print_level_start(console, progress.level_id)
        print_graph_level_apply(console, progress)
    elif progress.event == deployment_progress.GraphLevelEvent.START_ROLLBACK:
        print_level_end(console, progress.level_id, False)
        print_level_start(console, progress.level_id)
        print_graph_level_rollback(console, progress)
    elif progress.event == deployment_progress.GraphLevelEvent.SUCCESS:
        print_graph_level_success(console, progress)
        print_level_end(console, progress.level_id, True)


def print_node_compare(
    console: Console, compare_result: object_comparer.CompareResult
) -> None:
    """Prints detailed information about a CompareResult object."""
    if compare_result.no_action_needed:
        console.print(
            Text(
                "Existing object matches the desired spec; no action needed.",
                style="green",
            )
        )
        return
    if compare_result.needs_replacement:
        console.print(
            Text("Differences detected: Requires replacement.", style="bold red")
        )
    elif compare_result.needs_patch:
        console.print(
            Text("Differences detected: Can be patched.", style="bold yellow")
        )
    table = Table(show_header=True, show_lines=True, header_style="bold magenta")
    table.add_column("Path", style="dim", no_wrap=False)
    table.add_column("Existing", style="red")
    table.add_column("Desired", style="green")
    for difference in compare_result.differences.considered:
        existing_str = json.dumps(difference.existing, sort_keys=True, indent=2)
        desired_str = json.dumps(difference.desired, sort_keys=True, indent=2)
        table.add_row(str(difference.path), existing_str, desired_str)
    console.print(table)


def print_node_progress(
    console: Console, progress: deployment_progress.NodeProgress
) -> None:
    """Prints detailed node execution progress using Rich formatting."""
    k8s_object = progress.node.deploying_object.k8s_object

    # Basic node info template
    obj_info = f"[bold]{k8s_object.kind} {k8s_object.name}[/]"

    # Handle different NodeEvents
    if progress.event == deployment_progress.NodeEvent.START_APPLY:
        console.print(f"{obj_info} - Applying object")
    elif progress.event == deployment_progress.NodeEvent.NEW_OBJ:
        console.print(f"{obj_info} - [bold green]New object, will be created.[/]")
    elif progress.event == deployment_progress.NodeEvent.ERROR:
        error_message = f"{obj_info} - [bold red]Error:[/] {progress.exception}"
        console.print(error_message)
        console.print(f"[red]{progress.traceback}[/]")
    elif progress.event == deployment_progress.NodeEvent.START_ROLLBACK:
        console.print(
            f"{obj_info} - [bold yellow]Rolling back...[/] Deployment status: {progress.deployment_status}"
        )
    elif progress.event == deployment_progress.NodeEvent.COMPLETE:
        console.print(f"{obj_info} - [bold green]Application completed.[/]")
    elif progress.event == deployment_progress.NodeEvent.COMPARE:
        console.print(f"{obj_info} - Comparing existing object...")
        if progress.compare_result:
            print_node_compare(console, progress.compare_result)


def print_progress(console: Console, progress: deployment_progress.Progress) -> None:
    """Prints the execution progress using Rich formatting."""
    if isinstance(progress, deployment_progress.ExecutionProgress):
        print_execution_progress(console, progress)
    elif isinstance(progress, deployment_progress.GraphLevelProgress):
        print_graph_level_progress(console, progress)
    elif isinstance(progress, deployment_progress.NodeProgress):
        print_node_progress(console, progress)
