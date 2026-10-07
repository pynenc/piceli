"""OTLP the receiver refused is spooled and sent when it is back (0.17.0)."""

from __future__ import annotations

import http.server
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("opentelemetry.exporter.otlp.proto.http")

from piceli.gitops import otel
from piceli.gitops.otlp_spool import Spool


class Receiver(http.server.BaseHTTPRequestHandler):
    bodies: list[tuple[str, bytes]] = []

    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length") or 0)
        type(self).bodies.append((self.path, self.rfile.read(size)))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, *_: Any) -> None:
        pass


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def receiver_port() -> Iterator[int]:
    Receiver.bodies = []
    yield _free_port()


def _start(port: int) -> http.server.HTTPServer:
    server = http.server.HTTPServer(("127.0.0.1", port), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_spans_refused_while_the_receiver_is_down_are_sent_when_it_is_back(
    tmp_path: Path, receiver_port: int
) -> None:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    settings = otel.resolve(
        {
            "endpoint": f"http://127.0.0.1:{receiver_port}",
            "protocol": "http/protobuf",
            "insecure": True,
            "timeout_seconds": 1,
        },
        {},
    )
    assert settings is not None
    spool_dir = tmp_path / "spool"
    providers = otel.otlp_providers(settings, {}, spool_dir=spool_dir)
    span_exporter = providers.spools[0]
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(span_exporter))
    try:
        tracer.get_tracer("t").start_span("deploy while the receiver is down").end()
        assert len(list((spool_dir / "traces").glob("*.pb"))) == 1
        server = _start(receiver_port)
        try:
            tracer.get_tracer("t").start_span("after it is back").end()
        finally:
            server.shutdown()
            server.server_close()
    finally:
        tracer.shutdown()
        providers.shutdown(1)
    names = []
    for path, body in Receiver.bodies:
        assert path == "/v1/traces"
        request = ExportTraceServiceRequest.FromString(body)
        names += [
            span.name
            for resource in request.resource_spans
            for scope in resource.scope_spans
            for span in scope.spans
        ]
    # The new batch first, then the spooled one (with its own timestamps).
    assert names == ["after it is back", "deploy while the receiver is down"]
    assert list((spool_dir / "traces").glob("*.pb")) == []


def test_the_spool_is_bounded_and_drops_the_oldest(tmp_path: Path) -> None:
    spool = Spool(tmp_path / "s", max_bytes=25)
    for index in range(4):
        spool.put(bytes([index]) * 10)
    kept = [path.read_bytes() for path in spool.items()]
    assert kept == [bytes([2]) * 10, bytes([3]) * 10]
    sent: list[bytes] = []
    assert spool.drain(lambda data: sent.append(data) or len(sent) < 2) == 1
    assert sent == kept and len(spool.items()) == 1


def test_resends_wait_while_the_receiver_stays_away(tmp_path: Path) -> None:
    from piceli.gitops.otlp_spool import SpoolingExporter

    class Down:
        def export(self, batch: Any) -> Any:
            return type("R", (), {"name": "FAILURE"})()

    now = {"t": 0.0}
    tries: list[bytes] = []
    exporter = SpoolingExporter(
        Down(),
        encode=lambda batch: b"x",
        send=lambda data: tries.append(data) is not None,
        spool=Spool(tmp_path / "s"),
        retry_seconds=30,
        clock=lambda: now["t"],
    )
    exporter.export([object()])
    assert exporter.resend() == 0 and tries == []  # within 30 s: not tried
    now["t"] = 31
    assert exporter.resend() == 0 and tries == [b"x"]  # tried, still down
    now["t"] = 40
    assert exporter.resend() == 0 and tries == [b"x"]


def test_the_controller_prints_each_line_with_its_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import re

    from piceli import cli_contract

    try:
        cli_contract.stamp_lines()
        cli_contract.say("main: push infinite-haiku -> abc\nsecond line")
    finally:
        cli_contract.stamp_lines(False)
    cli_contract.say("plain again")
    lines = capsys.readouterr().err.splitlines()
    stamp = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ "
    assert re.fullmatch(stamp + "main: push infinite-haiku -> abc", lines[0])
    assert re.fullmatch(stamp + "second line", lines[1])
    assert lines[2] == "plain again"
