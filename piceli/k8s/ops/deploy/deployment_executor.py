import asyncio
import logging
from enum import StrEnum, auto
from typing import NamedTuple

from piceli.k8s.exceptions import api_exceptions
from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.object_manager.factory import ManagerFactory
from piceli.k8s.ops.compare import object_comparer
from piceli.k8s.ops.deploy import deployment_graph, deployment_progress

logger = logging.getLogger(__name__)


class NoActionNeeded(Exception):
    pass


class ExecutionStatus(StrEnum):
    PENDING = auto()
    DONE = auto()
    FAILED = auto()
    ROLLED_BACK = auto()


class LevelNodes(NamedTuple):
    level: int
    nodes: list[deployment_graph.ObjectNode]


class DeploymentExecutor:
    def __init__(self, graph: deployment_graph.DeploymentGraph):
        self.graph = graph
        self.deployed_nodes: list[LevelNodes] = []
        self.waited_nodes: set = set()
        self.status = ExecutionStatus.PENDING
        self.progress: list[deployment_progress.Progress] = []

    @property
    def is_done(self) -> bool:
        return self.status == ExecutionStatus.DONE

    @property
    def is_rolled_back(self) -> bool:
        return self.status == ExecutionStatus.ROLLED_BACK

    @property
    def is_final(self) -> bool:
        return self.is_done or self.is_rolled_back

    async def wait_for_all(self, ctx: ClientContext, namespace: str | None) -> None:
        for level, nodes in self.deployed_nodes:
            logger.info(f"waiting for graph {level=} {nodes=}")
            await asyncio.gather(
                *(self._wait_for_node(node, ctx, namespace) for node in nodes)
            )

    async def deploy(self, ctx: ClientContext, namespace: str | None = None) -> None:
        self.progress.append(deployment_progress.ExecutionProgress.deploy(self.status))
        try:
            for level, nodes in enumerate(self.graph.traverse_graph()):
                self.progress.append(
                    deployment_progress.GraphLevelProgress.apply(level, nodes)
                )
                await asyncio.gather(
                    *(self.apply_node(node, ctx, namespace) for node in nodes)
                )
                self.progress.append(
                    deployment_progress.GraphLevelProgress.success(level, nodes)
                )
                self.deployed_nodes.append(LevelNodes(level, nodes))
            self.status = ExecutionStatus.DONE
            self.progress.append(
                deployment_progress.ExecutionProgress.success(self.status)
            )
        except Exception as ex:
            logger.error(f"Deployment failed: {ex}")
            self.status = ExecutionStatus.FAILED
            self.progress.append(
                deployment_progress.ExecutionProgress.error(self.status, ex)
            )
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
            self.progress.append(deployment_progress.NodeProgress.rollback(node))
        else:
            object_manager = node.deploying_object
            done_status = deployment_graph.DeploymentStatus.DONE
            self.progress.append(deployment_progress.NodeProgress.apply(node))
        try:
            await self._wait_for_dependencies(node, ctx, namespace, on_rollback)
            await self._apply_node(node, ctx, namespace, object_manager)
            node.deployment_status = done_status
            self.progress.append(deployment_progress.NodeProgress.done(node))
        except NoActionNeeded:
            node.deployment_status = deployment_graph.DeploymentStatus.NO_ACTION_NEEDED
            self.progress.append(deployment_progress.NodeProgress.done(node))
        except Exception as ex:
            node.deployment_status = deployment_graph.DeploymentStatus.FAILED
            self.progress.append(deployment_progress.NodeProgress.error(node, ex))
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
            self.progress.append(
                deployment_progress.NodeProgress.compare(node, compare_result)
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
                self.progress.append(deployment_progress.NodeProgress.new_obj(node))
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
        self.progress.append(
            deployment_progress.ExecutionProgress.rollback(self.status)
        )
        self.waited_nodes = set()
        for level, nodes in self.deployed_nodes:
            self.progress.append(
                deployment_progress.GraphLevelProgress.rollback(level, nodes)
            )
            await asyncio.gather(
                *(self.rollback_node(node, ctx, namespace) for node in nodes)
            )
        self.status = ExecutionStatus.ROLLED_BACK
        self.progress.append(
            deployment_progress.ExecutionProgress.rolled_back(self.status)
        )
