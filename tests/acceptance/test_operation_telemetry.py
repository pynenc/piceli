"""Bounded real HTTP export, with actual deployment executor hooks."""
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from piceli.telemetry import OperationTelemetry, OtlpOptions
from tests.acceptance.fake_api import manifest
from tests.acceptance.test_local_executor import executor, mutations, prepare


@contextmanager
def receiver(status=200, body=b"", delay=0):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, raw))
            time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_success_parenting_and_fixed_secret_safe_fields():
    secret = "not-a-task-payload-or-exception"
    with receiver() as (url, requests):
        emitter = OperationTelemetry(OtlpOptions(url, "private-token"))
        with emitter.operation("plan", "outer"):
            with emitter.operation("build", "inner"):
                pass
        with pytest.raises(ValueError), emitter.operation("apply", "failed"):
            raise ValueError(secret)
        assert emitter.flush(2)
        stats = emitter.shutdown(2)
        assert stats["drained"] and stats["pending"] == 0
        assert stats["signals"]["traces"]["acknowledged"] == 3
        assert stats["signals"]["logs"]["acknowledged"] == 3
        assert secret.encode() not in b"".join(raw for _, raw in requests)
        spans = []
        for path, raw in requests:
            if path == "/v1/traces":
                request = ExportTraceServiceRequest.FromString(raw)
                spans.extend(request.resource_spans[0].scope_spans[0].spans)
        inner = next(span for span in spans if span.name == "piceli.build")
        outer = next(span for span in spans if span.name == "piceli.plan")
        assert (
            inner.parent_span_id == outer.span_id and inner.trace_id == outer.trace_id
        )
        with emitter.operation("cancel", "late"):
            pass
        assert emitter.stats()["rejected_after_shutdown"] == 1


@pytest.mark.parametrize(
    "status,body,field",
    [
        (401, b"secret-body", "rejected"),
        (503, b"secret-body", "unknown"),
        (200, b"malformed", "unknown"),
        (200, b"x" * 1025, "unknown"),
    ],
)
def test_rejection_malformed_unavailable_and_oversized_accounting(status, body, field):
    with receiver(status, body) as (url, _):
        emitter = OperationTelemetry(
            OtlpOptions(url, "private", max_response_bytes=1024)
        )
        with emitter.operation("apply", "adverse"):
            pass
        stats = emitter.shutdown(2)
        assert stats["drained"]
        for signal in stats["signals"].values():
            assert signal["attempted"] == 1
            assert signal[field] == 1 and signal["acknowledged"] == 0


def test_partial_rejection_no_retry():
    response = ExportTraceServiceResponse()
    response.partial_success.rejected_spans = 1
    with receiver(body=response.SerializeToString()) as (url, requests):
        emitter = OperationTelemetry(OtlpOptions(url, "private"))
        with emitter.operation("apply", "partial"):
            pass
        stats = emitter.shutdown(2)
        assert stats["signals"]["traces"]["rejected"] == 1
        assert len(requests) == 2


def test_compensation_retention_state_is_exported_verbatim():
    with receiver() as (url, requests):
        emitter = OperationTelemetry(OtlpOptions(url, "private"))
        with emitter.operation("compensate", "retained") as operation:
            operation.state = "compensated-with-retention"
        assert emitter.shutdown(2)["drained"]
        log = (
            next(
                ExportLogsServiceRequest.FromString(raw)
                for path, raw in requests
                if path == "/v1/logs"
            )
            .resource_logs[0]
            .scope_logs[0]
            .log_records[0]
        )
        attributes = {item.key: item.value.string_value for item in log.attributes}
        assert attributes["piceli.operation.state"] == "compensated-with-retention"


def test_queue_flush_and_shutdown_budgets_account_for_drops():
    with receiver(delay=0.3) as (url, _):
        emitter = OperationTelemetry(
            OtlpOptions(url, "private", capacity=1, request_seconds=0.1)
        )
        for i in range(30):
            with emitter.operation("apply", f"op-{i}"):
                pass
        started = time.monotonic()
        assert not emitter.flush(0.001)
        initial = emitter.shutdown(0.005)
        assert time.monotonic() - started < 0.1
        assert initial["dropped"] > 0
        # A timed-out shutdown explicitly reports in-flight work, never a zero-loss success.
        if not initial["drained"]:
            assert initial["pending"] > 0
        final = emitter.shutdown(1)
        assert final["drained"] and final["pending"] == 0
        for signal in final["signals"].values():
            assert (
                signal["attempted"]
                == signal["acknowledged"] + signal["rejected"] + signal["unknown"]
            )
            assert signal["attempted"] + signal["not_sent"] == 30


def test_actual_executor_emits_blocked_then_resumed_without_reapplying(
    local_api, tmp_path
):
    api, provider = local_api
    with receiver() as (url, requests):
        telemetry = OperationTelemetry(OtlpOptions(url, "private"))
        run = executor(provider, tmp_path, telemetry=telemetry)
        plan, snapshot, grant = prepare(provider, [manifest()])
        api.inject("POST", "/configmaps", disconnect_after=True, dry_run=False)
        assert run.run("lost", plan, snapshot, grant)["state"] == "blocked"
        assert run.run("lost", plan, snapshot, grant, resume=True)["state"] == "ready"
        assert len(mutations(api)) == 1
        assert telemetry.shutdown(2)["drained"]
        states = []
        for path, raw in requests:
            if path == "/v1/logs":
                log = (
                    ExportLogsServiceRequest.FromString(raw)
                    .resource_logs[0]
                    .scope_logs[0]
                    .log_records[0]
                )
                states.append({a.key: a.value.string_value for a in log.attributes})
        assert [
            (row["piceli.operation.phase"], row["piceli.operation.state"])
            for row in states
        ] == [("apply", "blocked"), ("resume", "ready")]
