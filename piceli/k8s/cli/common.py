from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

if TYPE_CHECKING:
    from piceli.k8s.cli.context import ContextObject


def print_command_name(console: Console, command_name: str) -> None:
    running_command_msg = (
        f"[bold blue]Running command:[/] [bold yellow]{command_name}[/]"
    )
    console.print(
        Panel(
            running_command_msg,
            title="[bold green]Command Execution[/]",
            border_style="green",
        )
    )


def print_ctx_options(console: Console, ctx_obj: "ContextObject") -> None:
    # Prepare the description of the task with different styles
    details = Text()
    details.append("Namespace: ", style="bold")
    details.append(ctx_obj.namespace, style="normal")
    details.append("\nModule Name: ", style="bold")
    details.append(
        ctx_obj.module_name if ctx_obj.module_name else "Not specified",
        style="italic yellow" if not ctx_obj.module_name else "normal",
    )
    details.append("\nModule Path: ", style="bold")
    details.append(
        ctx_obj.module_path if ctx_obj.module_path else "Not specified",
        style="italic yellow" if not ctx_obj.module_path else "normal",
    )
    details.append("\nFolder Path: ", style="bold")
    details.append(
        ctx_obj.folder_path if ctx_obj.folder_path else "Not specified",
        style="italic yellow" if not ctx_obj.folder_path else "normal",
    )
    details.append("\nInclude Sub-elements: ", style="bold")
    details.append(str(ctx_obj.sub_elements), style="normal")

    # Display the task description in a panel with a clear title
    console.print(Panel(details, title="[bold]Context Options", expand=False))
