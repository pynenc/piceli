import asyncio
import logging
from enum import StrEnum, auto

from piceli.k8s.exceptions import api_exceptions
from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.object_manager.factory import ManagerFactory
from piceli.k8s.ops.compare import object_comparer
from piceli.k8s.ops.deploy import deployment_graph

logger = logging.getLogger(__name__)


class NoActionNeeded(Exception):
    pass


class ExecutionStatus(StrEnum):
    PENDING = auto()
    DONE = auto()
    FAILED = auto()
    ROLLED_BACK = auto()


class DeploymentExecutor:
    def __init__(self, graph: deployment_graph.DeploymentGraph):
        self.graph = graph
        self.deployed_nodes: list[list[deployment_graph.ObjectNode]] = []
        self.waited_nodes: set = set()
        self.status = ExecutionStatus.PENDING

    @property
    def is_done(self) -> bool:
        return self.status == ExecutionStatus.DONE

    async def wait_for_all(self, ctx: ClientContext, namespace: str | None) -> None:
        for level_nodes in self.deployed_nodes:
            await asyncio.gather(
                *(self._wait_for_node(node, ctx, namespace) for node in level_nodes)
            )

    async def deploy(self, ctx: ClientContext, namespace: str | None = None) -> None:
        try:
            for level_nodes in self.graph.traverse_graph():
                # Deploy all nodes in the current level in parallel
                await asyncio.gather(
                    *(self.apply_node(node, ctx, namespace) for node in level_nodes)
                )
                self.deployed_nodes.append(level_nodes)
            self.status = ExecutionStatus.DONE
        except Exception as ex:
            logger.error(f"Deployment failed: {ex}")
            self.status = ExecutionStatus.FAILED
            await self.rollback(ctx, namespace)
            raise

    async def _wait_for_node(
        self,
        node: deployment_graph.ObjectNode,
        ctx: ClientContext,
        namespace: str | None,
        on_rollback: bool = False,
    ) -> None:
        if on_rollback:
            if node.deployment_status in [
                deployment_graph.DeploymentStatus.NO_ACTION_NEEDED,
                deployment_graph.DeploymentStatus.PENDING,
            ]:
                return
            if node.previous_object is None:
                return
            object_manager = ManagerFactory.get_manager(node.previous_object)
        else:
            object_manager = node.deploying_object
        object_manager.wait(ctx, namespace)
        self.waited_nodes.add(node.identifier)

    async def _wait_for_dependencies(
        self,
        node: deployment_graph.ObjectNode,
        ctx: ClientContext,
        namespace: str | None,
        on_rollback: bool = False,
    ) -> None:
        logger.info(f"waiting for dependencies {node.identifier} {on_rollback=}")
        await asyncio.gather(
            *[
                self._wait_for_node(
                    self.graph.nodes[dep_id], ctx, namespace, on_rollback
                )
                for dep_id in node.dependencies
                if dep_id not in self.waited_nodes
            ]
        )

    async def apply_node(
        self,
        node: deployment_graph.ObjectNode,
        ctx: ClientContext,
        namespace: str | None,
        on_rollback: bool = False,
    ) -> None:
        if on_rollback:
            if not node.previous_object:
                node.deploying_object.delete(ctx, namespace)
                node.deployment_status = deployment_graph.DeploymentStatus.ROLLED_BACK
                return
            object_manager = ManagerFactory.get_manager(node.previous_object)
            done_status = deployment_graph.DeploymentStatus.ROLLED_BACK
        else:
            object_manager = node.deploying_object
            done_status = deployment_graph.DeploymentStatus.DONE
        try:
            await self._wait_for_dependencies(node, ctx, namespace, on_rollback)
            await self._apply_node(node, ctx, namespace, object_manager)
            node.deployment_status = done_status
        except NoActionNeeded:
            node.deployment_status = deployment_graph.DeploymentStatus.NO_ACTION_NEEDED
        except:
            node.deployment_status = deployment_graph.DeploymentStatus.FAILED
            raise

    async def _apply_node(
        self,
        node: deployment_graph.ObjectNode,
        ctx: ClientContext,
        namespace: str | None,
        object_manager: deployment_graph.ObjectManager,
    ) -> None:
        try:
            existing_obj = object_manager.read(ctx, namespace)
            # compare with existing object and determine action
            compare_result = object_comparer.determine_update_action(
                object_manager.k8s_object, existing_obj
            )
            if compare_result.no_action_needed:
                raise NoActionNeeded(
                    f"No action needed for {node.identifier} -> {compare_result=}"
                )
            if compare_result.needs_patch:
                object_manager.patch(
                    ctx, namespace, patch_doc=compare_result.patch_document()
                )
            else:
                object_manager.replace(ctx, namespace)
        except api_exceptions.ApiOperationException as ex:
            if ex.not_found:
                object_manager.create(ctx, namespace)
            else:
                raise

    async def rollback_node(
        self,
        node: deployment_graph.ObjectNode,
        ctx: ClientContext,
        namespace: str | None,
    ) -> None:
        if node.deployment_status in [
            deployment_graph.DeploymentStatus.NO_ACTION_NEEDED,
            deployment_graph.DeploymentStatus.PENDING,
        ]:
            return
        await self.apply_node(node, ctx, namespace, on_rollback=True)

    async def rollback(
        self,
        ctx: ClientContext,
        namespace: str | None,
    ) -> None:
        self.waited_nodes = set()
        for level_nodes in self.deployed_nodes:
            await asyncio.gather(
                *(self.rollback_node(node, ctx, namespace) for node in level_nodes)
            )
        self.status = ExecutionStatus.ROLLED_BACK
