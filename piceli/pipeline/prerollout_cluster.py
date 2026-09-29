"""Pre-rollout checks, cluster part: read what a check needs and run its Job.

One :class:`PreRolloutCluster` wraps an API client bound to a target. It reads
Secrets, ConfigMaps, claims and pods, creates a check Job, waits for it, keeps
an exit status and a scrubbed log tail, and always removes the Job again. No
Secret value leaves this module: values are read only to redact them from log
tails, and never returned.

Importing this module is side-effect free (the ``kubernetes`` client is
imported when a cluster is built).
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from piceli.pipeline.errors import PipelineError
from piceli.pipeline.prerollout import (
    APP_LABEL,
    CHECK_LABEL,
    DEADLINE_SLACK_SECONDS,
    scrub,
)

#: Container waiting reasons that no retry fixes: fail at once.
FATAL_WAITING = frozenset(
    {
        "CreateContainerConfigError",
        "InvalidImageName",
        "ErrImageNeverPull",
        "ImagePullBackOff",
    }
)
#: Pod events that mean the pod cannot get what it mounts or where it needs to run.
STUCK_EVENTS = frozenset(
    {"FailedMount", "FailedAttachVolume", "FailedScheduling", "FailedCreatePodSandBox"}
)


class Unreadable(Exception):
    """The API refused a read (for example RBAC): the caller skips that check."""


@dataclass
class Outcome:
    """How a check Job ended (never holds a secret value)."""

    state: str  # passed | failed | timeout | not-startable
    seconds: float = 0.0
    exit_code: int | None = None
    reason: str | None = None
    category: str | None = None
    log_tail: str = ""
    cleaned: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "state": self.state,
            "seconds": round(self.seconds, 1),
            "cleaned": self.cleaned,
        }
        for key in ("exit_code", "reason", "category"):
            if getattr(self, key) is not None:
                value[key] = getattr(self, key)
        if self.log_tail:
            value["log_tail"] = self.log_tail
        return value


class PreRolloutCluster:
    """The reads and writes a pre-rollout stage needs, over one API client.

    :param client: A ``kubernetes.client.ApiClient`` for the target.
    :param namespace: The target namespace (every object is namespaced).
    :param request_seconds: Timeout of one API request.
    :param poll_seconds: Interval between Job status reads.
    :param mount_grace_seconds: How long a pod may be stuck on a mount,
        scheduling or sandbox event before the check fails as not startable.
    """

    def __init__(
        self,
        client: Any,
        namespace: str,
        *,
        request_seconds: float = 30.0,
        poll_seconds: float = 2.0,
        mount_grace_seconds: float = 20.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.namespace = namespace
        self.request_seconds = request_seconds
        self.poll_seconds = poll_seconds
        self.mount_grace_seconds = mount_grace_seconds
        self.clock = clock
        self.sleep = sleep

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------- reads
    def _get(self, path: str, **query: Any) -> Any:
        from kubernetes.client.exceptions import ApiException

        try:
            response = self.client.call_api(
                path,
                "GET",
                query_params=list(query.items()),
                header_params={"Accept": "application/json"},
                auth_settings=["BearerToken"],
                _preload_content=False,
                _request_timeout=self.request_seconds,
            )
        except ApiException as error:
            if error.status == 404:
                return None
            if error.status in {401, 403}:
                raise Unreadable(path) from None
            raise
        return json.loads(_raw(response).data)

    def _ns(self, *parts: str) -> str:
        return f"/api/v1/namespaces/{self.namespace}/" + "/".join(parts)

    def read_object(self, kind: str, name: str) -> set[str] | None:
        """The keys of a Secret or ConfigMap, or ``None`` when it does not exist.

        :raises Unreadable: when the API refuses the read.
        """
        found = self._get(
            self._ns("secrets" if kind == "Secret" else "configmaps", name)
        )
        if found is None:
            return None
        return set(found.get("data") or {}) | set(found.get("binaryData") or {})

    def secret_values(self, names: Iterable[str]) -> list[str]:
        """Decoded values of the named Secrets, for redaction only."""
        values: list[str] = []
        for name in names:
            try:
                found = self._get(self._ns("secrets", name))
            except Unreadable:
                continue
            for value in ((found or {}).get("data") or {}).values():
                try:
                    values.append(base64.b64decode(value).decode())
                except Exception:
                    continue
        return values

    def read_claim(self, name: str) -> dict[str, Any] | None:
        """``{"phase", "modes"}`` of a claim, or ``None`` when it does not exist."""
        found = self._get(self._ns("persistentvolumeclaims", name))
        if found is None:
            return None
        return {
            "phase": (found.get("status") or {}).get("phase"),
            "modes": list((found.get("spec") or {}).get("accessModes") or []),
        }

    def pods_using(self, claim: str) -> list[dict[str, Any]]:
        """Running or pending pods (any owner) that mount ``claim``, with their node."""
        found = self._get(self._ns("pods")) or {}
        pods = []
        for pod in found.get("items") or []:
            phase = (pod.get("status") or {}).get("phase")
            if phase not in {"Running", "Pending"} or (pod.get("metadata") or {}).get(
                "deletionTimestamp"
            ):
                continue
            claims = {
                (item.get("persistentVolumeClaim") or {}).get("claimName")
                for item in (pod.get("spec") or {}).get("volumes") or []
            }
            if claim in claims:
                pods.append(
                    {
                        "name": pod["metadata"]["name"],
                        "node": (pod.get("spec") or {}).get("nodeName"),
                    }
                )
        return pods

    # ------------------------------------------------------------- writes
    def remove_stale(self, app: str) -> int:
        """Delete check Jobs of ``app`` that an earlier, interrupted run left."""
        found = self._get(
            "/apis/batch/v1/namespaces/" + self.namespace + "/jobs",
            labelSelector=f"{CHECK_LABEL},{APP_LABEL}={app}",
        )
        removed = 0
        for job in (found or {}).get("items") or []:
            self.delete_job(job["metadata"]["name"])
            removed += 1
        return removed

    def _send(self, path: str, method: str, body: Any, **query: Any) -> Any:
        return self.client.call_api(
            path,
            method,
            query_params=list(query.items()),
            header_params={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            body=body,
            auth_settings=["BearerToken"],
            _preload_content=False,
            _request_timeout=self.request_seconds,
        )

    def delete_job(self, name: str) -> bool:
        """Delete a check Job and its pods; ``True`` when it is gone or was absent."""
        from kubernetes.client.exceptions import ApiException

        path = f"/apis/batch/v1/namespaces/{self.namespace}/jobs/{name}"
        for _ in range(4):
            try:
                current = self._get(path)
            except Unreadable:
                return False
            if current is None:
                return True
            meta = current["metadata"]
            try:
                self._send(
                    path,
                    "DELETE",
                    {
                        "propagationPolicy": "Background",
                        "preconditions": {
                            "uid": meta["uid"],
                            "resourceVersion": meta["resourceVersion"],
                        },
                    },
                )
                return True
            except ApiException as error:
                if error.status == 404:
                    return True
                if error.status != 409:
                    return False
        return False

    # ------------------------------------------------------------ the Job
    def run_job(self, job: Mapping[str, Any], *, redact: Iterable[str] = ()) -> Outcome:
        """Create ``job``, wait for it, keep its outcome, and always remove it.

        The wait ends when the Job completes or fails, when a pod is stuck
        (see :data:`FATAL_WAITING`, :data:`STUCK_EVENTS`), or ``timeout`` plus
        a slack after creation. The Job is deleted in every case, including
        an interrupt.

        :raises PipelineError: ``prerollout-unavailable`` when the Job cannot
            be created (RBAC, quota, admission).
        """
        from kubernetes.client.exceptions import ApiException

        name = job["metadata"]["name"]
        timeout = int(job["spec"]["activeDeadlineSeconds"])
        started = self.clock()
        outcome: Outcome | None = None
        try:
            try:
                self._send(
                    f"/apis/batch/v1/namespaces/{self.namespace}/jobs",
                    "POST",
                    job,
                    fieldManager="piceli-prerollout",
                )
            except ApiException as error:
                raise PipelineError(
                    "prerollout-unavailable",
                    f"the API refused to create the check Job {name} "
                    f"(HTTP {error.status})",
                    failed=True,
                ) from None
            outcome = self._wait(name, timeout + DEADLINE_SLACK_SECONDS, started)
            outcome.log_tail = self._logs(name, redact)
            outcome.seconds = self.clock() - started
            return outcome
        finally:
            gone = self.delete_job(name)
            if outcome is not None:
                outcome.cleaned = gone

    def _wait(self, name: str, allowed: float, started: float) -> Outcome:
        job_path = f"/apis/batch/v1/namespaces/{self.namespace}/jobs/{name}"
        stuck_since: float | None = None
        while True:
            job = self._get(job_path)
            status = (job or {}).get("status") or {}
            for condition in status.get("conditions") or []:
                if condition.get("status") != "True":
                    continue
                if condition.get("type") == "Complete":
                    return Outcome("passed", exit_code=0)
                if condition.get("type") == "Failed":
                    pod = self._pod(name)
                    code = _exit_code(pod)
                    if condition.get("reason") == "DeadlineExceeded":
                        return Outcome("timeout", reason="job-deadline-exceeded")
                    return Outcome(
                        "failed", exit_code=code, reason=condition.get("reason")
                    )
            pod = self._pod(name)
            fatal = _fatal_waiting(pod)
            if fatal is not None:
                return Outcome("not-startable", reason=fatal[0], category=fatal[1])
            event = self._stuck_event(pod) if pod is not None else None
            if event is not None:
                stuck_since = stuck_since if stuck_since is not None else self.clock()
                if self.clock() - stuck_since >= self.mount_grace_seconds:
                    return Outcome(
                        "not-startable", reason=event, category=_category(event)
                    )
            else:
                stuck_since = None
            if self.clock() - started >= allowed:
                return Outcome("timeout", reason="deadline")
            self.sleep(self.poll_seconds)

    def _pod(self, job: str) -> dict[str, Any] | None:
        found = self._get(self._ns("pods"), labelSelector=f"job-name={job}") or {}
        items = found.get("items") or []
        return items[-1] if items else None

    def _logs(self, job: str, redact: Iterable[str]) -> str:
        pod = self._pod(job)
        if pod is None:
            return ""
        from kubernetes.client.exceptions import ApiException

        try:
            response = self.client.call_api(
                self._ns("pods", pod["metadata"]["name"], "log"),
                "GET",
                query_params=[
                    ("container", "check"),
                    ("tailLines", 60),
                    ("limitBytes", 16384),
                ],
                header_params={"Accept": "text/plain"},
                auth_settings=["BearerToken"],
                _preload_content=False,
                _request_timeout=self.request_seconds,
            )
        except ApiException:
            return ""
        return scrub(_raw(response).data.decode(errors="replace"), redact)

    def _stuck_event(self, pod: Mapping[str, Any]) -> str | None:
        if _started(pod):
            return None
        found = (
            self._get(
                self._ns("events"),
                fieldSelector=f"involvedObject.name={pod['metadata']['name']}",
            )
            or {}
        )
        for event in found.get("items") or []:
            if event.get("reason") in STUCK_EVENTS:
                return str(event["reason"])
        return None


# ---------------------------------------------------------------- helpers


def _raw(response: Any) -> Any:
    """The HTTP response of ``call_api`` (a tuple in some client versions)."""
    return response[0] if isinstance(response, tuple) else response


def _status(pod: Mapping[str, Any] | None) -> dict[str, Any]:
    for item in ((pod or {}).get("status") or {}).get("containerStatuses") or []:
        if item.get("name") == "check":
            return dict(item)
    return {}


def _exit_code(pod: Mapping[str, Any] | None) -> int | None:
    terminated = (_status(pod).get("state") or {}).get("terminated") or {}
    code = terminated.get("exitCode")
    return code if isinstance(code, int) else None


def _started(pod: Mapping[str, Any]) -> bool:
    state = _status(pod).get("state") or {}
    return "running" in state or "terminated" in state


def _category(reason: str) -> str:
    if reason in {"FailedMount", "FailedAttachVolume", "CreateContainerConfigError"}:
        return "mount"
    if reason == "FailedScheduling":
        return "scheduling"
    if reason in {"InvalidImageName", "ErrImageNeverPull", "ImagePullBackOff"}:
        return "image"
    return "pod"


def _fatal_waiting(pod: Mapping[str, Any] | None) -> tuple[str, str] | None:
    waiting = (_status(pod).get("state") or {}).get("waiting") or {}
    reason = waiting.get("reason")
    if reason in FATAL_WAITING:
        return str(reason), _category(str(reason))
    return None
