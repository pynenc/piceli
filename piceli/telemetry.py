"""Bounded OTLP operation spans/logs; no credentials, manifests or exception bodies.

Optional dependencies load only when an exporter is explicitly constructed.
Projection is piceli.operation-otel.v1 over generic OpenTelemetry spans/logs, not the
Rustvello task-attempt mapping. Transport identity supplies tenancy.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import ipaddress
from queue import Empty, Full, Queue
import secrets
import threading
import time
from typing import Any, Iterator
from urllib.parse import urlsplit

from piceli.bounds import bounded_call, positive, seconds, text

_EXPORT_SLOTS = threading.BoundedSemaphore(2)
PHASES = frozenset(
    {
        "plan",
        "build",
        "inspect",
        "import",
        "run",
        "apply",
        "resume",
        "compensate",
        "cancel",
    }
)
STATES = frozenset(
    {
        "succeeded",
        "ready",
        "failed",
        "blocked",
        "cancelled",
        "compensated",
        "compensated-with-retention",
        "compensation-blocked",
        "rejected",
        "timed-out",
        "output-limit",
    }
)


@dataclass
class Operation:
    state: str = "succeeded"


class NoopTelemetry:
    @contextmanager
    def operation(
        self,
        phase: str,
        operation_id: str,
        *,
        plan_hash: str = "",
        action_count: int = 0,
    ) -> Iterator[Operation]:
        yield Operation()


@dataclass(frozen=True)
class OtlpOptions:
    endpoint: str = field(repr=False)
    token: str = field(repr=False)
    capacity: int = 256
    request_seconds: float = 1.0
    max_response_bytes: int = 65_536

    def __post_init__(self) -> None:
        endpoint = urlsplit(self.endpoint)
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("explicit OTLP endpoint required")
        if endpoint.scheme == "http":
            try:
                local = (
                    endpoint.hostname == "localhost"
                    or ipaddress.ip_address(endpoint.hostname).is_loopback
                )
            except ValueError:
                local = False
            if not local:
                raise ValueError("remote OTLP requires authenticated HTTPS")
        text(self.token, "private OTLP token")
        positive(self.capacity, "OTLP queue", 4096)
        positive(self.max_response_bytes, "OTLP response", 1_048_576)
        seconds(self.request_seconds, "OTLP request", 10)


@dataclass(frozen=True)
class _Record:
    phase: str
    operation_id: str
    plan_hash: str
    action_count: int
    state: str
    start: int
    end: int
    trace_id: int
    span_id: int
    parent_span_id: int
    sampled: bool
    tracestate: str


class OperationTelemetry(NoopTelemetry):
    def __init__(self, options: OtlpOptions) -> None:
        from opentelemetry import trace
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        self.options = options
        self._trace = trace
        self._proto_available = ExportTraceServiceRequest
        self._queue: Queue[_Record] = Queue(maxsize=options.capacity)
        self._condition = threading.Condition()
        self._closing = False
        self._close_deadline = float("inf")
        self._accepted = self._processed = self._dropped = self._after_shutdown = 0
        self._signals = {
            signal: {
                key: 0
                for key in (
                    "attempted",
                    "acknowledged",
                    "rejected",
                    "unknown",
                    "not_sent",
                )
            }
            for signal in ("traces", "logs")
        }
        self._thread = threading.Thread(
            target=self._run, name="piceli-otlp", daemon=True
        )
        self._thread.start()

    @contextmanager
    def operation(
        self,
        phase: str,
        operation_id: str,
        *,
        plan_hash: str = "",
        action_count: int = 0,
    ) -> Iterator[Operation]:
        if phase not in PHASES:
            raise ValueError("unsupported operation phase")
        text(operation_id, "public operation ID")
        if (
            len(operation_id) > 128
            or len(plan_hash) > 128
            or not isinstance(action_count, int)
            or isinstance(action_count, bool)
            or not 0 <= action_count <= 4096
        ):
            raise ValueError("operation attributes exceed bounds")
        if plan_hash:
            import re

            if not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", plan_hash):
                raise ValueError("invalid public plan hash")
        trace = self._trace
        parent = trace.get_current_span().get_span_context()
        trace_id = parent.trace_id if parent.is_valid else secrets.randbits(128) or 1
        sampled = (
            bool(parent.trace_flags & trace.TraceFlags.SAMPLED)
            if parent.is_valid
            else True
        )
        context = trace.SpanContext(
            trace_id,
            secrets.randbits(64) or 1,
            False,
            trace.TraceFlags(trace.TraceFlags.SAMPLED if sampled else 0),
            parent.trace_state,
        )
        operation = Operation()
        start = time.time_ns()
        try:
            with trace.use_span(
                trace.NonRecordingSpan(context),
                end_on_exit=False,
                record_exception=False,
                set_status_on_exception=False,
            ):
                yield operation
        except BaseException:
            operation.state = "failed"
            raise
        finally:
            state = operation.state if operation.state in STATES else "failed"
            self._enqueue(
                _Record(
                    phase,
                    operation_id,
                    plan_hash,
                    action_count,
                    state,
                    start,
                    time.time_ns(),
                    trace_id,
                    context.span_id,
                    parent.span_id if parent.is_valid else 0,
                    sampled,
                    context.trace_state.to_header(),
                )
            )

    def _enqueue(self, record: _Record) -> None:
        with self._condition:
            if self._closing:
                self._after_shutdown += 1
            else:
                try:
                    self._queue.put_nowait(record)
                    self._accepted += 1
                    return
                except Full:
                    self._dropped += 1
            self._signals["logs"]["not_sent"] += 1
            self._signals["traces"]["not_sent"] += int(record.sampled)

    def stats(self) -> dict[str, Any]:
        with self._condition:
            return {
                "accepted": self._accepted,
                "processed": self._processed,
                "dropped": self._dropped,
                "rejected_after_shutdown": self._after_shutdown,
                "pending": self._accepted - self._processed,
                "closed": self._closing,
                "signals": {
                    kind: dict(values) for kind, values in self._signals.items()
                },
            }

    def flush(self, timeout: float = 5.0) -> bool:
        seconds(timeout, "OTLP flush", 60)
        deadline = time.monotonic() + timeout
        with self._condition:
            target = self._accepted
            while self._processed < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True

    def shutdown(self, timeout: float = 5.0) -> dict[str, Any]:
        seconds(timeout, "OTLP shutdown", 60)
        deadline = time.monotonic() + timeout
        with self._condition:
            self._closing = True
            self._close_deadline = min(self._close_deadline, deadline)
        self._thread.join(max(0, deadline - time.monotonic()))
        return self.stats() | {"drained": not self._thread.is_alive()}

    def _wire(self, record: _Record) -> list[tuple[str, bytes]]:
        from opentelemetry.proto.common.v1.common_pb2 import (
            AnyValue,
            KeyValue,
            InstrumentationScope,
        )
        from opentelemetry.proto.resource.v1.resource_pb2 import Resource
        from opentelemetry.proto.trace.v1.trace_pb2 import (
            ResourceSpans,
            ScopeSpans,
            Span,
            Status,
        )
        from opentelemetry.proto.logs.v1.logs_pb2 import (
            ResourceLogs,
            ScopeLogs,
            LogRecord,
            SEVERITY_NUMBER_ERROR,
            SEVERITY_NUMBER_INFO,
        )
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
            ExportLogsServiceRequest,
        )

        def attr(key: str, value: str | int) -> Any:
            return KeyValue(
                key=key,
                value=AnyValue(int_value=value)
                if isinstance(value, int)
                else AnyValue(string_value=value),
            )

        resource = Resource(
            attributes=[
                attr("service.name", "piceli"),
                attr("service.namespace", "piceli"),
                attr("telemetry.sdk.language", "python"),
            ]
        )
        scope = InstrumentationScope(name="piceli.operation", version="1")
        attrs = [
            attr("piceli.mapping.revision", "piceli.operation-otel.v1"),
            attr("piceli.operation.id", record.operation_id),
            attr("piceli.operation.phase", record.phase),
            attr("piceli.operation.state", record.state),
            attr("piceli.action.count", record.action_count),
        ]
        if record.plan_hash:
            attrs.append(attr("piceli.plan.hash", record.plan_hash))
        trace_id = record.trace_id.to_bytes(16, "big")
        span_id = record.span_id.to_bytes(8, "big")
        success = record.state in {"succeeded", "ready", "compensated"}
        result = []
        if record.sampled:
            span = Span(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=record.parent_span_id.to_bytes(8, "big")
                if record.parent_span_id
                else b"",
                trace_state=record.tracestate,
                name=f"piceli.{record.phase}",
                kind=Span.SPAN_KIND_INTERNAL,
                start_time_unix_nano=record.start,
                end_time_unix_nano=record.end,
                attributes=attrs,
                status=Status(
                    code=Status.STATUS_CODE_OK if success else Status.STATUS_CODE_ERROR
                ),
            )
            result.append(
                (
                    "traces",
                    ExportTraceServiceRequest(
                        resource_spans=[
                            ResourceSpans(
                                resource=resource,
                                scope_spans=[ScopeSpans(scope=scope, spans=[span])],
                            )
                        ]
                    ).SerializeToString(),
                )
            )
        log = LogRecord(
            time_unix_nano=record.end,
            observed_time_unix_nano=record.end,
            trace_id=trace_id,
            span_id=span_id,
            flags=int(record.sampled),
            severity_number=(
                SEVERITY_NUMBER_INFO if success else SEVERITY_NUMBER_ERROR
            ),
            severity_text="INFO" if success else "ERROR",
            body=AnyValue(string_value=f"piceli.{record.phase}.{record.state}"),
            attributes=attrs,
        )
        result.append(
            (
                "logs",
                ExportLogsServiceRequest(
                    resource_logs=[
                        ResourceLogs(
                            resource=resource,
                            scope_logs=[ScopeLogs(scope=scope, log_records=[log])],
                        )
                    ]
                ).SerializeToString(),
            )
        )
        return result

    def _post(self, signal: str, body: bytes, deadline: float) -> str:
        from urllib3 import PoolManager, Timeout
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceResponse,
        )
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
            ExportLogsServiceResponse,
        )

        def send() -> str:
            response = None
            pool = PoolManager()
            try:
                remaining = max(0.001, deadline - time.monotonic())
                response = pool.request(
                    "POST",
                    self.options.endpoint.rstrip("/") + "/v1/" + signal,
                    body=body,
                    headers={
                        "Content-Type": "application/x-protobuf",
                        "Authorization": "Bearer " + self.options.token,
                    },
                    timeout=Timeout(total=remaining, connect=remaining, read=remaining),
                    retries=False,
                    redirect=False,
                    preload_content=False,
                )
                if response.status in {400, 401, 403, 404, 413, 422}:
                    return "rejected"
                if response.status != 200:
                    return "unknown"
                raw = response.read(
                    self.options.max_response_bytes + 1, decode_content=False
                )
                if len(raw) > self.options.max_response_bytes:
                    return "unknown"
                if signal == "traces":
                    trace_response = ExportTraceServiceResponse()
                    trace_response.ParseFromString(raw)
                    rejected = trace_response.partial_success.rejected_spans
                else:
                    logs_response = ExportLogsServiceResponse()
                    logs_response.ParseFromString(raw)
                    rejected = logs_response.partial_success.rejected_log_records
                return (
                    "acknowledged"
                    if rejected == 0
                    else "rejected"
                    if rejected == 1
                    else "unknown"
                )
            finally:
                if response is not None:
                    response.close()
                pool.clear()

        try:
            return bounded_call(send, deadline, slots=_EXPORT_SLOTS)
        except Exception:
            return "unknown"

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._closing and self._queue.empty():
                    return
            try:
                record = self._queue.get(timeout=0.02)
            except Empty:
                continue
            try:
                messages = self._wire(record)
                deadline = min(
                    time.monotonic() + self.options.request_seconds,
                    self._close_deadline,
                )
                for signal, body in messages:
                    with self._condition:
                        deadline = min(deadline, self._close_deadline)
                        if time.monotonic() >= deadline or len(body) > 4 * 1024 * 1024:
                            self._signals[signal]["not_sent"] += 1
                            continue
                        self._signals[signal]["attempted"] += 1
                    outcome = self._post(signal, body, deadline)
                    with self._condition:
                        self._signals[signal][outcome] += 1
            except Exception:
                with self._condition:
                    self._signals["logs"]["not_sent"] += 1
                    self._signals["traces"]["not_sent"] += int(record.sampled)
            finally:
                with self._condition:
                    self._processed += 1
                    self._condition.notify_all()
