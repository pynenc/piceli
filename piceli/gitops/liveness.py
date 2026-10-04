"""Is the GitOps controller actually running? (0.15.1)

The status ConfigMap is written by the controller itself, so a controller
that crash-loops leaves its last document behind, looking current. These
helpers read the controller's pod (waiting reason, restarts, the last exit
message Kubernetes keeps) and the age of the last poll, and give one verdict
that ``gitops status``, ``cluster status`` and the UI all show.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from piceli.integrity import EXIT_CORRUPTED, MESSAGE

#: The controller's pods (``app.kubernetes.io/name`` of its Deployment).
POD_SELECTOR = "app.kubernetes.io/name=piceli-gitops"
LINE_CHARS = 300


def _last_line(message: Any) -> str | None:
    if not isinstance(message, str):
        return None
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    return lines[-1][:LINE_CHARS] if lines else None


def _seconds_since(value: Any, now: float) -> float | None:
    if not value:
        return None
    try:
        return (
            now - datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        )
    except ValueError:
        return None


def pod_summary(pods: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The newest controller pod: phase, readiness, restarts, the last exit."""
    if not pods:
        return None
    pod = max(
        pods,
        key=lambda item: str(
            (item.get("metadata") or {}).get("creationTimestamp") or ""
        ),
    )
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    container = next(
        (item for item in statuses if item.get("name") == "controller"),
        statuses[0] if statuses else {},
    )
    state = container.get("state") or {}
    terminated = (
        (container.get("lastState") or {}).get("terminated")
        or state.get("terminated")
        or {}
    )
    code = terminated.get("exitCode")
    line = _last_line(terminated.get("message"))
    corrupted = code == EXIT_CORRUPTED or (line is not None and MESSAGE in line)
    return {
        "pod": (pod.get("metadata") or {}).get("name"),
        "node": (pod.get("spec") or {}).get("nodeName"),
        "phase": (pod.get("status") or {}).get("phase"),
        "ready": bool(container.get("ready")),
        "restarts": int(container.get("restartCount") or 0),
        "waiting": (state.get("waiting") or {}).get("reason"),
        "last_exit": {
            "code": code,
            "reason": terminated.get("reason"),
            "at": terminated.get("finishedAt"),
            "line": line,
        }
        if terminated
        else None,
        "self_check": "files-corrupted" if corrupted else None,
    }


def liveness(
    document: Mapping[str, Any] | None,
    pod: Mapping[str, Any] | None,
    now: float,
    *,
    pod_readable: bool = True,
) -> dict[str, Any]:
    """``running``, ``down``, ``stale`` or ``starting``, with why and what to do.

    ``down``: the pod is not ready (a crash loop, an image pull, a failed
    self-check). ``stale``: no poll for three poll intervals and a minute,
    whatever the pod says. Without a readable pod (no RBAC) only the age of
    the last poll decides.
    """
    controller = (document or {}).get("controller") or {}
    last_poll = controller.get("last_poll")
    age = _seconds_since(last_poll, now)
    limit = 3 * int(controller.get("poll_seconds") or 60) + 60
    state = "running"
    message = None
    if pod is not None and not pod.get("ready"):
        state = "down" if pod.get("restarts") or pod.get("waiting") else "starting"
        if pod.get("self_check") == "files-corrupted":
            message = MESSAGE
        else:
            parts = [pod.get("waiting") or pod.get("phase") or "not ready"]
            if pod.get("restarts"):
                parts.append(f"{pod['restarts']} restarts")
            line = (pod.get("last_exit") or {}).get("line")
            message = ", ".join(str(item) for item in parts) + (
                f"; last error: {line}" if line else ""
            )
    elif pod is None and pod_readable and document is not None:
        state, message = "down", "no controller pod"
    if state == "running" and age is not None and age > limit:
        state, message = (
            "stale",
            f"no poll for {int(age)} s (expected every {controller.get('poll_seconds') or 60} s)",
        )
    stale_data = state in {"down", "stale"} and document is not None
    return {
        "state": state,
        "message": message,
        "last_poll": last_poll,
        "pod": dict(pod) if pod is not None else None,
        # The document below was written before the controller stopped.
        "status_is_stale": stale_data,
        "note": f"status last written by the controller at {last_poll}; it is not running, so it may be out of date"
        if stale_data
        else None,
    }


def read_pods(api: Any, namespace: str) -> list[dict[str, Any]] | None:
    """The controller's pods (``api``: an object with ``call(path, method)``,
    like :class:`piceli.gitops.install.Api`); ``None`` when they cannot be read
    (no RBAC, unreachable): the age of the last poll still decides."""
    from urllib.parse import quote

    try:
        found = api.call(
            f"/api/v1/namespaces/{namespace}/pods?labelSelector={quote(POD_SELECTOR)}",
            "GET",
        )
    except Exception:
        return None
    items = (found or {}).get("items") if isinstance(found, Mapping) else None
    return [item for item in items or [] if isinstance(item, Mapping)]  # type: ignore[misc]


def check(
    api: Any, namespace: str, document: Mapping[str, Any] | None, now: float
) -> dict[str, Any]:
    """:func:`liveness` from the cluster: its pods read through ``api``."""
    pods = read_pods(api, namespace)
    return liveness(
        document,
        pod_summary(pods) if pods else None,
        now,
        pod_readable=pods is not None,
    )
