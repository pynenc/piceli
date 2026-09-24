from __future__ import annotations

from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.tree import Tree

from piceli.k8s.cli import common
from piceli.k8s.ops import loader
from piceli.k8s.ops import plan as deployment_plan
from piceli.k8s.ops.discovery import (
    ApiResource,
    DiscoveryCoverage,
    ResourceScope,
    ResourceType,
)

if TYPE_CHECKING:
    from piceli.k8s.cli.context import ContextObject


def plan(
    ctx: typer.Context,
    cluster_id: str = typer.Option(
        ...,
        "--cluster-id",
        help="Stable cluster identity to bind into this offline plan.",
    ),
    validate: bool = typer.Option(
        False,
        "--validate",
        "-v",
        help="Validate the deployment graph for cycles and errors before showing the plan.",
    ),
) -> None:
    """
    Deployment plan for the kubernetes object model.

    Note: The command options are shared among commands and should be specified at the root level.
    """
    console = Console()
    common.print_command_name(console, "Deployment Plan")
    ctx_obj: ContextObject = ctx.obj
    common.print_ctx_options(console, ctx_obj)

    k8s_objects = list(
        loader.load_all(
            module_name=ctx_obj.module_name,
            module_path=ctx_obj.module_path,
            folder_path=ctx_obj.folder_path,
            sub_elements=ctx_obj.sub_elements,
        )
    )
    try:
        composition = deployment_plan.DeploymentComposition(
            (deployment_plan.component_from_objects("model", k8s_objects),)
        )
        target = deployment_plan.PlanTarget(cluster_id, ctx_obj.namespace)
        resource_types = tuple(
            sorted(
                {
                    ResourceType(resource.ref.api_version, resource.ref.kind)
                    for component in composition.components
                    for resource in component.resources
                }
            )
        )
        api_resources = tuple(
            ApiResource(
                resource_type,
                next(
                    ResourceScope.CLUSTER
                    if resource.ref.namespace == ""
                    else ResourceScope.NAMESPACED
                    for component in composition.components
                    for resource in component.resources
                    if resource.ref.api_version == resource_type.api_version
                    and resource.ref.kind == resource_type.kind
                ),
                resource_type.kind.lower() + "s",
            )
            for resource_type in resource_types
        )
        coverage = DiscoveryCoverage(
            "piceli-cli-explicit-empty",
            "offline-preview",
            "no-api-defaults",
            resource_types,
            resource_types,
            api_resources,
        )
        snapshot = deployment_plan.ObservedSnapshot(target, coverage)
        resolved_plan = deployment_plan.build_plan(
            composition,
            snapshot,
            deployment_plan.PlanAuthorization(target),
        )
    except ValueError as error:
        console.print(f"[bold red]Validation error: {error}[/]")
        return
    deployment_plan_tree = Tree(
        "[bold green]Kubernetes Deployment Plan", guide_style="bold bright_blue"
    )
    if validate:
        console.print("[bold blue]Validating deployment composition...[/]")
        console.print("[bold green]Validation successful![/]")

    console.print(f"Plan hash: [bold cyan]{resolved_plan.plan_hash}[/]")
    console.print(
        f"Target: [bold cyan]{resolved_plan.target.cluster_id}[/] / "
        f"[bold magenta]{resolved_plan.target.namespace}[/]"
    )

    actions = {action.resource.ref: action for action in resolved_plan.actions}
    for level_index, level in enumerate(resolved_plan.levels):
        level_tree = deployment_plan_tree.add(
            f"[bold yellow]Step {level_index + 1}:", guide_style="bold bright_yellow"
        )
        for resource in level:
            action = actions[resource]
            node_text = (
                f"[bold]{action.operation.value}[/] [dim]{resource.kind} "
                f"[bold cyan]{resource.name}[/] in namespace "
                f"[bold magenta]{resource.namespace or '<cluster>'}[/]"
            )
            level_tree.add(node_text)

    # Display the structured deployment plan
    console.print(deployment_plan_tree)
