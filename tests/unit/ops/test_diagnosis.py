"""Pod causes of a workload that does not become ready (piceli.k8s.ops.diagnosis)."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from piceli.k8s.ops.diagnosis import (
    compact,
    current_pods,
    diagnose_workload,
    document,
    headline,
    human_lines,
    redact,
    secret_needles,
)


class Reader:
    """An in-memory :class:`PodReader`."""

    def __init__(
        self,
        pods: list[dict[str, Any]],
        replica_sets: list[dict[str, Any]] | None = None,
        logs: dict[tuple[str, str, bool], str] | None = None,
        events: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.pods = pods
        self.replica_sets = replica_sets or []
        self.logs = logs or {}
        self.events = events or {}
        self.log_calls: list[tuple[str, str, bool]] = []

    def list_pods(self, selector: str, **_: Any) -> list[dict[str, Any]]:
        assert selector == "app=web"
        return self.pods

    def list_replica_sets(self, selector: str, **_: Any) -> list[dict[str, Any]]:
        return self.replica_sets

    def pod_events(self, pod: str, **_: Any) -> list[dict[str, Any]]:
        return self.events.get(pod, [])

    def pod_log(self, pod: str, container: str, *, previous: bool, **_: Any) -> str:
        self.log_calls.append((pod, container, previous))
        if (pod, container, previous) not in self.logs:
            raise ValueError("no such log")
        return self.logs[(pod, container, previous)]


def _workload(kind: str, **extra: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "metadata": {"name": "web", "uid": "web-uid", "generation": 2, **extra},
        "spec": {"selector": {"matchLabels": {"app": "web"}}},
    }


def _pod(
    name: str,
    owner: str,
    *,
    labels: dict[str, str] | None = None,
    waiting: str | None = None,
    restarts: int = 0,
    exit_code: int | None = None,
    init: bool = False,
    phase: str = "Running",
    message: str = "",
) -> dict[str, Any]:
    status: dict[str, Any] = {
        "name": "app",
        "restartCount": restarts,
        "state": {"waiting": {"reason": waiting, "message": message}}
        if waiting
        else {"running": {}},
        "lastState": {"terminated": {"exitCode": exit_code, "reason": "Error"}}
        if exit_code is not None
        else {},
    }
    return {
        "kind": "Pod",
        "metadata": {
            "name": name,
            "labels": {"app": "web", **(labels or {})},
            "ownerReferences": [{"uid": owner}],
        },
        "status": {
            "phase": phase,
            ("initContainerStatuses" if init else "containerStatuses"): [status],
        },
    }


def _diagnose(reader: Reader, manifest: dict[str, Any], **kwargs: Any) -> Any:
    return diagnose_workload(
        reader,
        manifest,
        crash_restarts=kwargs.pop("crash_restarts", 3),
        known=kwargs.pop("known", lambda: ()),
        deadline=None,
        **kwargs,
    )


def test_deployment_counts_only_the_current_replica_set() -> None:
    deployment = _workload(
        "Deployment", annotations={"deployment.kubernetes.io/revision": "2"}
    )
    replica_sets = [
        {
            "metadata": {
                "uid": "rs-old",
                "annotations": {"deployment.kubernetes.io/revision": "1"},
                "ownerReferences": [{"uid": "web-uid"}],
            }
        },
        {
            "metadata": {
                "uid": "rs-new",
                "annotations": {"deployment.kubernetes.io/revision": "2"},
                "ownerReferences": [{"uid": "web-uid"}],
            }
        },
    ]
    old = _pod("web-old", "rs-old", waiting="CrashLoopBackOff", restarts=9)
    new = _pod("web-new", "rs-new", waiting="ContainerCreating")
    reader = Reader([old, new], replica_sets)
    assert current_pods(reader, deployment, None) == [new]
    assert _diagnose(reader, deployment) is None
    assert _diagnose(reader, deployment, final=True) is None


def test_unknown_revision_reports_nothing() -> None:
    reader = Reader([_pod("web-1", "rs", waiting="CrashLoopBackOff")])
    assert current_pods(reader, _workload("Deployment"), None) is None
    assert _diagnose(reader, _workload("Deployment")) is None
    statefulset = _workload("StatefulSet")  # no status.updateRevision yet
    assert current_pods(reader, statefulset, None) is None


def test_statefulset_and_daemonset_revisions() -> None:
    statefulset = _workload("StatefulSet") | {"status": {"updateRevision": "web-2"}}
    old = _pod(
        "web-0", "web-uid", labels={"controller-revision-hash": "web-1"}, waiting="X"
    )
    new = _pod("web-1", "web-uid", labels={"controller-revision-hash": "web-2"})
    assert current_pods(Reader([old, new]), statefulset, None) == [new]
    daemonset = _workload("DaemonSet")
    old = _pod("web-a", "web-uid", labels={"pod-template-generation": "1"})
    new = _pod("web-b", "web-uid", labels={"pod-template-generation": "2"})
    assert current_pods(Reader([old, new]), daemonset, None) == [new]


def test_init_crash_loop_with_logs_and_events() -> None:
    job = _workload("Job")
    pod = _pod("web-x", "web-uid", waiting="CrashLoopBackOff", restarts=1, init=True)
    pod["status"]["initContainerStatuses"][0]["lastState"] = {
        "terminated": {"exitCode": 2, "reason": "Error"}
    }
    reader = Reader(
        [pod],
        logs={("web-x", "app", True): "one\n\nmigration failed: token=abc123\n"},
        events={
            "web-x": [
                {"type": "Normal", "reason": "Pulled", "message": "ok", "count": 1},
                {
                    "type": "Warning",
                    "reason": "BackOff",
                    "message": "Back-off restarting",
                    "count": 4,
                    "lastTimestamp": "2026-01-01T00:00:09Z",
                },
            ]
        },
    )
    found = _diagnose(reader, job)
    assert found["fatal"] is True
    [cause] = found["causes"]
    assert cause["reason"] == "Init:CrashLoopBackOff"
    assert cause["init"] is True and cause["exit_code"] == 2
    assert cause["logs"] == ["one", "migration failed: token=[REDACTED]"]
    assert [event["reason"] for event in cause["events"]] == ["Pulled", "BackOff"]
    assert reader.log_calls == [("web-x", "app", True)]


def test_failed_job_without_pods_is_fatal() -> None:
    job = _workload("Job") | {
        "status": {
            "conditions": [
                {
                    "type": "Failed",
                    "status": "True",
                    "reason": "BackoffLimitExceeded",
                    "message": "Job has reached the specified backoff limit",
                }
            ]
        }
    }
    found = _diagnose(Reader([]), job)
    assert found["fatal"] is True
    assert found["causes"][0]["reason"] == "BackoffLimitExceeded"


def test_final_reports_non_fatal_causes() -> None:
    job = _workload("Job")
    pod = _pod("web-x", "web-uid", restarts=1, exit_code=1)
    reader = Reader([pod], logs={("web-x", "app", True): "slow start\n"})
    assert _diagnose(reader, job) is None
    found = _diagnose(reader, job, final=True)
    assert found["fatal"] is False
    assert found["causes"][0]["logs"] == ["slow start"]
    pending = _pod("web-y", "web-uid")
    pending["status"] = {
        "phase": "Pending",
        "conditions": [
            {
                "type": "PodScheduled",
                "status": "False",
                "reason": "Unschedulable",
                "message": "0/1 nodes are available",
            }
        ],
    }
    found = _diagnose(Reader([pending]), job, final=True)
    assert found["causes"][0]["reason"] == "Unschedulable"


@pytest.mark.parametrize(
    "reason",
    [
        "CrashLoopBackOff",
        "ImagePullBackOff",
        "ErrImagePull",
        "CreateContainerConfigError",
        "CreateContainerError",
        "RunContainerError",
    ],
)
def test_every_fatal_waiting_reason_fails_at_once(reason: str) -> None:
    found = _diagnose(Reader([_pod("p", "web-uid", waiting=reason)]), _workload("Job"))
    assert found["fatal"] is True and found["causes"][0]["reason"] == reason


def test_restart_threshold() -> None:
    pod = _pod("p", "web-uid", restarts=2, exit_code=137)
    assert _diagnose(Reader([pod]), _workload("Job"), crash_restarts=3) is None
    found = _diagnose(Reader([pod]), _workload("Job"), crash_restarts=2)
    assert found["causes"][0]["reason"] == "Error"


def test_known_secrets_are_read_only_when_there_is_a_cause() -> None:
    calls = []

    def known() -> tuple[str, ...]:
        calls.append(1)
        return ("s3cr3t-value",)

    reader = Reader([_pod("p", "web-uid", waiting="ContainerCreating")])
    assert _diagnose(reader, _workload("Job"), known=known) is None
    assert calls == []
    pod = _pod("p", "web-uid", waiting="CreateContainerConfigError")
    pod["status"]["containerStatuses"][0]["state"]["waiting"]["message"] = (
        "couldn't find key s3cr3t-value in Secret"
    )
    found = _diagnose(Reader([pod]), _workload("Job"), known=known)
    assert found["causes"][0]["message"] == "couldn't find key [REDACTED] in Secret"


def test_redact_patterns() -> None:
    assert redact("Authorization: Bearer abcdefghijkl") == (
        "Authorization: [REDACTED] [REDACTED]"
    )
    assert redact("GET https://user:pa55word@db.example:5432/x") == (
        "GET https://user:[REDACTED]@db.example:5432/x"
    )
    assert "[REDACTED]" in redact("jwt eyJhbGciOi.eyJzdWIiOiIx.c2lnbmF0dXJl")
    assert redact("-----BEGIN RSA PRIVATE KEY----- MIIE") == (
        "-----BEGIN RSA PRIVATE KEY-----[REDACTED]"
    )
    assert redact("password=hunter2, ok") == "password=[REDACTED], ok"
    assert len(redact("x" * 1000)) == 300
    assert redact("tab\there\x1b[0m") == "tab here [0m"


def test_secret_needles_skip_manifests_but_keep_secret_data() -> None:
    encoded = base64.b64encode(b"decoded-secret").decode()
    values = [
        "generated-value",
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "settings"}},
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "credentials"},
            "data": {"password": encoded},
            "stringData": {"url": "redis://cache:6379"},
        },
        {"nested": ["-----BEGIN KEY-----\nAAAABBBBCCCC\n-----END KEY-----"]},
        "abc",
    ]
    needles = secret_needles(values)
    assert "generated-value" in needles
    assert encoded in needles and "decoded-secret" in needles
    assert "redis://cache:6379" in needles
    assert "AAAABBBBCCCC" in needles
    assert "settings" not in needles and "credentials" not in needles
    assert "abc" not in needles  # too short to redact safely
    assert needles == tuple(sorted(needles, key=lambda item: (-len(item), item)))


def test_human_lines_and_compact() -> None:
    diagnosis = document(
        "apply-crashloop",
        [
            {
                "kind": "Deployment",
                "name": "api",
                "fatal": True,
                "causes": [
                    {
                        "container": "api",
                        "reason": "CrashLoopBackOff",
                        "exit_code": 101,
                        "restarts": 3,
                        "logs": ['bad "quote"', "Redis URL file must be owner-only"],
                        "events": [],
                    }
                ],
            },
            {
                "kind": "Deployment",
                "name": "worker-long",
                "fatal": True,
                "causes": [
                    {
                        "container": "w",
                        "reason": "ImagePullBackOff",
                        "exit_code": None,
                        "restarts": 0,
                        "message": "",
                        "logs": [],
                        "events": [
                            {"type": "Warning", "message": 'pull "x" failed'},
                        ],
                    }
                ],
            },
        ],
    )
    assert headline(diagnosis) == "2 workloads not starting"
    assert human_lines(diagnosis) == [
        '  api          api  exit 101          "Redis URL file must be owner-only"',
        "  worker-long  w    ImagePullBackOff  \"pull 'x' failed\"",
    ]
    assert compact(diagnosis)[1] == {
        "kind": "Deployment",
        "name": "worker-long",
        "container": "w",
        "reason": "ImagePullBackOff",
        "exit_code": None,
        "restarts": 0,
    }
    assert headline(None) is None and human_lines(None) == [] and compact(None) == []
    assert headline(document("readiness-timeout", diagnosis["workloads"])) == (
        "2 workloads not ready"
    )


def test_an_ended_container_is_read_before_its_previous_instance() -> None:
    pod = _pod("p", "web-uid", restarts=3, exit_code=1)
    status = pod["status"]["containerStatuses"][0]
    status["state"] = {"terminated": {"exitCode": 1, "reason": "Error"}}
    reader = Reader(
        [pod],
        logs={
            ("p", "app", False): "fatal: from the ended instance\n",
            ("p", "app", True): "unable to retrieve container logs for x://1",
        },
    )
    found = _diagnose(reader, _workload("Job"))
    assert found["causes"][0]["logs"] == ["fatal: from the ended instance"]
    assert reader.log_calls == [("p", "app", False)]
    # Waiting to restart: the previous instance, and a log the kubelet no
    # longer has is skipped for the current one.
    waiting = _pod("q", "web-uid", waiting="CrashLoopBackOff", restarts=1)
    reader = Reader(
        [waiting],
        logs={
            ("q", "app", True): "unable to retrieve container logs for x://2",
            ("q", "app", False): "",
        },
    )
    assert _diagnose(reader, _workload("Job"))["causes"][0]["logs"] == []
    assert reader.log_calls == [("q", "app", True), ("q", "app", False)]


def test_a_failed_pod_is_final_for_a_pod_but_retried_by_a_job() -> None:
    pod = _pod("web", "web-uid", phase="Failed")
    pod["status"]["containerStatuses"][0]["state"] = {
        "terminated": {"exitCode": 2, "reason": "Error"}
    }
    manifest = {"kind": "Pod", "metadata": {"name": "web"}} | pod
    found = _diagnose(Reader([]), manifest)
    assert found["fatal"] is True and found["causes"][0]["exit_code"] == 2
    assert _diagnose(Reader([pod]), _workload("Job")) is None
