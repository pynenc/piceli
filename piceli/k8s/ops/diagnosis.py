"""Why a workload does not become ready: its pods' causes, redacted and bounded.

While the executor waits for a Deployment, StatefulSet, DaemonSet, Job or Pod
to become ready it asks :func:`diagnose_workload` every few seconds. A pod of
the revision being rolled out whose container is in ``CrashLoopBackOff``
(``Init:CrashLoopBackOff`` for an init container), ``ImagePullBackOff``,
``ErrImagePull``, ``InvalidImageName``, ``CreateContainerConfigError``,
``CreateContainerError`` or ``RunContainerError``, or that restarted at least
``crash_restarts`` times, is **fatal**: waiting longer cannot help, so the
apply fails at once (``apply-crashloop``) instead of at the deadline. A failed
Job (``Failed`` condition, after its retries) and a bare Pod in phase
``Failed`` are fatal too.

Each cause carries the container, reason, exit code, restart count, the last
log lines and the latest events of its pod. Every text that came from the
cluster (logs, event and status messages) is **redacted** before it leaves
this module: values known to the release's secret store are replaced, and so
is anything that looks like a secret (see :func:`redact`). Lines, counts and
lengths are bounded, so a diagnosis stays small in the journal and the result.

Only pods of the revision being rolled out count (a Deployment's newest
ReplicaSet, a StatefulSet's ``updateRevision``, a DaemonSet's template
generation, a Job's or Pod's own uid): an old pod that crashes while the new
revision starts never fails the apply. When the revision cannot be told apart
(no revision yet, the ReplicaSets cannot be read) nothing is reported and the
executor keeps waiting as before.

Importing this module is side-effect free.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Protocol

SCHEMA = "piceli.diagnosis.v1"
#: Workload kinds whose pods are diagnosed.
WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Job", "Pod"})
#: Waiting reasons that no amount of waiting fixes.
FATAL_WAITING = frozenset(
    {
        "CrashLoopBackOff",
        "ImagePullBackOff",
        "ErrImagePull",
        "InvalidImageName",
        "CreateContainerConfigError",
        "CreateContainerError",
        "RunContainerError",
    }
)
#: Waiting reasons of a container that is simply starting.
NORMAL_WAITING = frozenset({"ContainerCreating", "PodInitializing"})
#: Reasons whose container never ran, so it has no logs to read.
_NO_LOGS = frozenset(
    {
        "ImagePullBackOff",
        "ErrImagePull",
        "InvalidImageName",
        "CreateContainerConfigError",
        "CreateContainerError",
    }
)
#: What the API server answers (200) for an instance whose log is gone.
_NO_LOG_TEXT = "unable to retrieve container logs"
LOG_LINES = 20
LOG_BYTES = 16_384
LINE_CHARS = 300
EVENTS = 5
CAUSES = 5
_MIN_SECRET = 4
_MIN_SECRET_LINE = 8

_PATTERNS = (
    # "Authorization: Bearer <token>", "bearer <token>"
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]{8,})"),
    # password=…, token: …, secret=…, key=…, authorization: … (as the
    # operator's log redaction does)
    re.compile(
        r"(?i)((?:password|passwd|token|secret|key|authorization|bearer)\s*[:=]\s*)"
        r"([^\s,;]+)"
    ),
    # Credentials in a URL: scheme://user:password@host
    re.compile(r"(://[^/\s:@]+:)([^/\s@]+)(?=@)"),
    # JSON Web Tokens
    re.compile(r"()(eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+)"),
    # PEM private key markers (the base64 body lines follow on their own)
    re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)(.*)"),
)


class PodReader(Protocol):
    """The read-only calls a diagnosis needs (a ``KubernetesProvider``)."""

    def list_pods(
        self, selector: str, *, deadline: float | None = ..., limit: int = ...
    ) -> list[dict[str, Any]]: ...

    def list_replica_sets(
        self, selector: str, *, deadline: float | None = ..., limit: int = ...
    ) -> list[dict[str, Any]]: ...

    def pod_events(
        self, pod: str, *, deadline: float | None = ..., limit: int = ...
    ) -> list[dict[str, Any]]: ...

    def pod_log(
        self,
        pod: str,
        container: str,
        *,
        previous: bool,
        tail_lines: int,
        limit_bytes: int,
        deadline: float | None = ...,
    ) -> str: ...


# ------------------------------------------------------------------ redaction


def secret_needles(values: Iterable[Any]) -> tuple[str, ...]:
    """Every string worth redacting from secret store values.

    Stored manifests contribute only a Secret's ``data``/``stringData``.
    Strings nested in mappings and lists, the decoded form of base64 values
    (Secret ``data``) and each longer line of a multi-line value (a PEM key
    shows up in a log one line at a time). Longest first, so a value is
    replaced before any of its parts.
    """
    found: set[str] = set()

    def add(text: str) -> None:
        text = text.strip()
        if len(text) < _MIN_SECRET:
            return
        found.add(text)
        if "\n" in text:
            found.update(
                line.strip()
                for line in text.splitlines()
                if len(line.strip()) >= _MIN_SECRET_LINE
            )

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            if isinstance(value.get("apiVersion"), str) and "kind" in value:
                # A stored manifest (receipts keep applied objects here too):
                # only a Secret's values are secret, never names or specs.
                if value.get("kind") == "Secret":
                    walk(value.get("data") or {})
                    walk(value.get("stringData") or {})
                return
            for item in value.values():
                walk(item)
        elif isinstance(value, list | tuple):
            for item in value:
                walk(item)
        elif isinstance(value, bytes):
            add(value.decode(errors="replace"))
        elif isinstance(value, str):
            add(value)
            try:
                decoded = base64.b64decode(value, validate=True).decode()
            except (binascii.Error, ValueError, UnicodeDecodeError):
                return
            add(decoded)

    walk(list(values))
    return tuple(sorted(found, key=lambda item: (-len(item), item)))


def redact(text: str, known: Sequence[str] = ()) -> str:
    """``text`` without known secret values or anything that looks like one.

    Known values become ``[REDACTED]``; so do ``password=…``/``token: …``
    style assignments, bearer tokens, URL credentials, JSON Web Tokens and
    PEM private key markers. The result is cut to :data:`LINE_CHARS`.
    """
    cleaned = text
    for value in known:
        if value and len(value) >= _MIN_SECRET:
            cleaned = cleaned.replace(value, "[REDACTED]")
    for pattern in _PATTERNS:
        cleaned = pattern.sub(r"\g<1>[REDACTED]", cleaned)
    cleaned = "".join(char if char.isprintable() else " " for char in cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) > LINE_CHARS:
        cleaned = cleaned[: LINE_CHARS - 1] + "…"
    return cleaned


# ------------------------------------------------------------------ selection


def _selector(manifest: Mapping[str, Any]) -> str | None:
    spec = manifest.get("spec") or {}
    selector = spec.get("selector") if isinstance(spec, Mapping) else None
    if not isinstance(selector, Mapping):
        return None
    labels = selector.get("matchLabels") or {}
    if not isinstance(labels, Mapping) or not labels:
        return None
    parts = [f"{key}={value}" for key, value in sorted(labels.items())]
    for expression in selector.get("matchExpressions") or ():
        if not isinstance(expression, Mapping):
            return None
        key, operator = expression.get("key"), expression.get("operator")
        values = ",".join(str(item) for item in expression.get("values") or ())
        if operator == "In":
            parts.append(f"{key} in ({values})")
        elif operator == "NotIn":
            parts.append(f"{key} notin ({values})")
        elif operator == "Exists":
            parts.append(str(key))
        elif operator == "DoesNotExist":
            parts.append(f"!{key}")
        else:
            return None
    return ",".join(parts)


def _owned_by(item: Mapping[str, Any], uid: str) -> bool:
    return any(
        isinstance(owner, Mapping) and owner.get("uid") == uid
        for owner in (item.get("metadata") or {}).get("ownerReferences") or ()
    )


def _labels(item: Mapping[str, Any]) -> Mapping[str, Any]:
    labels = (item.get("metadata") or {}).get("labels") or {}
    return labels if isinstance(labels, Mapping) else {}


_REVISION = "deployment.kubernetes.io/revision"


def current_pods(
    reader: PodReader, manifest: Mapping[str, Any], deadline: float | None
) -> list[dict[str, Any]] | None:
    """The pods of the revision being rolled out, or ``None`` when unknown."""
    kind = manifest.get("kind")
    metadata = manifest.get("metadata") or {}
    uid = metadata.get("uid")
    if kind == "Pod":
        return [dict(manifest)]
    if not isinstance(uid, str) or not uid:
        return None
    selector = _selector(manifest)
    if selector is None:
        return None
    if kind == "Deployment":
        revision = (metadata.get("annotations") or {}).get(_REVISION)
        if not revision:
            return None
        owners = {
            (item.get("metadata") or {}).get("uid")
            for item in reader.list_replica_sets(selector, deadline=deadline)
            if _owned_by(item, uid)
            and ((item.get("metadata") or {}).get("annotations") or {}).get(_REVISION)
            == revision
        }
        owners.discard(None)
        if not owners:
            return None
        return [
            pod
            for pod in reader.list_pods(selector, deadline=deadline)
            if any(_owned_by(pod, str(owner)) for owner in owners)
        ]
    pods = [
        pod
        for pod in reader.list_pods(selector, deadline=deadline)
        if _owned_by(pod, uid)
    ]
    if kind == "StatefulSet":
        update = (manifest.get("status") or {}).get("updateRevision")
        if not update:
            return None
        return [
            pod
            for pod in pods
            if _labels(pod).get("controller-revision-hash") == update
        ]
    if kind == "DaemonSet":
        generation = str(metadata.get("generation", ""))
        return [
            pod
            for pod in pods
            if str(_labels(pod).get("pod-template-generation", "")) == generation
        ]
    if kind == "Job":
        return pods
    return None


# --------------------------------------------------------------------- causes


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _container_causes(
    pod: Mapping[str, Any], crash_restarts: int, final: bool, kind: str = "Pod"
) -> list[dict[str, Any]]:
    """Cause records (with a ``fatal`` flag) for one pod's containers.

    A failed pod is final only for a bare Pod: a Job retries it (its own
    ``Failed`` condition says when it gave up).
    """
    status = pod.get("status") or {}
    name = str((pod.get("metadata") or {}).get("name", ""))
    failed_pod = status.get("phase") == "Failed" and kind == "Pod"
    causes = []
    for init, key in ((True, "initContainerStatuses"), (False, "containerStatuses")):
        for container in status.get(key) or ():
            if not isinstance(container, Mapping):
                continue
            state = container.get("state") or {}
            waiting = state.get("waiting") or {}
            running_terminated = state.get("terminated") or {}
            last = (container.get("lastState") or {}).get("terminated") or {}
            terminated = running_terminated or last
            reason = waiting.get("reason")
            restarts = _int(container.get("restartCount")) or 0
            exit_code = _int(terminated.get("exitCode"))
            failed_exit = (
                bool(running_terminated)
                and (_int(running_terminated.get("exitCode")) or 0) != 0
            )
            fatal = (
                reason in FATAL_WAITING
                or restarts >= crash_restarts
                or (failed_pod and failed_exit)
            )
            notable = fatal or (
                final
                and (
                    (reason and reason not in NORMAL_WAITING)
                    or restarts > 0
                    or failed_exit
                )
            )
            if not notable:
                continue
            label = str(
                reason
                or running_terminated.get("reason")
                or (last.get("reason") if restarts else None)
                or ("Restarting" if restarts else "NotReady")
            )
            if init and reason:
                label = "Init:" + label
            causes.append(
                {
                    "pod": name,
                    "container": str(container.get("name", "")),
                    "init": init,
                    "reason": label,
                    "exit_code": exit_code,
                    "restarts": restarts,
                    "message": str(
                        waiting.get("message") or terminated.get("message") or ""
                    ),
                    "fatal": fatal,
                    "_waiting": reason,
                    "_ended": bool(running_terminated),
                }
            )
    if final and not causes:
        for condition in status.get("conditions") or ():
            if (
                isinstance(condition, Mapping)
                and condition.get("type") == "PodScheduled"
                and condition.get("status") == "False"
            ):
                causes.append(
                    {
                        "pod": name,
                        "container": "",
                        "init": False,
                        "reason": str(condition.get("reason") or "Unschedulable"),
                        "exit_code": None,
                        "restarts": 0,
                        "message": str(condition.get("message") or ""),
                        "fatal": False,
                        "_waiting": None,
                        "_ended": False,
                    }
                )
    return causes


def _event_time(event: Mapping[str, Any]) -> str:
    return str(
        event.get("lastTimestamp")
        or event.get("eventTime")
        or event.get("firstTimestamp")
        or (event.get("metadata") or {}).get("creationTimestamp")
        or ""
    )


def _events(
    reader: PodReader, pod: str, deadline: float | None, known: Sequence[str]
) -> list[dict[str, Any]]:
    try:
        events = reader.pod_events(pod, deadline=deadline)
    except Exception:  # advisory: an unreadable event list is left out
        return []
    latest = sorted(events, key=_event_time)[-EVENTS:]
    return [
        {
            "type": str(event.get("type") or ""),
            "reason": str(event.get("reason") or ""),
            "message": redact(str(event.get("message") or ""), known),
            "count": _int(event.get("count")) or 1,
        }
        for event in latest
    ]


def _logs(
    reader: PodReader,
    cause: Mapping[str, Any],
    deadline: float | None,
    known: Sequence[str],
) -> list[str]:
    if not cause["container"] or cause["_waiting"] in _NO_LOGS:
        return []
    # The instance that crashed: the current one when it has ended, else
    # (waiting to restart) the previous one.
    if cause["_ended"] or not cause["restarts"]:
        attempts: tuple[bool, ...] = (False, True) if cause["restarts"] else (False,)
    else:
        attempts = (True, False)
    for previous in attempts:
        try:
            text = reader.pod_log(
                cause["pod"],
                cause["container"],
                previous=previous,
                tail_lines=LOG_LINES,
                limit_bytes=LOG_BYTES,
                deadline=deadline,
            )
        except Exception:  # no previous instance, no permission: try the next
            continue
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) == 1 and lines[0].startswith(_NO_LOG_TEXT):
            continue  # the kubelet no longer has that instance's log
        if lines:
            return [redact(line, known) for line in lines[-LOG_LINES:]]
    return []


def diagnose_workload(
    reader: PodReader,
    manifest: Mapping[str, Any],
    *,
    crash_restarts: int,
    known: Callable[[], Sequence[str]],
    deadline: float | None,
    final: bool = False,
) -> dict[str, Any] | None:
    """Causes for one workload, or ``None`` when there are none (or unknown).

    ``final`` (the readiness deadline passed) also reports causes that are
    not fatal: a waiting reason, restarts, an unschedulable pod. ``known``
    returns the secret values to redact; it is called only when there is a
    cause to report. The result's ``fatal`` says whether to fail at once.
    """
    kind = str(manifest.get("kind", ""))
    name = str((manifest.get("metadata") or {}).get("name", ""))
    if kind not in WORKLOAD_KINDS:
        return None
    pods = current_pods(reader, manifest, deadline)
    if pods is None:
        return None
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for pod in sorted(
        pods, key=lambda item: str((item.get("metadata") or {}).get("name", ""))
    ):
        for cause in _container_causes(pod, crash_restarts, final, kind):
            key = (cause["container"], cause["reason"])
            if key in seen:
                continue  # one pod per container and reason is enough
            seen.add(key)
            found.append(cause)
    job_failed = kind == "Job" and any(
        isinstance(item, Mapping)
        and item.get("type") == "Failed"
        and item.get("status") == "True"
        for item in (manifest.get("status") or {}).get("conditions") or ()
    )
    fatal = job_failed or any(item["fatal"] for item in found)
    if not found and not job_failed:
        return None
    if not fatal and not final:
        return None
    found.sort(key=lambda item: not item["fatal"])
    secrets = tuple(known())
    causes = []
    for cause in found[:CAUSES]:
        causes.append(
            {
                "pod": cause["pod"],
                "container": cause["container"],
                "init": cause["init"],
                "reason": cause["reason"],
                "exit_code": cause["exit_code"],
                "restarts": cause["restarts"],
                "message": redact(cause["message"], secrets),
                "logs": _logs(reader, cause, deadline, secrets),
                "events": _events(reader, cause["pod"], deadline, secrets)
                if cause["pod"]
                else [],
            }
        )
    if job_failed and not causes:
        condition = next(
            item
            for item in (manifest.get("status") or {}).get("conditions") or ()
            if isinstance(item, Mapping) and item.get("type") == "Failed"
        )
        causes.append(
            {
                "pod": "",
                "container": "",
                "init": False,
                "reason": str(condition.get("reason") or "Failed"),
                "exit_code": None,
                "restarts": 0,
                "message": redact(str(condition.get("message") or ""), secrets),
                "logs": [],
                "events": [],
            }
        )
    return {"kind": kind, "name": name, "fatal": fatal, "causes": causes}


def document(code: str, workloads: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The ``piceli.diagnosis.v1`` object kept in the journal and the result."""
    return {"schema": SCHEMA, "code": code, "workloads": [dict(w) for w in workloads]}


# ---------------------------------------------------------------------- human


def last_line(cause: Mapping[str, Any]) -> str:
    """The most telling text of a cause: last log line, else message or event."""
    logs = cause.get("logs") or ()
    if logs:
        return str(logs[-1])
    if cause.get("message"):
        return str(cause["message"])
    events = [item for item in cause.get("events") or () if item.get("message")]
    warnings = [item for item in events if item.get("type") == "Warning"]
    chosen = (warnings or events)[-1:] if events else []
    return str(chosen[0]["message"]) if chosen else ""


def human_lines(diagnosis: Mapping[str, Any] | None) -> list[str]:
    """One line per cause: ``<workload> <container> exit <code> "<last line>"``."""
    if not diagnosis:
        return []
    rows = []
    for workload in diagnosis.get("workloads") or ():
        for cause in workload.get("causes") or ():
            code = cause.get("exit_code")
            outcome = f"exit {code}" if code is not None else str(cause.get("reason"))
            rows.append(
                (
                    str(workload.get("name", "")),
                    str(cause.get("container") or "-"),
                    outcome,
                    last_line(cause).replace('"', "'"),
                )
            )
    if not rows:
        return []
    widths = [max(len(row[index]) for row in rows) for index in range(3)]
    return [
        f"  {name:<{widths[0]}}  {container:<{widths[1]}}  {outcome:<{widths[2]}}"
        + (f'  "{text}"' if text else "")
        for name, container, outcome, text in rows
    ]


def headline(diagnosis: Mapping[str, Any] | None) -> str | None:
    """``3 workloads not starting`` (``None`` without causes)."""
    if not diagnosis:
        return None
    count = sum(1 for item in diagnosis.get("workloads") or () if item.get("causes"))
    if not count:
        return None
    noun = "workload" if count == 1 else "workloads"
    if diagnosis.get("code") != "apply-crashloop":
        return f"{count} {noun} not ready"
    return f"{count} {noun} not starting"


def compact(diagnosis: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Causes without logs, events or messages: safe for public run records."""
    if not diagnosis:
        return []
    return [
        {
            "kind": workload.get("kind"),
            "name": workload.get("name"),
            "container": cause.get("container"),
            "reason": cause.get("reason"),
            "exit_code": cause.get("exit_code"),
            "restarts": cause.get("restarts"),
        }
        for workload in diagnosis.get("workloads") or ()
        for cause in workload.get("causes") or ()
    ]
