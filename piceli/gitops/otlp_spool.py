"""Keep OTLP the receiver did not take, and send it when it is back (0.17.0).

A controller that deploys its own telemetry receiver (an app that observes
the cluster it runs in) stops that receiver during the deploy: every span
and event of the deploy was dropped exactly when it mattered. A
:class:`SpoolingExporter` wraps the span or log exporter: a batch the
receiver refuses is encoded as its OTLP protobuf request and written to the
controller's state volume (one file per batch, at most
:data:`SPOOL_MAX_BYTES` per signal, the oldest dropped first); after the
next successful export, and on the heartbeat, the spooled requests are sent
again oldest first, with their original timestamps. Metrics are not spooled:
they are periodic cumulative readings, and the next one carries the totals.

Nothing here raises into the controller: a spool that cannot be written or
read drops the batch, as before.

Importing this module is side-effect free.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

#: Bytes of OTLP requests kept per signal while the receiver is away.
SPOOL_MAX_BYTES = 64 * 1024 * 1024
#: Seconds between two resend attempts while the receiver stays away.
RETRY_SECONDS = 30.0
#: Requests sent per resend (the rest wait for the next one).
DRAIN_BATCH = 64


class Spool:
    """OTLP requests on disk, oldest first (``<ns>-<seq>.pb``, owner-only)."""

    def __init__(self, directory: Path, max_bytes: int = SPOOL_MAX_BYTES) -> None:
        self.directory = directory
        self.max_bytes = max_bytes
        self._seq = 0
        self._lock = threading.Lock()

    def items(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return sorted(self.directory.glob("*.pb"))

    def put(self, data: bytes) -> None:
        """Keep one request; drop the oldest when over :attr:`max_bytes`."""
        if len(data) > self.max_bytes:
            return
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._seq += 1
            name = f"{time.time_ns():020d}-{self._seq:06d}.pb"
            partial = self.directory / f".{name}.partial"
            descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
            os.replace(partial, self.directory / name)
            found = self.items()
            sizes = {path: path.stat().st_size for path in found}
            total = sum(sizes.values())
            for path in found:
                if total <= self.max_bytes:
                    break
                total -= sizes[path]
                path.unlink(missing_ok=True)

    def drain(self, send: Callable[[bytes], bool], limit: int = DRAIN_BATCH) -> int:
        """Send kept requests oldest first until one fails; how many were sent."""
        sent = 0
        with self._lock:
            for path in self.items()[:limit]:
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                if not send(data):
                    break
                path.unlink(missing_ok=True)
                sent += 1
        return sent


def _succeeded(result: Any) -> bool:
    return getattr(result, "name", None) == "SUCCESS"


class SpoolingExporter:
    """A span or log exporter that spools what the receiver refused.

    :param inner: The OTLP exporter.
    :param encode: A batch as its serialized OTLP request.
    :param send: Sends one serialized request; ``True`` when it was taken.
    """

    def __init__(
        self,
        inner: Any,
        *,
        encode: Callable[[Sequence[Any]], bytes],
        send: Callable[[bytes], bool],
        spool: Spool,
        retry_seconds: float = RETRY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.inner = inner
        self.encode = encode
        self.send = send
        self.spool = spool
        self.retry_seconds = retry_seconds
        self.clock = clock
        self._next_try = 0.0

    def export(self, batch: Sequence[Any]) -> Any:
        result = self.inner.export(batch)
        if _succeeded(result):
            self.resend(force=True)
            return result
        try:
            self.spool.put(self.encode(batch))
        except Exception:  # never into the controller: dropped, as before
            pass
        self._next_try = self.clock() + self.retry_seconds
        return result

    def resend(self, *, force: bool = False) -> int:
        """Send what was spooled (at most every ``retry_seconds`` unless forced)."""
        if not force and self.clock() < self._next_try:
            return 0
        try:
            sent = self.spool.drain(self._safe_send)
        except Exception:
            return 0
        if self.spool.items():
            self._next_try = self.clock() + self.retry_seconds
        return sent

    def _safe_send(self, data: bytes) -> bool:
        try:
            return bool(self.send(data))
        except Exception:
            return False

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        flush = getattr(self.inner, "force_flush", None)
        return bool(flush(timeout_millis)) if callable(flush) else True


def http_sender(exporter: Any) -> Callable[[bytes], bool]:
    """Send a serialized request with an OTLP/HTTP exporter's session and headers."""

    def send(data: bytes) -> bool:
        response = exporter._export(data, float(exporter._timeout))
        return bool(response.ok)

    return send


def grpc_sender(exporter: Any, request: Any) -> Callable[[bytes], bool]:
    """Send a serialized request over an OTLP/gRPC exporter's channel."""

    def send(data: bytes) -> bool:
        if exporter._client is None:
            return False
        exporter._client.Export(
            request=request.FromString(data),
            metadata=exporter._headers,
            timeout=float(exporter._timeout),
        )
        return True

    return send


def spooling(
    span: Any, log: Any, protocol: str, directory: Path
) -> tuple[SpoolingExporter, SpoolingExporter]:
    """The span and log exporters of ``protocol`` wrapped with spools in ``directory``."""
    from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

    if protocol == "grpc":
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
            ExportLogsServiceRequest,
        )
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        send_spans = grpc_sender(span, ExportTraceServiceRequest)
        send_logs = grpc_sender(log, ExportLogsServiceRequest)
    else:
        send_spans, send_logs = http_sender(span), http_sender(log)
    return (
        SpoolingExporter(
            span,
            encode=lambda batch: encode_spans(batch).SerializePartialToString(),
            send=send_spans,
            spool=Spool(directory / "traces"),
        ),
        SpoolingExporter(
            log,
            encode=lambda batch: encode_logs(batch).SerializeToString(),
            send=send_logs,
            spool=Spool(directory / "logs"),
        ),
    )
