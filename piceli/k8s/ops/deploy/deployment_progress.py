import traceback
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from piceli.k8s.ops.compare import object_comparer
    from piceli.k8s.ops.deploy import deployment_graph
    from piceli.k8s.ops.deploy.deployment_executor import ExecutionStatus


class Progress:
    """Keeps track of the Progress of the Deployment Executor"""


class ExecutionEvent(StrEnum):
    START_DEPLOY = auto()
    SUCCESS = auto()
    ERROR = auto()
    START_ROLLBACK = auto()
    ROLLED_BACK = auto()


@dataclass
class ExecutionProgress(Progress):
    status: "ExecutionStatus"
    event: ExecutionEvent
    exception: Exception | None = None
    traceback: str | None = None

    @classmethod
    def deploy(cls, status: "ExecutionStatus") -> "ExecutionProgress":
        return cls(status, ExecutionEvent.START_DEPLOY)

    @classmethod
    def success(cls, status: "ExecutionStatus") -> "ExecutionProgress":
        return cls(status, ExecutionEvent.SUCCESS)

    @classmethod
    def rollback(cls, status: "ExecutionStatus") -> "ExecutionProgress":
        return cls(status, ExecutionEvent.START_ROLLBACK)

    @classmethod
    def rolled_back(cls, status: "ExecutionStatus") -> "ExecutionProgress":
        return cls(status, ExecutionEvent.ROLLED_BACK)

    @classmethod
    def error(
        cls, status: "ExecutionStatus", exception: Exception
    ) -> "ExecutionProgress":
        return cls(status, ExecutionEvent.ERROR, exception, traceback.format_exc())


class GraphLevelEvent(StrEnum):
    START_APPLY = auto()
    START_ROLLBACK = auto()
    SUCCESS = auto()


@dataclass
class GraphLevelProgress(Progress):
    level_id: int
    nodes: list["deployment_graph.ObjectNode"]
    event: GraphLevelEvent

    @classmethod
    def apply(
        cls, level_id: int, nodes: list["deployment_graph.ObjectNode"]
    ) -> "GraphLevelProgress":
        return cls(level_id, nodes, GraphLevelEvent.START_APPLY)

    @classmethod
    def rollback(
        cls, level_id: int, nodes: list["deployment_graph.ObjectNode"]
    ) -> "GraphLevelProgress":
        return cls(level_id, nodes, GraphLevelEvent.START_ROLLBACK)

    @classmethod
    def success(
        cls, level_id: int, nodes: list["deployment_graph.ObjectNode"]
    ) -> "GraphLevelProgress":
        return cls(level_id, nodes, GraphLevelEvent.SUCCESS)


class NodeEvent(StrEnum):
    START_APPLY = auto()
    START_ROLLBACK = auto()
    COMPARE = auto()
    NEW_OBJ = auto()
    ERROR = auto()
    COMPLETE = auto()


@dataclass
class NodeProgress(Progress):
    node: "deployment_graph.ObjectNode"
    deployment_status: "deployment_graph.DeploymentStatus"
    event: NodeEvent
    exception: Exception | None = None
    traceback: str | None = None
    compare_result: Optional["object_comparer.CompareResult"] = None

    @classmethod
    def apply(cls, node: "deployment_graph.ObjectNode") -> "NodeProgress":
        return cls(node, node.deployment_status, NodeEvent.START_APPLY)

    @classmethod
    def rollback(cls, node: "deployment_graph.ObjectNode") -> "NodeProgress":
        return cls(node, node.deployment_status, NodeEvent.START_ROLLBACK)

    @classmethod
    def done(cls, node: "deployment_graph.ObjectNode") -> "NodeProgress":
        return cls(node, node.deployment_status, NodeEvent.COMPLETE)

    @classmethod
    def new_obj(cls, node: "deployment_graph.ObjectNode") -> "NodeProgress":
        return cls(node, node.deployment_status, NodeEvent.NEW_OBJ)

    @classmethod
    def compare(
        cls,
        node: "deployment_graph.ObjectNode",
        compare_result: "object_comparer.CompareResult",
    ) -> "NodeProgress":
        return cls(
            node,
            node.deployment_status,
            NodeEvent.COMPARE,
            compare_result=compare_result,
        )

    @classmethod
    def error(
        cls, node: "deployment_graph.ObjectNode", exception: Exception
    ) -> "NodeProgress":
        return cls(
            node=node,
            deployment_status=node.deployment_status,
            event=NodeEvent.ERROR,
            exception=exception,
            traceback=traceback.format_exc(),
        )
