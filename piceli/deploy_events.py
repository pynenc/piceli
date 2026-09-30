"""Deploy events exported to an OTLP endpoint (``piceli.deploy-otel.v1``).

``piceli deploy`` and ``piceli release apply|rollback`` can send one trace per
run (a root span with one child span per stage) and one log record with the
run result, over OTLP/HTTP with the JSON encoding. No SDK is needed.

Rules, all covered by tests:

- Off unless configured: ``--otlp-endpoint`` or the standard
  ``OTEL_EXPORTER_OTLP[_TRACES|_LOGS]_ENDPOINT``.
- Never blocks or fails a run: one bounded export at the end, in a daemon
  thread; any problem becomes one warning line with a fixed code.
- Never exports secrets: attributes are a fixed allow-list of bounded scalars
  (names, hashes, commits, digests, fixed error codes). Manifests, messages,
  exception text and kubeconfig paths never enter. Headers are read from the
  environment only and never printed.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

SCHEMA = "piceli.deploy-otel.v1"
DEFAULT_TIMEOUT = 3.0
MAX_TIMEOUT = 10.0
_LIMIT = 256
_TERMINAL = frozenset({"ready", "failed", "rolled-back", "interrupted", "rejected"})
_OK = frozenset({"ready", "planned", "stopped", "approval-required", "succeeded"})
_TOKEN = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")


class DeployEventsConfigError(ValueError):
    """The OTLP configuration is unusable (never carries the offending value)."""


@dataclass(frozen=True)
class OtlpConfig:
    traces_url: str = field(repr=False)
    logs_url: str = field(repr=False)
    headers: Mapping[str, str] = field(repr=False, default_factory=dict)
    service_name: str = "piceli"
    resource: Mapping[str, str] = field(default_factory=dict)
    timeout: float = DEFAULT_TIMEOUT


def _url(value: str, signal: str | None) -> str:
    parts = urlsplit(value.strip())
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise DeployEventsConfigError("endpoint")
    if signal is None:
        return value.strip()
    return value.strip().rstrip("/") + f"/v1/{signal}"


def _pairs(raw: str, *, header: bool) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in filter(None, (part.strip() for part in raw.split(","))):
        key, sep, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not _TOKEN.fullmatch(key) or len(key) > 128:
            raise DeployEventsConfigError("headers" if header else "resource")
        if any(ord(c) < 32 or ord(c) == 127 for c in value) or len(value) > 4096:
            raise DeployEventsConfigError("headers" if header else "resource")
        result[key] = value
    return result


def resolve(
    endpoint: str | None, environ: Mapping[str, str] | None = None
) -> OtlpConfig | None:
    """The export configuration, or ``None`` when nothing asks for one.

    Raises :class:`DeployEventsConfigError` for a configuration that asks for
    export but cannot work (the caller warns and carries on).
    """
    env = os.environ if environ is None else environ
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return None
    generic = endpoint or env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    traces = (
        None
        if endpoint
        else env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip() or None
    )
    logs = (
        None
        if endpoint
        else env.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "").strip() or None
    )
    if not (generic or traces or logs):
        return None
    protocol = env.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf").strip()
    if protocol not in {"http/protobuf", "http/json", ""}:
        raise DeployEventsConfigError("protocol")
    if not generic and not (traces and logs):
        generic = ""
    traces_url = _url(traces, None) if traces else None
    logs_url = _url(logs, None) if logs else None
    if generic:
        traces_url = traces_url or _url(generic, "traces")
        logs_url = logs_url or _url(generic, "logs")
    if not traces_url or not logs_url:
        raise DeployEventsConfigError("endpoint")
    timeout = DEFAULT_TIMEOUT
    raw_timeout = env.get("OTEL_EXPORTER_OTLP_TIMEOUT", "").strip()
    if raw_timeout:
        try:
            timeout = min(MAX_TIMEOUT, max(0.1, int(raw_timeout) / 1000))
        except ValueError:
            raise DeployEventsConfigError("timeout") from None
    name = env.get("OTEL_SERVICE_NAME", "").strip() or "piceli"
    if len(name) > _LIMIT:
        raise DeployEventsConfigError("service")
    return OtlpConfig(
        traces_url,
        logs_url,
        _pairs(env.get("OTEL_EXPORTER_OTLP_HEADERS", ""), header=True),
        name,
        _pairs(env.get("OTEL_RESOURCE_ATTRIBUTES", ""), header=False),
        timeout,
    )


def _ns(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(
            datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1e9
        )
    except ValueError:
        return None


def _value(value: Any) -> dict[str, Any] | None:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value[:_LIMIT]}
    if isinstance(value, (list, tuple)):
        return {
            "arrayValue": {
                "values": [
                    {"stringValue": str(item)[:_LIMIT]} for item in list(value)[:64]
                ]
            }
        }
    return None


def _attrs(values: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = []
    for key, value in values.items():
        wire = None if value is None or value == "" else _value(value)
        if wire is not None:
            result.append({"key": key, "value": wire})
    return result


def _post(
    url: str, body: dict[str, Any], headers: Mapping[str, str], deadline: float
) -> None:
    from urllib3 import PoolManager, Timeout

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    pool = PoolManager()
    try:
        response = pool.request(
            "POST",
            url,
            body=json.dumps(body).encode(),
            headers={**headers, "Content-Type": "application/json"},
            timeout=Timeout(total=remaining, connect=remaining, read=remaining),
            retries=False,
            redirect=False,
        )
        if not 200 <= response.status < 300:
            raise OSError("status")
    finally:
        pool.clear()


def warn(say: Callable[[str], None], *, code: str) -> None:
    """The one human warning line (a fixed code, never a value)."""
    say(f"warning: deploy events not exported ({code}); the deploy is unaffected")


class DeployRecorder:
    """Collects one run's stage events and exports them once, at the end.

    :param kind: ``deploy`` or ``release`` (the root span is ``piceli.<kind>``).
    :param say: Prints the single human warning line (stderr).
    """

    def __init__(
        self,
        config: OtlpConfig,
        *,
        kind: str = "deploy",
        say: Callable[[str], None] = lambda _m: None,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.kind = kind
        self.say = say
        self.attributes: dict[str, Any] = dict(attributes or {})
        self.started = time.time_ns()
        self._running: dict[str, int] = {}
        self._stages: list[dict[str, Any]] = []
        self._commits: dict[str, str] = {}
        self._refs: dict[str, str] = {}
        self._dirty = False
        self.exported = False

    # ---------------------------------------------------------------- input
    def stage_event(self, event: Mapping[str, Any]) -> None:
        """Observe one ``piceli.deploy-event.v1`` stage event (never raises)."""
        try:
            self._stage_event(event)
        except Exception:
            pass

    def _stage_event(self, event: Mapping[str, Any]) -> None:
        if event.get("event") != "stage":
            return
        stage, state = str(event.get("stage")), str(event.get("state"))
        if run_id := event.get("run_id"):
            self.attributes.setdefault("piceli.run.id", str(run_id))
        at = _ns(event.get("at")) or time.time_ns()
        detail = event.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        if stage == "inputs" and state in {"done", "skipped"}:
            self._inputs(detail)
        if state == "running":
            self._running[stage] = at
            return
        if state == "planned":
            return
        item: dict[str, Any] = {
            "stage": stage,
            "state": state,
            "start": self._running.pop(stage, at),
            "end": at,
        }
        reason = detail.get("reason") if isinstance(detail.get("reason"), str) else None
        if reason:
            item["reason"] = reason
        why = detail.get("why")
        if isinstance(why, str) and len(why) <= 64:
            item["why"] = why
        self._stages.append(item)

    def add_stage(
        self, stage: str, state: str, start: int, end: int, reason: str | None = None
    ) -> None:
        """Record a finished stage directly (runs without stage events)."""
        item: dict[str, Any] = {
            "stage": stage,
            "state": state,
            "start": start,
            "end": end,
        }
        if reason:
            item["reason"] = reason
        self._stages.append(item)

    def _inputs(self, detail: Mapping[str, Any]) -> None:
        for name, value in (detail.get("refs") or {}).items():
            if isinstance(value, dict):
                self._commits[str(name)] = str(value.get("commit") or "")
                self._refs[str(name)] = str(value.get("ref") or "")
        for name, value in (detail.get("sources") or {}).items():
            if isinstance(value, dict):
                if value.get("commit"):
                    self._commits.setdefault(str(name), str(value["commit"]))
                self._dirty = self._dirty or bool(value.get("dirty"))

    # --------------------------------------------------------------- output
    def finish(
        self,
        result: Mapping[str, Any],
        *,
        rollback: Mapping[str, Any] | None = None,
    ) -> bool:
        """Export the run; ``True`` when both requests were accepted.

        Never raises and never waits longer than the configured timeout.
        """
        if self.exported:
            return False
        self.exported = True
        try:
            traces, logs = self._build(result, rollback)
        except Exception:
            self._warn(code="deploy-events-export-failed")
            return False
        failure: list[str] = []

        def send() -> None:
            deadline = time.monotonic() + self.config.timeout
            try:
                _post(self.config.traces_url, traces, self.config.headers, deadline)
                if self._log_wanted(result):
                    _post(self.config.logs_url, logs, self.config.headers, deadline)
            except Exception:
                failure.append("failed")

        worker = threading.Thread(target=send, name="piceli-deploy-events", daemon=True)
        worker.start()
        worker.join(self.config.timeout + 0.5)
        if worker.is_alive() or failure:
            self._warn(code="deploy-events-export-failed")
            return False
        return True

    def _warn(self, *, code: str) -> None:
        warn(self.say, code=code)

    @staticmethod
    def _log_wanted(result: Mapping[str, Any]) -> bool:
        return str(result.get("state")) in _TERMINAL

    def _build(
        self, result: Mapping[str, Any], rollback: Mapping[str, Any] | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        end = time.time_ns()
        state = str(result.get("state", "failed"))
        trace_id, root_id = secrets.token_hex(16), secrets.token_hex(8)
        images = result.get("images")
        digests = (
            [f"{name}={digest}" for name, digest in sorted(images.items())]
            if isinstance(images, dict)
            else []
        )
        commits = [f"{n}={c}" for n, c in sorted(self._commits.items()) if c]
        refs = [f"{n}={r}" for n, r in sorted(self._refs.items()) if r]
        reason = result.get("reason")
        root_attrs: dict[str, Any] = {
            **self.attributes,
            "piceli.schema": SCHEMA,
            "piceli.outcome": state,
            "piceli.run.id": result.get("run_id")
            or self.attributes.get("piceli.run.id"),
            "piceli.release.id": result.get("release"),
            "piceli.plan.hash": result.get("combined_hash")
            or self.attributes.get("piceli.plan.hash"),
            "piceli.commit": commits[0].partition("=")[2]
            if len(commits) == 1
            else None,
            "piceli.commits": commits or None,
            "piceli.ref": refs or None,
            "piceli.source.dirty": True if self._dirty else None,
            "piceli.image.digests": digests or None,
            "piceli.reason": reason if isinstance(reason, str) else None,
            "piceli.duration_ms": (end - self.started) // 1_000_000,
        }
        failed = state not in _OK
        spans: list[dict[str, Any]] = [
            {
                "traceId": trace_id,
                "spanId": root_id,
                "name": f"piceli.{self.kind}",
                "kind": 1,
                "startTimeUnixNano": str(self.started),
                "endTimeUnixNano": str(end),
                "attributes": _attrs(root_attrs),
                "status": {"code": 2 if failed else 1},
            }
        ]
        for item in self._stages:
            bad = item["state"] in {"failed", "rejected", "interrupted"}
            spans.append(
                {
                    "traceId": trace_id,
                    "spanId": secrets.token_hex(8),
                    "parentSpanId": root_id,
                    "name": f"piceli.{self.kind}.{item['stage']}",
                    "kind": 1,
                    "startTimeUnixNano": str(item["start"]),
                    "endTimeUnixNano": str(max(item["start"], item["end"])),
                    "attributes": _attrs(
                        {
                            "piceli.stage": item["stage"],
                            "piceli.stage.state": item["state"],
                            "piceli.stage.duration_ms": max(
                                0, item["end"] - item["start"]
                            )
                            // 1_000_000,
                            "piceli.reason": item.get("reason"),
                            "piceli.stage.why": item.get("why"),
                        }
                    ),
                    "status": {"code": 2 if bad else 1},
                }
            )
        if rollback:
            target = rollback.get("target")
            spans.append(
                {
                    "traceId": trace_id,
                    "spanId": secrets.token_hex(8),
                    "parentSpanId": root_id,
                    "name": f"piceli.{self.kind}.rollback",
                    "kind": 1,
                    "startTimeUnixNano": str(end),
                    "endTimeUnixNano": str(end),
                    "attributes": _attrs(
                        {
                            "piceli.stage": "rollback",
                            "piceli.stage.state": rollback.get("state"),
                            "piceli.rollback.target": target
                            if isinstance(target, str)
                            else None,
                            "piceli.reason": rollback.get("reason")
                            if isinstance(rollback.get("reason"), str)
                            else None,
                        }
                    ),
                    "status": {"code": 1 if rollback.get("state") == "ready" else 2},
                }
            )
        resource = {
            "attributes": _attrs(
                {
                    **self.config.resource,
                    "service.name": self.config.service_name,
                    "telemetry.sdk.language": "python",
                    "telemetry.sdk.name": "piceli",
                }
            )
        }
        scope = {"name": "piceli.deploy", "version": "1"}
        traces = {
            "resourceSpans": [
                {"resource": resource, "scopeSpans": [{"scope": scope, "spans": spans}]}
            ]
        }
        log_attrs = {
            **root_attrs,
            "event.name": f"piceli.{self.kind}",
            "piceli.deploy.stages": [
                f"{i['stage']}={i['state']}" for i in self._stages
            ],
        }
        record = {
            "timeUnixNano": str(end),
            "observedTimeUnixNano": str(end),
            "severityNumber": 17 if failed else 9,
            "severityText": "ERROR" if failed else "INFO",
            "body": {"stringValue": f"piceli.{self.kind}.{state}"},
            "attributes": _attrs(log_attrs),
            "traceId": trace_id,
            "spanId": root_id,
            "flags": 1,
        }
        logs = {
            "resourceLogs": [
                {
                    "resource": resource,
                    "scopeLogs": [{"scope": scope, "logRecords": [record]}],
                }
            ]
        }
        return traces, logs


def recorder(
    endpoint: str | None,
    *,
    kind: str,
    say: Callable[[str], None],
    attributes: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> DeployRecorder | None:
    """A recorder when export is configured, else ``None``; a bad configuration
    only warns (``deploy-events-config-invalid``)."""
    try:
        config = resolve(endpoint, environ)
    except DeployEventsConfigError:
        warn(say, code="deploy-events-config-invalid")
        return None
    if config is None:
        return None
    return DeployRecorder(config, kind=kind, say=say, attributes=attributes)
