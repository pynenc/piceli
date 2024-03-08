import asyncio

from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.k8s_objects.base import K8sObject
from piceli.k8s.ops.deploy import strategy_auto, deployment_executor


def deploy(
    ctx: ClientContext, k8s_objects: list[K8sObject], namespace: str | None
) -> deployment_executor.DeploymentExecutor:
    """Builds the deployment graph and deploy the objects to the cluster."""
    # For now only stragey auto is definedkob
    # TODO: add user defined strategies, settings, etc...
    # TODO: Find out of model objects
    strategy = strategy_auto.StrategyAuto()
    deployment_graph = strategy.build_deployment_graph(k8s_objects)
    deployment_graph.validate()
    executor = deployment_executor.DeploymentExecutor(deployment_graph)
    try:
        asyncio.run(executor.deploy(ctx, namespace))
        asyncio.run(executor.wait_for_all(ctx, namespace))
    except Exception as e:
        print(f"Deployment failed: {e}")
        # Deployment failure and rollback logic is handled within executor.deploy()
    return executor
