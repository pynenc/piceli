from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table

from piceli.k8s.cli import common
from piceli.k8s.ops import loader

if TYPE_CHECKING:
    from piceli.k8s.cli.context import ContextObject

app = typer.Typer()


@app.command()
def list(ctx: typer.Context) -> None:
    """
    Lists Kubernetes objects based on the command options.

    Note: The command options are shared among commands and should be specified at the root level.
    The model listed in this command will be the same as those used in other commands, such as deploy.
    """
    ctx_obj: "ContextObject" = ctx.obj

    # Setting up Rich console and table
    console = Console()
    common.print_command_name(console, "List Kubernetes Objects Model")
    table = Table(show_header=True, header_style="bold magenta", expand=True)
    table.add_column("Name", style="dim")
    table.add_column("Kind", style="green")
    table.add_column("Namespace", style="yellow")
    table.add_column("Origin", style="cyan")

    common.print_ctx_options(console, ctx_obj)

    # Load and display Kubernetes objects
    for obj in loader.load_all(
        module_name=ctx_obj.module_name,
        module_path=ctx_obj.module_path,
        folder_path=ctx_obj.folder_path,
        sub_elements=ctx_obj.sub_elements,
    ):
        namespace = obj.namespace if obj.namespace else "Default"
        table.add_row(obj.name, obj.kind, namespace, str(obj.origin))

    # Print the table or a message if no objects are found
    if table.row_count == 0:
        console.print("[bold red]No Kubernetes objects found.[/]")
    else:
        console.print(table)


if __name__ == "__main__":
    app()
