import logging
from enum import StrEnum, auto
from typing import Any, Callable, Iterator, Optional

from kubernetes import client
from urllib3.exceptions import HTTPError

from piceli import settings
from piceli.k8s.constants import phases, wait_conditions
from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.utils import utils_retry

logger = logging.getLogger(__name__)


class WaitException(Exception):
    """Wait Exception"""


class ImagePullException(Exception):
    """Image Pull Exception"""


class PullImageError(Exception):
    """K8S Pull Image Error"""


class WaitResult(StrEnum):
    """Wait result"""

    CONDITIONS_OK = auto()
    PHASE_OK = auto()
    READINESS_OK = auto()
    ALL_REPLICAS_OK = auto()
    ENOUGH_REPLICAS_OK = auto()
    EXISTS_OK = auto()


@utils_retry.retry_for_exception(HTTPError)
def wait(
    ctx: ClientContext,
    list_func: Callable,
    args: tuple,
    obj_name: str,
    condition: Optional[wait_conditions.WaitCondition] = None,
    phases: Optional[list[phases.Phase]] = None,
    check_readiness: bool = False,
    label_selector: Optional[str] = None,
    check_replicas: bool = False,
) -> WaitResult:
    """Simplified wait function."""
    logger.info("Waiting for %s", obj_name)
    field_selector = None if label_selector else f"metadata.name={obj_name}"
    phases_set = {p.value for p in phases} if phases else set()
    last_event = None
    for event in ctx.watch.stream(
        list_func,
        *args,
        field_selector=field_selector,
        label_selector=label_selector,
        timeout_seconds=settings.K8S_WAIT_TIMEOUT,
        _request_timeout=settings.K8S_WAIT_REQUEST_TIMEOUT,
    ):
        last_event = event
        if result := process_event(
            event, condition, phases_set, check_readiness, check_replicas, obj_name
        ):
            ctx.watch.stop()
            return result
        if check_for_image_pull_error(ctx, event):
            raise PullImageError(f"Abort wait! Failed to pull image for {obj_name}.")
        logger.info(f"Still waiting for {obj_name}")
    raise WaitException(f"Timeout waiting for {obj_name} ({last_event=})")


def process_event(
    event: Any,
    condition: Optional[wait_conditions.WaitCondition],
    phases: set[str],
    check_readiness: bool,
    check_replicas: bool,
    obj_name: str,
) -> Optional[WaitResult]:
    """Process individual watch events."""

    if "type" in event and event["type"] == "ADDED":
        return WaitResult.EXISTS_OK

    status = event["object"].status
    if condition and check_condition(condition, status):
        return WaitResult.CONDITIONS_OK
    if phases and phase_met(status, phases):
        return WaitResult.PHASE_OK
    if check_failure(status):
        raise WaitException(f"{obj_name} encountered a failure condition.")
    if check_readiness and readiness_met(status):
        return WaitResult.READINESS_OK
    if check_replicas:
        if result := check_replica_status(status):
            return result
    if not condition and not phases and not check_replicas:
        return WaitResult.EXISTS_OK
    return None


def check_condition(
    condition: wait_conditions.WaitCondition,
    status: Any,
) -> bool:
    for _condition in status.conditions or []:
        if _condition.type == condition.value:
            return bool(_condition.status)
    return False


def phase_met(status: Any, phases: set[str]) -> bool:
    """Check if the object is in one of the acceptable phases."""
    current_phase = getattr(status, "phase", None)
    return current_phase in phases if phases else False


def check_failure(status: Any) -> bool:
    """Check for failure conditions in the status."""
    return getattr(status, "failed", 0) > 0


def readiness_met(status: Any) -> bool:
    """Check if the object is ready."""
    container_statuses = getattr(status, "container_statuses", [])
    return any(container_status.ready for container_status in container_statuses)


def check_replica_status(status: Any) -> Optional[WaitResult]:
    """Check the status of replicas."""
    if isinstance(status, (client.V1ReplicaSetStatus, client.V1StatefulSetStatus)):
        if status.replicas == status.available_replicas:
            return WaitResult.ALL_REPLICAS_OK
        if status.available_replicas > 1 and status.ready_replicas > 1:
            return WaitResult.ENOUGH_REPLICAS_OK
    return None


def explore_object_pods(ctx: ClientContext, event: dict) -> Iterator[client.V1Pod]:
    _object = event["object"]
    namespace, selector = None, None
    if hasattr(_object, "metadata"):
        if hasattr(_object.metadata, "namespace"):
            namespace = _object.metadata.namespace
        if hasattr(_object.metadata, "name"):
            name = _object.metadata.name
    if hasattr(_object, "kind"):
        selector = f"{_object.kind.lower()}-name"
    if namespace and selector:
        yield from ctx.core_api.list_namespaced_pod(
            namespace, label_selector=f"{selector}={name}"
        ).items


def check_for_image_pull_error(ctx: ClientContext, event: Any) -> bool:
    """Check for image pull errors."""
    for pod in explore_object_pods(ctx, event):
        if not pod.status.container_statuses:
            continue
        for container_status in pod.status.container_statuses:
            if (
                waiting_status := container_status.state.waiting
            ) and waiting_status.reason in [
                "ErrImagePull",
                "ImagePullBackOff",
            ]:
                return True
    return False
