"""OpenTelemetry from the GitOps controller: traces, events and metrics.

The composition controller (``piceli gitops run``) sends, over OTLP/gRPC or
OTLP/HTTP (protobuf), with the official OpenTelemetry SDK (the ``telemetry``
extra, installed in the controller image):

- **one trace per pipeline run** (an environment's deploy of one revision):
  a root span with CI/CD semantics (``SYNC <composition>/<env>``) and one
  child per stage (builds per image, plan, approval wait, restore point,
  pre-rollout, apply with prune, checks with one child per check,
  rollback). Times come from the run journal's stage records and the
  controller's own hook timestamps, never invented;
- **events**: log records with ``event.name`` (``piceli.deploy.*``,
  ``piceli.environment.*``, ``piceli.controller.started``,
  ``piceli.build.failed``), each with a stable ``piceli.event.id`` and the
  run's trace and span;
- **metrics**: the CI/CD semantic-convention metrics
  (``cicd.pipeline.run.duration``, ``.active``, ``.errors``,
  ``cicd.worker.count``) and Piceli's (build durations, check results,
  approval and queue waits, time since the last successful deploy, and a
  heartbeat a background thread keeps ticking during long builds).

The semantic conventions followed are :data:`SEMCONV_VERSION`. Spans of a
run are produced when the run ends, from the run record kept in the state
volume (``telemetry/runs.json``): a controller that dies inside a run closes
it as ``interrupted`` at its next start, in the same trace (the trace and
span ids are derived from the cluster, environment and run), and sends the
run's events again (a backend deduplicates them by ``piceli.event.id``).

Telemetry never blocks or fails a deploy: every hook swallows its own
errors, spans, events and metrics go to bounded in-memory queues exported by
the SDK's background threads with bounded timeouts, and an unreachable
endpoint only drops data. Header values and CA files are read from mounted
Secret files, never printed and never put in an attribute.

Importing this module is side-effect free: the SDK is imported only when an
exporter is built.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

#: The OpenTelemetry semantic conventions version the names follow.
SEMCONV_VERSION = "1.43.0"
SCHEMA_URL = f"https://opentelemetry.io/schemas/{SEMCONV_VERSION}"
SERVICE_NAME = "piceli-controller"
SCOPE = "piceli.gitops"
#: OTLP protocols the controller speaks.
PROTOCOLS = ("grpc", "http/protobuf")
#: Where the controller's Deployment mounts the headers and CA Secrets.
HEADERS_DIR = "/var/run/piceli-gitops/otlp-headers"
CA_DIR = "/var/run/piceli-gitops/otlp-ca"
STORE_SCHEMA = "piceli.controller-otel-state.v1"
#: Seconds one export may take (each signal), and the shutdown flush.
EXPORT_TIMEOUT = 5.0
#: Spans, log records kept in memory while the endpoint is unreachable.
MAX_QUEUE = 2048
#: Events remembered per open run (resent when a restart closes it).
MAX_RUN_EVENTS = 40
#: Build log lines a ``piceli.build.failed`` event carries.
BUILD_TAIL_LINES = 20
#: Seconds between heartbeats.
HEARTBEAT_SECONDS = 15.0

_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_HEADER = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}")
_LIMIT = 256

#: Results of a run (``piceli.deploy.result``) and their CI/CD result.
RESULTS = {
    "success": "success",
    "failure": "failure",
    "rolled-back": "failure",
    "interrupted": "error",
    "superseded": "cancellation",
}
#: Journal stages that become spans: (span name, task type).
STAGES = {
    "prerollout": ("pre-rollout", "test"),
    "backup": ("restore point", "deploy"),
    "plan": ("plan", "deploy"),
    "apply": ("apply", "deploy"),
    "checks": ("checks", "test"),
}
_STAGE_RESULT = {
    "done": "success",
    "skipped": "skip",
    "failed": "failure",
    "rejected": "failure",
    "interrupted": "error",
}


class OtlpConfigError(ValueError):
    """An unusable OTLP setting (the message never carries a value)."""


# ---------------------------------------------------------------- settings


def check_otlp(
    endpoint: Any,
    protocol: Any,
    headers_secret: Any,
    ca_secret: Any,
    insecure: Any,
) -> None:
    """Refuse an unusable ``Otlp(...)`` (:class:`OtlpConfigError`)."""
    if not isinstance(endpoint, str):
        raise OtlpConfigError("Otlp(endpoint=) must be an http:// or https:// URL")
    parts = urlsplit(endpoint)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise OtlpConfigError(
            "Otlp(endpoint=) must be an http:// or https:// URL without "
            "credentials, query or fragment"
        )
    if protocol not in PROTOCOLS:
        raise OtlpConfigError('Otlp(protocol=) must be "grpc" or "http/protobuf"')
    if not isinstance(insecure, bool):
        raise OtlpConfigError("Otlp(insecure=) must be True or False")
    if parts.scheme == "http" and not insecure and not _loopback(parts.hostname):
        raise OtlpConfigError(
            "Otlp(endpoint=) is plain http: use https, or say insecure=True"
        )
    if parts.scheme == "https" and insecure:
        raise OtlpConfigError("Otlp(insecure=True) needs an http:// endpoint")
    for name, value in (("headers_secret", headers_secret), ("ca_secret", ca_secret)):
        if value is not None and (
            not isinstance(value, str) or not _LABEL.fullmatch(value)
        ):
            raise OtlpConfigError(f"Otlp({name}=) must name a Secret (a DNS label)")
    if ca_secret is not None and insecure:
        raise OtlpConfigError("Otlp(ca_secret=) needs TLS (an https:// endpoint)")


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Settings:
    """Where and how the controller exports (resolved at start)."""

    endpoint: str | None
    protocol: str = "grpc"
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    ca_file: str | None = None
    insecure: bool = False
    timeout: float = EXPORT_TIMEOUT
    #: ``True``: the standard ``OTEL_EXPORTER_OTLP_*`` variables configure
    #: the exporters (no composition setting).
    from_environment: bool = False


def _read_headers(directory: Path) -> dict[str, str]:
    """Header name → value from a mounted Secret (one file per header)."""
    found: dict[str, str] = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue  # the projected volume's ..data links
        if not _HEADER.fullmatch(path.name):
            raise OtlpConfigError("a key of the OTLP headers Secret is no header name")
        value = path.read_text().strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in value) or len(value) > 8192:
            raise OtlpConfigError("a value of the OTLP headers Secret is invalid")
        found[path.name.lower()] = value
    return found


def _ca_file(directory: Path) -> str | None:
    if not directory.is_dir():
        return None
    preferred = directory / "ca.crt"
    if preferred.is_file():
        return str(preferred)
    files = sorted(
        p for p in directory.iterdir() if p.is_file() and not p.name.startswith(".")
    )
    return str(files[0]) if len(files) == 1 else None


def resolve(
    telemetry: Mapping[str, Any] | None,
    environ: Mapping[str, str] | None = None,
    *,
    headers_dir: Path = Path(HEADERS_DIR),
    ca_dir: Path = Path(CA_DIR),
) -> Settings | None:
    """The export settings, or ``None`` when nothing asks for export.

    ``telemetry`` is the composition's ``Controller(telemetry=Otlp(...))``
    (``CompositionConfig.telemetry``); without it the standard
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` (or a per-signal endpoint) turns export
    on and the SDK reads the other ``OTEL_EXPORTER_OTLP_*`` variables.
    ``OTEL_SDK_DISABLED=true`` turns it off.
    """
    env = os.environ if environ is None else environ
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return None
    if telemetry:
        check_otlp(
            telemetry.get("endpoint"),
            telemetry.get("protocol", "grpc"),
            telemetry.get("headers_secret"),
            telemetry.get("ca_secret"),
            telemetry.get("insecure", False),
        )
        headers = _read_headers(headers_dir) if telemetry.get("headers_secret") else {}
        ca = _ca_file(ca_dir) if telemetry.get("ca_secret") else None
        if telemetry.get("ca_secret") and ca is None:
            raise OtlpConfigError("the OTLP CA Secret holds no ca.crt")
        return Settings(
            endpoint=str(telemetry["endpoint"]),
            protocol=str(telemetry.get("protocol", "grpc")),
            headers=headers,
            ca_file=ca,
            insecure=bool(telemetry.get("insecure", False)),
        )
    keys = (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    )
    if not any(env.get(key, "").strip() for key in keys):
        return None
    protocol = env.get("OTEL_EXPORTER_OTLP_PROTOCOL", "").strip() or "http/protobuf"
    if protocol not in PROTOCOLS:
        raise OtlpConfigError(
            "OTEL_EXPORTER_OTLP_PROTOCOL must be grpc or http/protobuf"
        )
    return Settings(endpoint=None, protocol=protocol, from_environment=True)


# ---------------------------------------------------------------- providers


@dataclass
class Providers:
    """The SDK providers of the three signals (built once per process)."""

    tracer_provider: Any
    logger_provider: Any
    meter_provider: Any
    ids: Any

    def shutdown(self, timeout: float = EXPORT_TIMEOUT) -> bool:
        """Flush and stop, waiting at most ``timeout`` seconds in all."""

        def stop() -> None:
            for provider in (
                self.tracer_provider,
                self.logger_provider,
                self.meter_provider,
            ):
                try:
                    provider.shutdown()
                except Exception:
                    pass

        worker = threading.Thread(target=stop, name="piceli-otel-stop", daemon=True)
        worker.start()
        worker.join(timeout)
        return not worker.is_alive()


def _id_generator() -> Any:
    """An SDK ``IdGenerator`` that hands out preset ids when asked (else random)."""
    from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

    class PresetIds(RandomIdGenerator):
        def __init__(self) -> None:
            self.local = threading.local()

        def generate_span_id(self) -> int:
            preset = getattr(self.local, "span", None)
            if preset:
                self.local.span = None
                return int(preset)
            return int(super().generate_span_id())

        def generate_trace_id(self) -> int:
            preset = getattr(self.local, "trace", None)
            if preset:
                self.local.trace = None
                return int(preset)
            return int(super().generate_trace_id())

    return PresetIds()


def resource_attributes(
    *,
    version: str,
    cluster_uid: str | None = None,
    cluster_name: str | None = None,
    namespace: str | None = None,
) -> dict[str, Any]:
    """The controller's resource (``service.name=piceli-controller``)."""
    found: dict[str, Any] = {
        "service.name": SERVICE_NAME,
        "service.version": version,
        "service.namespace": "piceli",
    }
    instance = os.environ.get("HOSTNAME", "").strip()
    if instance and len(instance) <= 253:
        found["service.instance.id"] = instance
    if cluster_uid:
        found["k8s.cluster.uid"] = cluster_uid
    if cluster_name:
        found["k8s.cluster.name"] = cluster_name
    if namespace:
        found["piceli.controller.namespace"] = namespace
    return found


def build_providers(
    resource: Mapping[str, Any],
    *,
    span_exporter: Any,
    log_exporter: Any,
    metric_reader: Any,
    batch: bool = True,
    schedule_delay: float = 2.0,
    timeout: float = EXPORT_TIMEOUT,
    max_queue: int = MAX_QUEUE,
) -> Providers:
    """Providers over the given exporters (batching, bounded queues)."""
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import (
        BatchLogRecordProcessor,
        SimpleLogRecordProcessor,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    found = Resource.create(dict(resource), schema_url=SCHEMA_URL)
    ids = _id_generator()
    tracer_provider = TracerProvider(resource=found, id_generator=ids)
    logger_provider = LoggerProvider(resource=found)
    if batch:
        delay, wait = int(schedule_delay * 1000), int(timeout * 1000)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                span_exporter,
                max_queue_size=max_queue,
                max_export_batch_size=min(512, max_queue),
                schedule_delay_millis=delay,
                export_timeout_millis=wait,
            )
        )
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                log_exporter,
                max_queue_size=max_queue,
                max_export_batch_size=min(512, max_queue),
                schedule_delay_millis=delay,
                export_timeout_millis=wait,
            )
        )
    else:  # tests: every span and record is exported when it ends
        tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    meter_provider = MeterProvider(resource=found, metric_readers=[metric_reader])
    return Providers(tracer_provider, logger_provider, meter_provider, ids)


def otlp_providers(settings: Settings, resource: Mapping[str, Any]) -> Providers:
    """Providers exporting over OTLP as ``settings`` say."""
    import logging

    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    # A collector that is down logs one error per failed batch, not a
    # warning per retry (the controller's log stays readable).
    logging.getLogger("opentelemetry.exporter.otlp").setLevel(logging.ERROR)
    span, log, metric = _exporters(settings)
    reader = PeriodicExportingMetricReader(
        metric,
        export_interval_millis=30_000,
        export_timeout_millis=int(settings.timeout * 1000),
    )
    return build_providers(
        resource,
        span_exporter=span,
        log_exporter=log,
        metric_reader=reader,
        timeout=settings.timeout,
    )


def _exporters(settings: Settings) -> tuple[Any, Any, Any]:
    if settings.protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter as GrpcLogs,
        )
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter as GrpcMetrics,
        )
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as GrpcSpans,
        )

        if settings.from_environment:
            return GrpcSpans(), GrpcLogs(), GrpcMetrics()
        options: dict[str, Any] = {
            "endpoint": settings.endpoint,
            "insecure": settings.insecure,
            "headers": dict(settings.headers) or None,
            "timeout": settings.timeout,
        }
        if settings.ca_file is not None:
            import grpc

            options["credentials"] = grpc.ssl_channel_credentials(
                root_certificates=Path(settings.ca_file).read_bytes()
            )
        return GrpcSpans(**options), GrpcLogs(**options), GrpcMetrics(**options)
    from opentelemetry.exporter.otlp.proto.http._log_exporter import (
        OTLPLogExporter as HttpLogs,
    )
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter as HttpMetrics,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter as HttpSpans,
    )

    if settings.from_environment:
        return HttpSpans(), HttpLogs(), HttpMetrics()
    base = str(settings.endpoint).rstrip("/")

    def per_signal(signal: str) -> dict[str, Any]:
        return {
            "endpoint": f"{base}/v1/{signal}",
            "headers": dict(settings.headers) or None,
            "timeout": settings.timeout,
            **({"certificate_file": settings.ca_file} if settings.ca_file else {}),
        }

    return (
        HttpSpans(**per_signal("traces")),
        HttpLogs(**per_signal("logs")),
        HttpMetrics(**per_signal("metrics")),
    )


# ---------------------------------------------------------------- helpers


def _ns(seconds: float) -> int:
    return int(seconds * 1_000_000_000)


def _iso_ns(value: Any) -> int | None:
    """An ISO timestamp as Unix nanoseconds (``None``: missing or unreadable)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        found = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if found.tzinfo is None:
        found = found.replace(tzinfo=UTC)
    return int(found.timestamp() * 1_000_000) * 1000


def _stamp(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, UTC).strftime("%Y%m%dT%H%M%S%fZ")[:-4] + "Z"


def trace_id_of(cluster: str, env: str, run: str) -> int:
    """The run's trace id: derived, so a restart continues the same trace."""
    digest = hashlib.sha256(f"piceli:{cluster}:{env}/{run}".encode()).digest()
    return int.from_bytes(digest[:16], "big") or 1


def span_id_of(trace_id: int, path: str) -> int:
    """A span's id: derived from its trace and its place in the run."""
    digest = hashlib.sha256(f"{trace_id:032x}:{path}".encode()).digest()
    return int.from_bytes(digest[:8], "big") or 1


def _text(value: Any, limit: int = _LIMIT) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


#: Attributes allowed longer than :data:`_LIMIT` characters.
_LONG = {"piceli.build.log_tail": 4000, "piceli.check.detail": 300}


def _attrs(values: Mapping[str, Any]) -> dict[str, Any]:
    """Drop empty values; bound strings and lists (attribute limits)."""
    found: dict[str, Any] = {}
    for key, value in values.items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, str):
            found[key] = value[-_LONG[key] :] if key in _LONG else value[:_LIMIT]
        elif isinstance(value, bool | int | float):
            found[key] = value
        elif isinstance(value, list | tuple):
            limit = 320 if key == "piceli.checks.detail" else _LIMIT
            found[key] = [str(item)[:limit] for item in list(value)[:64]]
    return found


def trigger_kind(trigger: Any) -> str | None:
    """``push``, ``tag``, ``promote``, ``sync``, ``start`` … of a record's trigger."""
    if not isinstance(trigger, str) or not trigger:
        return None
    return trigger.split(" ", 1)[0][:32]


def _ref_name(ref: Any) -> tuple[str | None, str | None]:
    if not isinstance(ref, str):
        return None, None
    for prefix, kind in (("refs/heads/", "branch"), ("refs/tags/", "tag")):
        if ref.startswith(prefix):
            return ref[len(prefix) :], kind
    return ref, None


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def tail_lines(text: Any, lines: int = BUILD_TAIL_LINES) -> str | None:
    """The last ``lines`` lines of a (redacted) build log tail."""
    if not isinstance(text, str) or not text:
        return None
    return "\n".join(text.rstrip("\n").splitlines()[-lines:])[-4000:]


# ---------------------------------------------------------------- the store


class Store:
    """The open runs and the last heartbeat, in the controller's state volume."""

    def __init__(self, state_dir: Path) -> None:
        self.directory = state_dir / "telemetry"
        self.path = self.directory / "runs.json"
        self.heartbeat_path = self.directory / "heartbeat"
        self.lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        found: Any
        try:
            found = json.loads(self.path.read_text())
        except (OSError, ValueError):
            found = None
        data: dict[str, Any] = {"schema": STORE_SCHEMA}
        if isinstance(found, dict) and found.get("schema") == STORE_SCHEMA:
            data = found
        data.setdefault("runs", {})
        data.setdefault("last_success", {})
        return data

    def save(self, data: Mapping[str, Any]) -> None:
        from piceli.gitops.state import write_json

        with self.lock:
            write_json(self.path, data)

    def beat(self, ns: int) -> None:
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = self.heartbeat_path.with_suffix(".tmp")
            temporary.write_text(str(ns))
            os.replace(temporary, self.heartbeat_path)

    def last_beat(self) -> int | None:
        try:
            return int(self.heartbeat_path.read_text().strip())
        except (OSError, ValueError):
            return None


# ---------------------------------------------------------------- telemetry


@dataclass
class _Instruments:
    run_duration: Any
    run_active: Any
    run_errors: Any
    workers: Any
    build_duration: Any
    checks: Any
    approval_wait: Any
    queue_wait: Any
    heartbeats: Any


class ControllerTelemetry:
    """The controller's OpenTelemetry (a no-op without providers).

    :param state_dir: The controller's state volume (open runs, heartbeat).
    :param providers: :class:`Providers` (``None``: disabled).
    :param cluster: The cluster's name in event ids and ``k8s.cluster.name``.
    :param composition: The composition's name (``cicd.pipeline.name``).
    """

    def __init__(
        self,
        state_dir: Path,
        providers: Providers | None,
        *,
        cluster: str | None = None,
        composition: str | None = None,
        version: str = "unknown",
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] = lambda _m: None,
    ) -> None:
        self.providers = providers
        self.store = Store(state_dir)
        self.cluster = cluster or "cluster"
        self.composition = composition or "composition"
        self.version = version
        self.clock = clock
        self.log = log
        self.controller: Any = None
        self.failures = 0
        self._warned: set[str] = set()
        self._queue_size = 0
        self._last_beat_ns: int | None = None
        self._heartbeat: threading.Thread | None = None
        self._stopping = threading.Event()
        self._data: dict[str, Any] = {}
        self._worker_busy = False
        self.instruments: _Instruments | None = None
        if providers is not None:
            self.tracer = providers.tracer_provider.get_tracer(
                SCOPE, version, schema_url=SCHEMA_URL
            )
            self.logger = providers.logger_provider.get_logger(
                SCOPE, version, schema_url=SCHEMA_URL
            )
            self.meter = providers.meter_provider.get_meter(
                SCOPE, version, schema_url=SCHEMA_URL
            )
            self.instruments = self._instruments()

    @property
    def enabled(self) -> bool:
        return self.providers is not None

    @classmethod
    def disabled(cls, state_dir: Path | None = None) -> ControllerTelemetry:
        return cls(state_dir or Path("."), None)

    # ------------------------------------------------------------ safety
    @contextmanager
    def _guard(self, where: str) -> Iterator[None]:
        """Telemetry never raises into the controller: one log line per kind."""
        try:
            yield
        except Exception as error:
            self.failures += 1
            key = f"{where}:{type(error).__name__}"
            if key not in self._warned:
                self._warned.add(key)
                self.log(f"telemetry: {where} skipped ({type(error).__name__})")

    def _now(self) -> int:
        return _ns(self.clock())

    # ------------------------------------------------------------ instruments
    def _instruments(self) -> _Instruments:
        meter = self.meter
        found = _Instruments(
            run_duration=meter.create_histogram(
                "cicd.pipeline.run.duration",
                unit="s",
                description="Duration of a pipeline run grouped by pipeline, state and result.",
            ),
            run_active=meter.create_up_down_counter(
                "cicd.pipeline.run.active",
                unit="{run}",
                description="The number of pipeline runs currently active per state.",
            ),
            run_errors=meter.create_counter(
                "cicd.pipeline.run.errors",
                unit="{error}",
                description="The number of errors encountered in pipeline runs.",
            ),
            workers=meter.create_up_down_counter(
                "cicd.worker.count",
                unit="{worker}",
                description="The number of workers on the CI/CD system by state.",
            ),
            build_duration=meter.create_histogram(
                "piceli.build.duration",
                unit="s",
                description="Duration of one image's build (or mirror) by result.",
            ),
            checks=meter.create_counter(
                "piceli.check.runs",
                unit="{check}",
                description="Post-deploy check executions by check and result.",
            ),
            approval_wait=meter.create_histogram(
                "piceli.deploy.approval.wait",
                unit="s",
                description="Time a run waited for its approval.",
            ),
            queue_wait=meter.create_histogram(
                "piceli.deploy.queue.wait",
                unit="s",
                description="Time from a wanted revision to the start of its deploy step.",
            ),
            heartbeats=meter.create_counter(
                "piceli.controller.heartbeats",
                unit="{heartbeat}",
                description="Heartbeats of the controller's background thread.",
            ),
        )
        meter.create_observable_gauge(
            "piceli.deploy.last_success.age",
            callbacks=[self._last_success_age],
            unit="s",
            description="Seconds since each environment's last successful deploy.",
        )
        meter.create_observable_gauge(
            "piceli.deploy.queue.size",
            callbacks=[self._queue_observation],
            unit="{run}",
            description="Environments due for a deploy step at the last step.",
        )
        meter.create_observable_gauge(
            "piceli.controller.heartbeat",
            callbacks=[self._heartbeat_observation],
            unit="s",
            description="Unix time of the controller's last heartbeat.",
        )
        return found

    def _last_success_age(self, _options: Any) -> Iterator[Any]:
        from opentelemetry.metrics import Observation

        now = time.time_ns()
        try:  # read from the exporter's thread while the controller writes
            found = sorted(dict(self._data.get("last_success") or {}).items())
        except RuntimeError:
            return
        for env, ns in found:
            if isinstance(ns, int):
                yield Observation(
                    max(0.0, (now - ns) / 1e9),
                    {"deployment.environment.name": env},
                )

    def _queue_observation(self, _options: Any) -> Iterator[Any]:
        from opentelemetry.metrics import Observation

        yield Observation(self._queue_size, {})

    def _heartbeat_observation(self, _options: Any) -> Iterator[Any]:
        from opentelemetry.metrics import Observation

        if self._last_beat_ns is not None:
            yield Observation(self._last_beat_ns / 1e9, {})

    # ------------------------------------------------------------ lifecycle
    def attach(self, controller: Any) -> None:
        """Called once by the controller: close interrupted runs, say it started."""
        if not self.enabled:
            return
        self.controller = controller
        with self._guard("start"):
            self._data = self.store.load()
            assert self.instruments is not None
            self.instruments.workers.add(1, {"cicd.worker.state": "available"})
            now = self._now()
            last_beat = self.store.last_beat()
            for key, run in sorted(dict(self._data["runs"]).items()):
                if run.get("in_step"):
                    end = max(int(run.get("step_started_ns") or now), last_beat or now)
                    self._close(
                        key, run, "interrupted", end, reason="controller-restarted"
                    )
                    continue
                run["active"] = None  # counters are per process
                self._set_active(run, "pending" if run.get("approval") else "executing")
            self.store.save(self._data)
            boot = _stamp(now)
            self._event(
                "piceli.controller.started",
                f"{self.cluster}:controller/{boot}:started",
                now,
                body=f"piceli controller {self.version} started",
                attributes={"piceli.controller.version": self.version},
            )

    def start_heartbeat(self, interval: float = HEARTBEAT_SECONDS) -> None:
        """A background thread that keeps the heartbeat ticking during long steps."""
        if not self.enabled or self._heartbeat is not None:
            return

        def loop() -> None:
            while not self._stopping.is_set():
                self.heartbeat()
                self._stopping.wait(interval)

        self._heartbeat = threading.Thread(
            target=loop, name="piceli-otel-heartbeat", daemon=True
        )
        self._heartbeat.start()

    def heartbeat(self) -> None:
        if not self.enabled:
            return
        with self._guard("heartbeat"):
            now = time.time_ns()
            self._last_beat_ns = now
            self.store.beat(now)
            assert self.instruments is not None
            self.instruments.heartbeats.add(1)

    def shutdown(self, timeout: float = EXPORT_TIMEOUT) -> bool:
        """Stop the heartbeat and flush what is queued (bounded)."""
        self._stopping.set()
        if self._heartbeat is not None:
            self._heartbeat.join(1.0)
        if self.providers is None:
            return True
        return self.providers.shutdown(timeout)

    # ------------------------------------------------------------ hooks
    def queue(self, size: int) -> None:
        self._queue_size = int(size)

    def step_started(self, record: Mapping[str, Any], work: str) -> None:
        """A controller step begins (``deploy``, ``stop``, ``start_owner`` …)."""
        if not self.enabled:
            return
        with self._guard("step start"):
            assert self.instruments is not None
            now = self._now()
            key = str(record.get("branch"))
            runs = self._data["runs"]
            run = runs.get(key)
            if run is not None and work == "deploy":
                same = dict(run.get("revision") or {}) == dict(
                    record.get("revision") or {}
                )
                if not same or run.get("kind") != "deploy":
                    self._close(key, run, "superseded", now, reason="superseded")
                    run = None
            elif run is not None:
                self._close(key, run, "superseded", now, reason="superseded")
                run = None
            if run is None:
                run = self._open(record, work, now)
                runs[key] = run
                self._set_active(run, "executing")
                pushed = _iso_ns(record.get("pushed_at"))
                if work == "deploy" and pushed is not None:
                    self.instruments.queue_wait.record(
                        max(0.0, (now - pushed) / 1e9), self._env_attrs(run)
                    )
            else:  # continued after an approval
                self._set_active(run, "executing")
                approval = run.get("approval")
                if isinstance(approval, dict) and approval.get("end") is None:
                    approval["end"] = now
            run["in_step"] = True
            run["step_started_ns"] = now
            try:
                self._queue_size = len(self.controller._due())
            except Exception:
                pass
            run["step"] = int(run.get("step") or 0) + 1
            run["approval_at_start"] = _dict(record.get("approval")) or None
            if not self._worker_busy:
                self._worker_busy = True
                self.instruments.workers.add(-1, {"cicd.worker.state": "available"})
                self.instruments.workers.add(1, {"cicd.worker.state": "busy"})
            self.store.save(self._data)

    def build_started(self, images: Sequence[str]) -> None:
        """The step starts building (or mirroring) ``images``."""
        if not self.enabled:
            return
        with self._guard("build start"):
            run = self._current()
            if run is None:
                return
            now = self._now()
            builds = run.setdefault("builds", [])
            for name in images:
                if name and name != "mirror":
                    builds.append(
                        {"image": name, "start": now, "step": run.get("step")}
                    )
            self.store.save(self._data)

    def built(self, image: str, digest: str | None) -> None:
        """One image of the step was built (or mirrored)."""
        if not self.enabled or image == "mirror":
            return
        with self._guard("build"):
            run = self._current()
            if run is None:
                return
            now = self._now()
            for entry in reversed(run.get("builds") or []):
                if entry.get("image") == image and entry.get("end") is None:
                    entry.update(end=now, digest=digest, result="success")
                    break
            self.store.save(self._data)

    def approved(self, record: Mapping[str, Any], via: Any) -> None:
        """The owner approved the plan a run waits for (``cli`` or ``ui``)."""
        if not self.enabled:
            return
        with self._guard("approval"):
            run = self._data["runs"].get(str(record.get("branch")))
            if run is None:
                return
            now = self._now()
            approval = run.setdefault("approval", {"start": now})
            if approval.get("end") is None:
                approval["end"] = now
            approval["via"] = via if via in ("cli", "ui") else "request"
            self._run_event(
                run,
                "piceli.deploy.approved",
                "approved",
                now,
                span_path="approval",
                body=f"{run['env']}: plan approved ({approval['via']})",
                attributes={
                    "piceli.approval.via": approval["via"],
                    "piceli.plan.hash": _text(record.get("approved_hash"), 80)
                    or _text(approval.get("plan_hash"), 80),
                },
            )
            self.store.save(self._data)

    def step_finished(
        self, record: Mapping[str, Any], work: str, outcome: Any = None
    ) -> None:
        """A controller step ended: close its run, or keep it open for an approval."""
        if not self.enabled:
            return
        with self._guard("step end"):
            assert self.instruments is not None
            key = str(record.get("branch"))
            run = self._data["runs"].get(key)
            if self._worker_busy:
                self._worker_busy = False
                self.instruments.workers.add(-1, {"cicd.worker.state": "busy"})
                self.instruments.workers.add(1, {"cicd.worker.state": "available"})
            if run is None:
                return
            now = self._now()
            run["in_step"] = False
            run["step_ended_ns"] = now
            if work != "deploy":
                self._finish_operation(key, run, record, work, now)
                return
            journal = self._journal(record, outcome)
            if journal is not None:
                run.setdefault("journals", []).append(
                    {"step": run.get("step"), "data": _journal_view(journal)}
                )
            state = str(record.get("state") or "")
            self._step_events(run, record, journal, now)
            if state == "approval-required":
                run["approval"] = {
                    "start": now,
                    "plan_hash": _text(record.get("plan_hash"), 80),
                }
                self._set_active(run, "pending")
                self._run_event(
                    run,
                    "piceli.deploy.approval.required",
                    f"approval-required:{run.get('step')}",
                    now,
                    span_path="approval",
                    body=f"{run['env']}: approval required",
                    attributes={"piceli.plan.hash": _text(record.get("plan_hash"), 80)},
                )
                self.store.save(self._data)
                return
            if state in {"pending", "held"} and not record.get("reason"):
                self.store.save(self._data)  # put off (an unreachable cluster)
                return
            result = self._result(record, journal)
            self._close(
                key,
                run,
                result,
                now,
                reason=None if result == "success" else _text(record.get("reason"), 64),
                record=record,
            )
            self.store.save(self._data)

    # ------------------------------------------------------------ runs
    def _open(self, record: Mapping[str, Any], work: str, now: int) -> dict[str, Any]:
        env = str(record.get("group") or record.get("branch"))
        cluster = record.get("cluster")
        where = cluster if isinstance(cluster, str) and cluster else self.cluster
        run_id = _stamp(now)
        kind = "deploy" if work == "deploy" else work
        trace = trace_id_of(where, str(record.get("branch")), run_id)
        return {
            "run": run_id,
            "kind": kind,
            "env": env,
            "record": str(record.get("branch")),
            "cluster": where,
            "placement": cluster if isinstance(cluster, str) else None,
            "trace_id": f"{trace:032x}",
            "started_ns": now,
            "revision": dict(record.get("revision") or {}),
            "refs": dict(record.get("refs") or {}),
            "commit": _text(record.get("commit"), 64),
            "trigger": _text(record.get("trigger")),
            "attempt": int(record.get("attempts") or 0) + 1,
            "namespace": _text(record.get("namespace"), 63),
            "events": [],
        }

    def _current(self) -> dict[str, Any] | None:
        for run in self._data.get("runs", {}).values():
            if isinstance(run, dict) and run.get("in_step"):
                return run
        return None

    def _set_active(self, run: dict[str, Any], state: str | None) -> None:
        """Move the run between ``cicd.pipeline.run.active`` states."""
        assert self.instruments is not None
        before = run.get("active")
        if before == state:
            return
        if before:
            self.instruments.run_active.add(-1, self._active_attrs(run, before))
        if state:
            self.instruments.run_active.add(1, self._active_attrs(run, state))
        run["active"] = state

    def _event_id(self, run: Mapping[str, Any], what: str) -> str:
        return f"{run['cluster']}:{run['record']}/{run['run']}:{what}"

    def _pipeline(self, run: Mapping[str, Any]) -> str:
        return f"{self.composition}/{run['env']}"

    def _env_attrs(self, run: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "cicd.pipeline.name": self._pipeline(run),
            "deployment.environment.name": run["env"],
        }

    def _active_attrs(self, run: Mapping[str, Any], state: str) -> dict[str, Any]:
        return {**self._env_attrs(run), "cicd.pipeline.run.state": state}

    def _common(self, run: Mapping[str, Any]) -> dict[str, Any]:
        return _attrs(
            {
                **self._env_attrs(run),
                "cicd.pipeline.run.id": f"{run['record']}/{run['run']}",
                "k8s.namespace.name": run.get("namespace"),
                "piceli.cluster": run.get("placement"),
            }
        )

    def _span_context(self, run: Mapping[str, Any], path: str) -> Any:
        from opentelemetry import trace
        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

        trace_id = int(run["trace_id"], 16)
        context = SpanContext(
            trace_id,
            span_id_of(trace_id, path),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        return trace.set_span_in_context(NonRecordingSpan(context))

    def _event(
        self,
        name: str,
        event_id: str,
        at: int,
        *,
        body: str,
        attributes: Mapping[str, Any],
        context: Any = None,
        error: bool = False,
    ) -> None:
        from opentelemetry._logs import SeverityNumber

        self.logger.emit(
            timestamp=at,
            observed_timestamp=time.time_ns(),
            context=context,
            severity_number=SeverityNumber.ERROR if error else SeverityNumber.INFO,
            severity_text="ERROR" if error else "INFO",
            body=body[:_LIMIT],
            attributes=_attrs({**attributes, "piceli.event.id": event_id}),
            event_name=name,
        )

    def _run_event(
        self,
        run: dict[str, Any],
        name: str,
        what: str,
        at: int,
        *,
        span_path: str = "root",
        body: str,
        attributes: Mapping[str, Any],
        error: bool = False,
    ) -> None:
        values = {**self._common(run), **attributes}
        event_id = self._event_id(run, what)
        self._event(
            name,
            event_id,
            at,
            body=body,
            attributes=values,
            context=self._span_context(run, span_path),
            error=error,
        )
        events = run.setdefault("events", [])
        events.append(
            {
                "name": name,
                "what": what,
                "at": at,
                "path": span_path,
                "body": body[:_LIMIT],
                "attributes": _attrs(values),
                "error": error,
            }
        )
        del events[:-MAX_RUN_EVENTS]

    def _journal(
        self, record: Mapping[str, Any], outcome: Any
    ) -> dict[str, Any] | None:
        """The raw run journal of this step (``None``: the step made none)."""
        controller = self.controller
        if controller is None:
            return None
        try:
            directories = controller._run_dirs(record)
        except Exception:
            return None
        run_id = getattr(outcome, "run_id", None)
        started = (
            self._data["runs"].get(str(record.get("branch")), {}).get("step_started_ns")
        )
        newest: tuple[int, dict[str, Any]] | None = None
        for directory in directories:
            if run_id:
                path = Path(directory) / f"{run_id}.json"
                if path.is_file():
                    return _load(path)
                continue
            for path in Path(directory).glob("*.json"):
                data = _load(path)
                created = _iso_ns((data or {}).get("created_at"))
                if data is None or created is None:
                    continue
                if started is not None and created < started - 1_000_000_000:
                    continue
                if newest is None or created > newest[0]:
                    newest = (created, data)
        return None if newest is None else newest[1]

    def _result(
        self, record: Mapping[str, Any], journal: Mapping[str, Any] | None
    ) -> str:
        state = str(record.get("state") or "")
        if journal is not None and journal.get("state") == "rolled-back":
            return "rolled-back"
        if journal is not None and journal.get("state") == "interrupted":
            return "interrupted"
        if state == "deployed" and record.get("health") != "degraded":
            return "success"
        return "failure"

    # ------------------------------------------------------------ step events
    def _step_events(
        self,
        run: dict[str, Any],
        record: Mapping[str, Any],
        journal: Mapping[str, Any] | None,
        now: int,
    ) -> None:
        step = run.get("step")
        state = str(record.get("state") or "")
        failure = _dict(record.get("failure"))
        if state in {"failed", "retrying"} and record.get("reason") == (
            "component-build-failed"
        ):
            images = [
                entry["image"]
                for entry in run.get("builds") or []
                if entry.get("step") == step and entry.get("end") is None
            ]
            self._run_event(
                run,
                "piceli.build.failed",
                f"build-failed:{step}",
                now,
                span_path=f"build:{images[0]}:{step}" if images else "root",
                body=f"{run['env']}: build failed",
                attributes={
                    "error.type": "component-build-failed",
                    "piceli.build.images": images,
                    "piceli.build.log_tail": tail_lines(failure.get("log_tail")),
                    "piceli.build.kept_job": _text(failure.get("kept_job"), 253),
                },
                error=True,
            )
        if journal is None:
            return
        stages = _dict(journal.get("stages"))
        checks = _dict(_dict(stages.get("checks")).get("output"))
        failed = [
            _dict(item)
            for item in checks.get("results") or []
            if isinstance(item, Mapping) and not item.get("passed")
        ]
        if failed:
            self._run_event(
                run,
                "piceli.deploy.checks.failed",
                f"checks-failed:{step}",
                now,
                span_path=f"s{step}/checks",
                body=f"{run['env']}: {len(failed)} check(s) failed",
                attributes={
                    "error.type": "pipeline-checks-failed",
                    "piceli.checks.failed": [
                        str(item.get("name") or "check") for item in failed
                    ],
                    "piceli.checks.detail": [
                        f"{item.get('name') or 'check'}: "
                        + str(item.get("detail") or item.get("code") or "")[:300]
                        for item in failed
                    ],
                },
                error=True,
            )
        rollback = _dict(checks.get("rollback"))
        if rollback:
            release = _journal_release(journal)
            self._run_event(
                run,
                "piceli.deploy.rollback",
                f"rollback:{step}",
                now,
                span_path=f"s{step}/rollback",
                body=f"{run['env']}: rolled back to {rollback.get('release')}",
                attributes={
                    "piceli.rollback.from": release,
                    "piceli.rollback.to": _text(rollback.get("release"), 128),
                    "piceli.rollback.state": _text(rollback.get("state"), 32),
                },
                error=rollback.get("state") != "ready",
            )
        apply = _dict(stages.get("apply"))
        if state == "deployed" and apply.get("state") == "done":
            components = _dict(record.get("components"))
            rolled = sorted(
                name
                for name, entry in components.items()
                if _dict(entry).get("state") == "synced"
            )
            self._run_event(
                run,
                "piceli.deploy.rolled",
                f"rolled:{step}",
                now,
                span_path=f"s{step}/apply",
                body=f"{run['env']}: rolled {', '.join(rolled) or 'nothing'}",
                attributes={
                    "piceli.components": rolled,
                    "piceli.image.digests": [
                        f"{name}={_dict(components[name]).get('digest')}"
                        for name in rolled
                    ],
                    "piceli.release.id": _journal_release(journal),
                    **_workloads(journal),
                },
            )
            deleted = [
                f"{item.get('kind')}/{item.get('name')}"
                for item in _dict(record).get("deleted") or []
                if isinstance(item, Mapping)
            ] or [
                f"{change['kind']}/{change['name']}"
                for change in _changes(journal)
                if change["operation"] == "delete"
            ]
            if deleted:
                self._run_event(
                    run,
                    "piceli.deploy.pruned",
                    f"pruned:{step}",
                    now,
                    span_path=f"s{step}/prune",
                    body=f"{run['env']}: pruned {len(deleted)} object(s)",
                    attributes={"piceli.pruned": deleted},
                )

    def _finish_operation(
        self,
        key: str,
        run: dict[str, Any],
        record: Mapping[str, Any],
        work: str,
        now: int,
    ) -> None:
        """A stop, start or teardown step: its own short trace and its event."""
        state = str(record.get("state") or "")
        ok = not record.get("stop_failed")
        if work == "stop" and state == "stopped" and ok:
            stop = _dict(record.get("stop"))
            self._run_event(
                run,
                "piceli.environment.stopped",
                "stopped",
                now,
                body=f"{run['env']}: stopped",
                attributes={
                    "piceli.stop.reason": _text(record.get("reason"), 32),
                    "piceli.stop.via": _text(stop.get("via"), 32),
                },
            )
        elif work == "start_owner" and state != "stopped":
            self._run_event(
                run,
                "piceli.environment.started",
                "started",
                now,
                body=f"{run['env']}: started",
                attributes={},
            )
        elif work in {"stop", "start_owner"}:
            ok = False
        self._close(
            key,
            run,
            "success" if ok else "failure",
            now,
            reason=None if ok else _text(record.get("reason"), 64),
            record=record,
        )
        self.store.save(self._data)

    # ------------------------------------------------------------ spans
    def _close(
        self,
        key: str,
        run: dict[str, Any],
        result: str,
        end: int,
        *,
        reason: str | None = None,
        record: Mapping[str, Any] | None = None,
    ) -> None:
        """End a run: its spans, its last events, its metrics; forget it."""
        assert self.instruments is not None
        runs = self._data["runs"]
        runs.pop(key, None)
        if result == "interrupted":
            for event in run.get("events") or []:  # resent: deduplicated by id
                self._event(
                    event["name"],
                    self._event_id(run, event["what"]),
                    int(event["at"]),
                    body=str(event.get("body") or ""),
                    attributes=_dict(event.get("attributes")),
                    context=self._span_context(run, str(event.get("path") or "root")),
                    error=bool(event.get("error")),
                )
            self._run_event(
                run,
                "piceli.deploy.interrupted",
                "interrupted",
                end,
                body=f"{run['env']}: run interrupted (controller restarted)",
                attributes={"error.type": "controller-restarted"},
                error=True,
            )
        self._set_active(run, None)
        approval = run.get("approval")
        if isinstance(approval, dict) and approval.get("end") is None:
            approval["end"] = end
        if run.get("kind") == "deploy":
            self._approval_event(run, record, end)
        self._spans(run, result, end, reason, record)
        self._metrics(run, result, end, reason)
        if result == "success" and run.get("kind") == "deploy":
            self._data.setdefault("last_success", {})[run["env"]] = end

    def _approval_event(
        self, run: dict[str, Any], record: Mapping[str, Any] | None, end: int
    ) -> None:
        """A run applied under the policy's approval: say so (``via: policy``)."""
        if record is None or run.get("approval"):
            return
        if (
            run.get("approval_at_start") is None
            and record.get("state") == "deployed"
            and record.get("last_action") == "deployed"
            and record.get("deployed_plan_hash") is None
        ):
            run["approved_via"] = "policy"
            self._run_event(
                run,
                "piceli.deploy.approved",
                "approved",
                int(run.get("step_started_ns") or end),
                body=f"{run['env']}: plan approved (policy)",
                attributes={"piceli.approval.via": "policy"},
            )

    def _span(
        self,
        run: Mapping[str, Any],
        path: str,
        name: str,
        start: int,
        end: int,
        attributes: Mapping[str, Any],
        *,
        ok: bool | None,
        parent: str | None = "root",
        root: bool = False,
        message: str | None = None,
    ) -> None:
        from opentelemetry.context import Context
        from opentelemetry.trace import SpanKind, Status, StatusCode

        assert self.providers is not None
        trace_id = int(run["trace_id"], 16)
        ids = self.providers.ids
        if root:
            context = Context()
            ids.local.trace = trace_id
        else:
            context = self._span_context(run, parent or "root")
        ids.local.span = span_id_of(trace_id, path)
        span = self.tracer.start_span(
            name,
            context=context,
            kind=SpanKind.SERVER if root else SpanKind.INTERNAL,
            attributes=_attrs(attributes),
            start_time=start,
        )
        ids.local.trace = ids.local.span = None
        if ok is True:
            span.set_status(Status(StatusCode.OK))
        elif ok is False:
            span.set_status(Status(StatusCode.ERROR, message))
        span.end(end_time=max(start, end))

    def _task(
        self,
        run: Mapping[str, Any],
        path: str,
        name: str,
        task_type: str,
        start: int,
        end: int,
        result: str,
        extra: Mapping[str, Any] | None = None,
        *,
        parent: str = "root",
    ) -> None:
        self._span(
            run,
            path,
            name,
            start,
            end,
            {
                "cicd.pipeline.task.name": name,
                "cicd.pipeline.task.run.id": f"{run['record']}/{run['run']}/{path}",
                "cicd.pipeline.task.run.result": result,
                "cicd.pipeline.task.type": task_type,
                **self._common(run),
                **(extra or {}),
            },
            ok=None if result == "skip" else result == "success",
            parent=parent,
            message=_text(_dict(extra).get("error.type"), 64),
        )

    def _spans(
        self,
        run: dict[str, Any],
        result: str,
        end: int,
        reason: str | None,
        record: Mapping[str, Any] | None,
    ) -> None:
        start = int(run["started_ns"])
        for build in run.get("builds") or []:
            done = build.get("end") is not None
            # WP-B: a build entry's own times win when the record has them.
            component = _dict(
                _dict(_dict(record).get("components")).get(build["image"])
            )
            first = _iso_ns(component.get("started_at")) or int(build["start"])
            last = _iso_ns(component.get("finished_at")) if done else None
            last = last or int(build.get("end") or run.get("step_ended_ns") or end)
            outcome = (
                "success" if done else "error" if result == "interrupted" else "failure"
            )
            self._task(
                run,
                f"build:{build['image']}:{build.get('step')}",
                f"build {build['image']}",
                "build",
                first,
                last,
                outcome,
                {
                    "piceli.image": build["image"],
                    "piceli.image.digest": build.get("digest"),
                    **({"error.type": "component-build-failed"} if not done else {}),
                },
            )
            if self.instruments is not None:
                self.instruments.build_duration.record(
                    max(0.0, (last - first) / 1e9),
                    {
                        "piceli.image": build["image"],
                        "piceli.build.result": outcome,
                        "deployment.environment.name": run["env"],
                    },
                )
        approval = run.get("approval")
        if isinstance(approval, dict) and approval.get("start") is not None:
            via = approval.get("via")
            waited_end = int(approval.get("end") or end)
            self._task(
                run,
                "approval",
                "approval wait",
                "deploy",
                int(approval["start"]),
                waited_end,
                "success"
                if via
                else "cancellation"
                if result == "superseded"
                else "error"
                if result == "interrupted"
                else "failure",
                {
                    "piceli.approval.via": via,
                    "piceli.plan.hash": approval.get("plan_hash"),
                },
            )
            if self.instruments is not None and via:
                self.instruments.approval_wait.record(
                    max(0.0, (waited_end - int(approval["start"])) / 1e9),
                    self._env_attrs(run),
                )
        workloads: dict[str, Any] = {}
        releases: list[str] = []
        for item in run.get("journals") or []:
            journal = _dict(item.get("data"))
            step = item.get("step")
            workloads = _workloads(journal) or workloads
            release = _journal_release(journal)
            if release:
                releases.append(release)
            self._journal_spans(run, journal, step, result, end)
        commit_name, commit_kind = None, None
        names = sorted(run.get("revision") or {})
        primary = names[0] if names else None
        if primary:
            commit_name, commit_kind = _ref_name(_dict(run.get("refs")).get(primary))
        record = _dict(record)
        attributes = {
            "cicd.pipeline.action.name": "SYNC",
            "cicd.pipeline.result": RESULTS.get(result, "failure"),
            "cicd.worker.name": SERVICE_NAME,
            "cicd.worker.id": os.environ.get("HOSTNAME") or None,
            "deployment.id": f"{run['record']}/{run['run']}",
            "deployment.status": "succeeded" if result == "success" else "failed",
            "error.type": reason if result != "success" else None,
            "piceli.deploy.result": result,
            "piceli.run.kind": run.get("kind"),
            "piceli.run.attempt": run.get("attempt"),
            "piceli.trigger": run.get("trigger"),
            "piceli.trigger.kind": trigger_kind(run.get("trigger")),
            "piceli.approval.via": _dict(approval).get("via")
            or run.get("approved_via"),
            "piceli.release.id": releases[-1] if releases else None,
            "piceli.plan.hash": _text(record.get("deployed_plan_hash"), 80),
            "vcs.ref.head.revision": run.get("revision", {}).get(primary)
            if primary
            else None,
            "vcs.ref.head.name": commit_name,
            "vcs.ref.head.type": commit_kind,
            "vcs.repository.name": primary,
            "piceli.sources": names,
            "piceli.source.revisions": [run["revision"][n] for n in names],
            "piceli.source.refs": [
                str(_dict(run.get("refs")).get(n) or "") for n in names
            ],
            "piceli.image.digests": [
                f"{name}={_dict(value).get('digest')}"
                for name, value in sorted(_dict(record.get("components")).items())
                if _dict(value).get("digest")
            ],
            **self._common(run),
            **workloads,
        }
        if record.get("namespace") and not run.get("namespace"):
            attributes["k8s.namespace.name"] = record["namespace"]
        action = "SYNC" if run.get("kind") == "deploy" else run.get("kind", "")
        label = {"start_owner": "START", "stop": "STOP", "teardown": "TEARDOWN"}.get(
            str(action), str(action).upper()
        )
        self._span(
            run,
            "root",
            f"{label} {self._pipeline(run)}",
            start,
            end,
            attributes,
            ok=result == "success",
            parent=None,
            root=True,
            message=reason,
        )

    def _journal_spans(
        self,
        run: Mapping[str, Any],
        journal: Mapping[str, Any],
        step: Any,
        result: str,
        end: int,
    ) -> None:
        stages = _dict(journal.get("stages"))
        for stage, (name, task_type) in STAGES.items():
            entry = _dict(stages.get(stage))
            begin = _iso_ns(entry.get("started_at"))
            if begin is None:
                continue  # never ran (pending, or skipped without running)
            finish = _iso_ns(entry.get("finished_at"))
            state = str(entry.get("state") or "")
            outcome = _STAGE_RESULT.get(
                state, "error" if result == "interrupted" else "failure"
            )
            if finish is None:
                finish = end
                outcome = "error" if result == "interrupted" else outcome
            path = f"s{step}/{stage}"
            extra: dict[str, Any] = {"piceli.stage": stage}
            if entry.get("reason"):
                extra["error.type"] = _text(entry.get("reason"), 64)
            if stage == "apply":
                extra.update(_workloads(journal))
                extra["piceli.release.id"] = _journal_release(journal)
            self._task(run, path, name, task_type, begin, finish, outcome, extra)
            if stage == "apply" and state == "done":
                deleted = [
                    f"{c['kind']}/{c['name']}"
                    for c in _changes(journal)
                    if c["operation"] == "delete"
                ]
                if deleted:
                    # Prune runs inside apply: its own times when the journal
                    # has them (0.16), else the apply window.
                    timed = _dict(_dict(entry.get("output")).get("prune"))
                    self._task(
                        run,
                        f"s{step}/prune",
                        "prune",
                        "deploy",
                        _iso_ns(timed.get("started_at")) or begin,
                        _iso_ns(timed.get("finished_at")) or finish,
                        "success",
                        {"piceli.pruned": deleted, "piceli.pruned.count": len(deleted)},
                        parent=path,
                    )
            if stage == "checks":
                self._check_spans(run, journal, entry, path, begin, finish, step)

    def _check_spans(
        self,
        run: Mapping[str, Any],
        journal: Mapping[str, Any],
        entry: Mapping[str, Any],
        path: str,
        begin: int,
        finish: int,
        step: Any,
    ) -> None:
        output = _dict(entry.get("output"))
        cursor = begin
        for index, item in enumerate(output.get("results") or []):
            check = _dict(item)
            seconds = check.get("duration")
            took = (
                int(float(seconds) * 1e9)
                if isinstance(seconds, int | float) and seconds >= 0
                else 0
            )
            stop = min(finish, cursor + took)
            passed = bool(check.get("passed"))
            name = str(check.get("name") or f"check-{index}")[:128]
            self._task(
                run,
                f"{path}/check:{index}",
                f"check {name}",
                "test",
                cursor,
                stop,
                "success" if passed else "failure",
                {
                    "piceli.check.name": name,
                    "piceli.check.type": _text(check.get("type"), 32),
                    "piceli.check.attempts": check.get("attempts")
                    if isinstance(check.get("attempts"), int)
                    else None,
                    "piceli.check.detail": None
                    if passed
                    else _text(str(check.get("detail") or ""), 300),
                    "error.type": None
                    if passed
                    else _text(check.get("code"), 64) or "check-failed",
                },
                parent=path,
            )
            if self.instruments is not None:
                self.instruments.checks.add(
                    1,
                    {
                        "piceli.check.name": name,
                        "piceli.check.result": "passed" if passed else "failed",
                        "deployment.environment.name": run["env"],
                    },
                )
            cursor = stop
        rollback = _dict(output.get("rollback"))
        if rollback:
            # The rollback runs after the last check, inside the checks stage:
            # its own times when the journal has them (0.16).
            ready = rollback.get("state") == "ready"
            self._task(
                run,
                f"s{step}/rollback",
                "rollback",
                "deploy",
                _iso_ns(rollback.get("started_at")) or cursor,
                _iso_ns(rollback.get("finished_at")) or finish,
                "success" if ready else "failure",
                {
                    "piceli.rollback.from": _journal_release(journal),
                    "piceli.rollback.to": _text(rollback.get("release"), 128),
                    "piceli.rollback.state": _text(rollback.get("state"), 32),
                    "error.type": None if ready else _text(rollback.get("reason"), 64),
                },
            )

    def _metrics(
        self, run: Mapping[str, Any], result: str, end: int, reason: str | None
    ) -> None:
        if self.instruments is None or run.get("kind") != "deploy":
            return
        base = {
            **self._env_attrs(run),
            "cicd.pipeline.result": RESULTS.get(result, "failure"),
            "piceli.deploy.result": result,
        }
        start = int(run["started_ns"])
        approval = _dict(run.get("approval"))
        pending = 0
        if approval.get("start") is not None:
            pending = max(0, int(approval.get("end") or end) - int(approval["start"]))
        executing = max(0, end - start - pending)
        self.instruments.run_duration.record(
            executing / 1e9, {**base, "cicd.pipeline.run.state": "executing"}
        )
        if pending:
            self.instruments.run_duration.record(
                pending / 1e9, {**base, "cicd.pipeline.run.state": "pending"}
            )
        if result != "success":
            self.instruments.run_errors.add(
                1, {**self._env_attrs(run), "error.type": reason or result}
            )


# ---------------------------------------------------------------- journals


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _journal_view(journal: Mapping[str, Any]) -> dict[str, Any]:
    """What the spans need of a run journal (kept with the open run)."""
    stages: dict[str, Any] = {}
    for name, value in _dict(journal.get("stages")).items():
        entry = _dict(value)
        kept: dict[str, Any] = {
            key: entry[key]
            for key in ("state", "started_at", "finished_at", "reason")
            if key in entry
        }
        output = _dict(entry.get("output"))
        if name == "checks":
            kept["output"] = {
                "results": [
                    {
                        key: _dict(item).get(key)
                        for key in (
                            "name",
                            "type",
                            "passed",
                            "code",
                            "detail",
                            "duration",
                            "attempts",
                        )
                    }
                    for item in output.get("results") or []
                ][:64],
                **(
                    {
                        "rollback": {
                            key: _dict(output.get("rollback")).get(key)
                            for key in (
                                "state",
                                "release",
                                "reason",
                                "started_at",
                                "finished_at",
                            )
                        }
                    }
                    if output.get("rollback")
                    else {}
                ),
            }
        elif name in {"plan", "apply"}:
            prune = _dict(_dict(output.get("execution")).get("prune"))
            kept["output"] = {
                "release": output.get("release"),
                **(
                    {
                        "prune": {
                            key: prune.get(key) for key in ("started_at", "finished_at")
                        }
                    }
                    if prune
                    else {}
                ),
                **(
                    {
                        "changes": [
                            {
                                key: _dict(item).get(key)
                                for key in ("operation", "kind", "name")
                            }
                            for item in output.get("changes") or []
                        ][:200]
                    }
                    if name == "plan"
                    else {}
                ),
            }
        stages[name] = kept
    return {
        "run_id": journal.get("run_id"),
        "state": journal.get("state"),
        "stages": stages,
    }


def _changes(journal: Mapping[str, Any]) -> list[dict[str, str]]:
    output = _dict(_dict(_dict(journal.get("stages")).get("plan")).get("output"))
    return [
        {
            "operation": str(_dict(item).get("operation")),
            "kind": str(_dict(item).get("kind")),
            "name": str(_dict(item).get("name")),
        }
        for item in output.get("changes") or []
        if isinstance(item, Mapping)
    ]


def _journal_release(journal: Mapping[str, Any]) -> str | None:
    stages = _dict(journal.get("stages"))
    for stage in ("apply", "plan"):
        release = _dict(_dict(stages.get(stage)).get("output")).get("release")
        if isinstance(release, str) and release:
            return release[:128]
    return None


def _workloads(journal: Mapping[str, Any]) -> dict[str, Any]:
    """The Deployments and StatefulSets a run created or changed (k8s.* attributes;
    pruned ones are in ``piceli.pruned``)."""
    found: dict[str, list[str]] = {"Deployment": [], "StatefulSet": []}
    for change in _changes(journal):
        if change["kind"] in found and change["operation"] not in {"no-op", "delete"}:
            found[change["kind"]].append(change["name"])
    result: dict[str, Any] = {}
    deployments, statefulsets = (
        sorted(found["Deployment"]),
        sorted(found["StatefulSet"]),
    )
    if deployments:
        result["piceli.k8s.deployments"] = deployments
        if len(deployments) == 1:
            result["k8s.deployment.name"] = deployments[0]
    if statefulsets:
        result["piceli.k8s.statefulsets"] = statefulsets
        if len(statefulsets) == 1:
            result["k8s.statefulset.name"] = statefulsets[0]
    return result


# ---------------------------------------------------------------- wiring


def for_controller(
    state_dir: Path,
    telemetry: Mapping[str, Any] | None,
    *,
    cluster: str | None,
    composition: str | None,
    version: str,
    cluster_uid: str | None = None,
    namespace: str | None = None,
    log: Callable[[str], None] = lambda _m: None,
    environ: Mapping[str, str] | None = None,
) -> ControllerTelemetry:
    """The telemetry of ``gitops run``: OTLP when configured, else disabled.

    A configuration that cannot work (or a missing SDK) logs one line with a
    fixed reason and disables telemetry; the controller runs on.
    """
    try:
        settings = resolve(telemetry, environ)
    except (OtlpConfigError, OSError) as error:
        message = str(error) if isinstance(error, OtlpConfigError) else "unreadable"
        log(f"telemetry: off (configuration refused: {message})")
        return ControllerTelemetry.disabled(state_dir)
    if settings is None:
        return ControllerTelemetry.disabled(state_dir)
    resource = resource_attributes(
        version=version,
        cluster_uid=cluster_uid,
        cluster_name=cluster,
        namespace=namespace,
    )
    try:
        providers = otlp_providers(settings, resource)
    except ImportError:
        log("telemetry: off (install piceli[telemetry] for the OpenTelemetry SDK)")
        return ControllerTelemetry.disabled(state_dir)
    except Exception as error:
        log(f"telemetry: off ({type(error).__name__})")
        return ControllerTelemetry.disabled(state_dir)
    where = (
        "the OTEL_EXPORTER_OTLP_* variables"
        if settings.from_environment
        else (urlsplit(str(settings.endpoint)).hostname or "endpoint")
    )
    log(f"telemetry: OTLP {settings.protocol} to {where}")
    return ControllerTelemetry(
        state_dir,
        providers,
        cluster=cluster,
        composition=composition,
        version=version,
        log=log,
    )


def cluster_uid(api: Any) -> str | None:
    """The ``kube-system`` namespace's UID (``k8s.cluster.uid``), or ``None``."""
    try:
        found = api.call("/api/v1/namespaces/kube-system", "GET")
    except Exception:
        return None
    uid = _dict(_dict(found).get("metadata")).get("uid")
    return uid if isinstance(uid, str) and len(uid) <= 64 else None
