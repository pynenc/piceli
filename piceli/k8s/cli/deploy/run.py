import asyncio
from typing import TYPE_CHECKING, Annotated

import typer
from kubernetes.client.exceptions import ApiException
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress

from piceli.k8s.cli import common
from piceli.k8s.cli.deploy import progress
from piceli.k8s.exceptions import api_exceptions
from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.ops import loader
from piceli.k8s.ops.deploy import deployment_executor, strategy_auto

if TYPE_CHECKING:
    from piceli.k8s.cli.context import ContextObject


def run(
    ctx: typer.Context,
    create_namespace: Annotated[
        bool,
        typer.Option(
            ...,
            "--create-namespace",
            "-c",
            help="Create the namespace if it does not exist.",
            is_flag=True,
            show_default=True,
        ),
    ] = True,
) -> None:
    """Deploy Kubernetes Object Model to the current cluster."""
    console = Console()
    common.print_command_name(console, "Running Deployment")
    ctx_obj: "ContextObject" = ctx.obj
    strategy = strategy_auto.StrategyAuto()
    k8s_objects = loader.load_all(
        module_name=ctx_obj.module_name,
        module_path=ctx_obj.module_path,
        folder_path=ctx_obj.folder_path,
        sub_elements=ctx_obj.sub_elements,
    )
    deploy_graph = strategy.build_deployment_graph(k8s_objects)
    deploy_graph.validate()
    executor = deployment_executor.DeploymentExecutor(deploy_graph)
    if create_namespace:
        _upsert_namespace(console, ctx_obj.namespace)
    asyncio.run(run_deployment(console, executor, ctx_obj.namespace))


def _upsert_namespace(console: Console, namespace_name: str) -> None:
    body = {"metadata": {"name": namespace_name}}
    client_ctx = ClientContext()
    try:
        client_ctx.core_api.read_namespace(name=namespace_name)
        console.print(
            f"[yellow]Namespace '{namespace_name}' already exists. No action required.[/]"
        )
    except ApiException as ex:
        api_op_ex = api_exceptions.ApiOperationException.from_api_exception(ex)
        if not api_op_ex.not_found:
            raise api_op_ex from ex
        client_ctx.core_api.create_namespace(body=body)
        console.print(f"[green]Namespace '{namespace_name}' created successfully.[/]")


async def update_progress(
    console: Console,
    progress_bar: Progress,
    executor: deployment_executor.DeploymentExecutor,
) -> None:
    def update_bar() -> None:
        if executor.status in (
            deployment_executor.ExecutionStatus.DONE,
            deployment_executor.ExecutionStatus.PENDING,
        ):
            style = "green"
        style = "red"
        progress_bar.update(
            task_id, completed=len(executor.deployed_nodes), style=style
        )

    total_steps = len(list(executor.graph.traverse_graph()))
    task_id = progress_bar.add_task(
        "Deployment", filename="x.txt", total=total_steps, style="green"
    )
    while not executor.is_final:
        last_index = None
        update_bar()
        for index, progress_event in enumerate(executor.progress):
            if last_index and index <= last_index:
                continue
            progress.print_progress(console, progress_event)
            last_index = index
        update_bar()
        await asyncio.sleep(1)
    update_bar()


async def run_deployment(
    console: Console,
    executor: deployment_executor.DeploymentExecutor,
    namespace: str,
) -> None:
    """Run the deployment with live progress updates."""
    with Progress(
        # TextColumn("[bold blue]Deploying", justify="right"),
        # BarColumn(bar_width=None),
        # "[progress.percentage]{task.percentage:>3.1f}%",
        # "•",
        # TimeRemainingColumn(),
        console=console,
    ) as progress:
        try:
            client_ctx = ClientContext()
            await asyncio.gather(
                executor.deploy(client_ctx, namespace),
                executor.wait_for_all(client_ctx, namespace),
                update_progress(console, progress, executor),
            )
        except Exception as e:
            console.print(
                Panel(f"[bold red]Deployment failed:[/] {str(e)}", expand=False)
            )
            raise
        else:
            console.print(
                Panel("[bold green]Deployment completed successfully[/]", expand=False)
            )
