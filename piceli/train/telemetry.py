"""A train run as one OpenTelemetry trace, with logs (``piceli.dev.*``).

:class:`TrainTelemetry` is an event sink (:data:`~piceli.train.runner.EventSink`)
that turns a run's events into OTLP/HTTP requests (no SDK): protobuf for
``http/protobuf`` (with the ``opentelemetry-proto`` classes of the
``piceli[telemetry]`` extra), JSON for ``http/json``:

- a root span ``piceli.dev.train`` for the run, with span events for each
  verdict (``branch-joined``, ``branch-ejected``, ``branch-included``) and
  for ``promoted``, ``promote-rejected``, ``pushed``, ``push-failed``,
  ``halves-parallel``, ``trial-discarded``, ``outputs-restored``,
  ``pruned``, ``branch-retired`` and ``retire-skipped``;
- a child span ``piceli.dev.train.trial`` per trial (``speculative`` for
  the right half of parallel halves), and under it a span
  ``piceli.dev.train.step`` per step (publish steps hang off the root), with
  the branches, step, kind, the host it ran on (a build host or ``local``),
  verdict (``reused``, with the run it came from, is ok), duration and
  impact selection;
- a span event ``piceli.dev.train.flaky`` on a step for each flaky test;
- a log record per step (``event.name`` ``piceli.dev.train.step``) whose
  body is the last lines of the step's output, linked to its span;
- a log record ``piceli.dev.train.finished`` (an OpenTelemetry event) with
  the run's summary when it ends.

Spans are sent as they finish, so a long run shows up while it runs. A
collector that is down never slows or fails a run: requests go through a
bounded queue to a background thread, each with a short timeout; when the
queue is full a request is dropped, and after three failures in a row the
exporter stops trying. The first failure prints one warning line
(``train-telemetry-failed``). At the end the run waits at most the timeout
for the queue to drain.

Configure it with ``Train(telemetry=Otlp("http://localhost:4318",
protocol="http/protobuf"))``, or with the standard ``OTEL_EXPORTER_OTLP_*``
variables (as ``piceli deploy``; ``OTEL_EXPORTER_OTLP_PROTOCOL`` unset is
JSON there). A collector behind a private CA: ``Otlp(..., ca_file=PATH)`` or
``OTEL_EXPORTER_OTLP_CERTIFICATE``. Headers come from
``OTEL_EXPORTER_OTLP_HEADERS`` only and are never printed.

A spec that declares ``http/protobuf`` without the extra installed is
refused before the run starts (``train-telemetry-unavailable``,
:func:`check_export`): a collector that takes protobuf only would reject
JSON, so a fallback would export nothing.

Importing this module is side-effect free.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from piceli.deploy_events import (
    DeployEventsConfigError,
    OtlpConfig,
    _attrs,
    _ns,
    _pairs,
    resolve,
)
from piceli.train.model import TrainError

if TYPE_CHECKING:
    from piceli.infra import Otlp

SCHEMA = "piceli.dev.train.v1"
#: Seconds one export request may take.
DEFAULT_TIMEOUT = 2.0
#: Requests waiting to be sent; more are dropped.
QUEUE_SIZE = 256
#: Failures in a row after which the exporter stops trying.
MAX_FAILURES = 3
#: Lines and bytes of a step's output in its log record.
TAIL_LINES = 20
TAIL_BYTES = 4096
_OK_STEP = frozenset({"passed", "skipped", "reused"})
_ROOT_EVENTS = frozenset(
    {
        "branch-joined",
        "branch-ejected",
        "branch-included",
        "promoted",
        "promote-rejected",
        "pushed",
        "push-failed",
        "halves-parallel",
        "trial-discarded",
        "outputs-restored",
        "pruned",
        "branch-retired",
        "retire-skipped",
    }
)


#: The extra that provides the protobuf classes.
EXTRA = "piceli[telemetry]"
_IDS = frozenset({"traceId", "spanId", "parentSpanId"})
_PROTOBUF_MODULES = (
    "google.protobuf.json_format",
    "opentelemetry.proto.collector.logs.v1.logs_service_pb2",
    "opentelemetry.proto.collector.trace.v1.trace_service_pb2",
)


@dataclass(frozen=True)
class TrainOtlpConfig(OtlpConfig):
    """Where and how a train exports: :class:`OtlpConfig` plus the
    encoding (``http/protobuf`` or ``http/json``) and a CA file."""

    protocol: str = "http/json"
    ca_file: str | None = field(default=None, repr=False)


def protobuf_available() -> bool:
    """Whether the OTLP protobuf classes (the ``piceli[telemetry]`` extra)
    can be imported."""
    from importlib import import_module

    try:
        for module in _PROTOBUF_MODULES:
            import_module(module)
    except ImportError:
        return False
    return True


def _ca_file(value: str | None) -> str | None:
    """The CA file as an absolute path (``~`` expanded, relative to the
    current directory); it must be a readable file."""
    if not value or not value.strip():
        return None
    path = Path(value.strip()).expanduser().resolve()
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError:
        raise DeployEventsConfigError("certificate") from None
    return str(path)


def config_for(
    telemetry: Otlp | None, environ: Mapping[str, str] | None = None
) -> TrainOtlpConfig | None:
    """The export configuration: the train's ``Otlp``, else the standard
    environment variables; ``None`` when neither asks for export.

    The train's ``Otlp`` sends protobuf (its protocol is ``http/protobuf``);
    with the variables, ``OTEL_EXPORTER_OTLP_PROTOCOL`` decides
    (``http/protobuf``, else JSON). The CA file is ``Otlp(ca_file=)``, else
    ``OTEL_EXPORTER_OTLP_CERTIFICATE``.

    Raises :class:`~piceli.deploy_events.DeployEventsConfigError` for a
    configuration that cannot work.
    """
    env = os.environ if environ is None else environ
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return None
    certificate = env.get("OTEL_EXPORTER_OTLP_CERTIFICATE", "")
    if telemetry is None:
        found = resolve(None, env)
        if found is None:
            return None
        protocol = env.get("OTEL_EXPORTER_OTLP_PROTOCOL", "").strip()
        return TrainOtlpConfig(
            found.traces_url,
            found.logs_url,
            found.headers,
            found.service_name,
            found.resource,
            min(found.timeout, DEFAULT_TIMEOUT),
            protocol="http/protobuf" if protocol == "http/protobuf" else "http/json",
            ca_file=_ca_file(certificate),
        )
    base = telemetry.endpoint.rstrip("/")
    name = env.get("OTEL_SERVICE_NAME", "").strip() or "piceli"
    return TrainOtlpConfig(
        f"{base}/v1/traces",
        f"{base}/v1/logs",
        _pairs(env.get("OTEL_EXPORTER_OTLP_HEADERS", ""), header=True),
        name,
        _pairs(env.get("OTEL_RESOURCE_ATTRIBUTES", ""), header=False),
        DEFAULT_TIMEOUT,
        protocol=telemetry.protocol,
        ca_file=_ca_file(telemetry.ca_file or certificate),
    )


def check_export(
    telemetry: Otlp | None, environ: Mapping[str, str] | None = None
) -> None:
    """Refuse, before a run starts, a spec's ``telemetry=`` that cannot
    export (:class:`~piceli.train.model.TrainError`):
    ``train-telemetry-unavailable`` for ``http/protobuf`` without the
    extra, ``train-invalid`` for a CA file this machine cannot read. The
    standard variables are never refused here: they only warn when the run
    starts (:func:`telemetry_for`)."""
    if telemetry is None:
        return
    env = os.environ if environ is None else environ
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return
    if telemetry.protocol == "http/protobuf" and not protobuf_available():
        raise TrainError(
            "train-telemetry-unavailable",
            'Otlp(protocol="http/protobuf") sends protobuf, which needs the '
            f"telemetry extra: pip install '{EXTRA}'",
        )
    try:
        config_for(telemetry, env)
    except DeployEventsConfigError as error:
        what = {
            "certificate": "Otlp(ca_file=) (or OTEL_EXPORTER_OTLP_CERTIFICATE) is "
            "not a readable file",
        }.get(str(error), f"the telemetry configuration is unusable ({error})")
        raise TrainError("train-invalid", what) from None


def encode(signal: str, body: Mapping[str, Any], protocol: str) -> tuple[bytes, str]:
    """``(request bytes, content type)`` of an OTLP JSON body
    (``resourceSpans`` or ``resourceLogs``) in ``protocol``.

    Protobuf is the same request parsed into the generated classes: the
    OTLP JSON mapping is protobuf's, except that trace and span ids are hex
    in OTLP JSON and base64 in protobuf's (so they are converted first).
    """
    if protocol != "http/protobuf":
        return json.dumps(body).encode(), "application/json"
    from google.protobuf.json_format import ParseDict
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
        ExportLogsServiceRequest,
    )
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    request: Any = (
        ExportTraceServiceRequest()
        if signal == "traces"
        else ExportLogsServiceRequest()
    )
    message = ParseDict(_base64_ids(body), request)
    return message.SerializeToString(), "application/x-protobuf"


def _base64_ids(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: base64.b64encode(bytes.fromhex(item)).decode()
            if key in _IDS and isinstance(item, str)
            else _base64_ids(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_base64_ids(item) for item in value]
    return value


def send(
    config: TrainOtlpConfig, url: str, signal: str, body: Mapping[str, Any]
) -> None:
    """POST one request within the timeout; raises on any failure."""
    from urllib3 import PoolManager, Timeout

    payload, content_type = encode(signal, body, config.protocol)
    deadline = time.monotonic() + config.timeout
    pool = (
        PoolManager(cert_reqs="CERT_REQUIRED", ca_certs=config.ca_file)
        if config.ca_file
        else PoolManager()
    )
    try:
        remaining = max(0.001, deadline - time.monotonic())
        response = pool.request(
            "POST",
            url,
            body=payload,
            headers={**config.headers, "Content-Type": content_type},
            timeout=Timeout(total=remaining, connect=remaining, read=remaining),
            retries=False,
            redirect=False,
        )
        if not 200 <= response.status < 300:
            raise OSError("status")
    finally:
        pool.clear()


def log_tail(path: Any, lines: int = TAIL_LINES, limit: int = TAIL_BYTES) -> str:
    """The last ``lines`` lines (at most ``limit`` characters) of a log file."""
    from piceli.train.status import tail

    return tail(path, lines)[-limit:]


class _Exporter:
    """Posts requests from a bounded queue in a daemon thread."""

    def __init__(self, config: TrainOtlpConfig, warn: Callable[[str], None]) -> None:
        self.config = config
        self.warn = warn
        self.queue: queue.Queue[tuple[str, str, dict[str, Any]] | None] = queue.Queue(
            QUEUE_SIZE
        )
        self.failures = 0
        self.sent = 0
        self.dropped = 0
        self.warned = False
        self._warn_lock = threading.Lock()
        self.thread = threading.Thread(
            target=self._work, name="piceli-train-telemetry", daemon=True
        )
        self.thread.start()

    def put(self, url: str, signal: str, body: dict[str, Any]) -> None:
        if self.failures >= MAX_FAILURES:
            self.dropped += 1
            return
        try:
            self.queue.put_nowait((url, signal, body))
        except queue.Full:
            self.dropped += 1

    def _work(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                return
            if self.failures >= MAX_FAILURES:
                self.dropped += 1
                continue
            url, signal, body = item
            try:
                send(self.config, url, signal, body)
            except Exception:
                self.failures += 1
                self._warn_once()
                continue
            self.failures = 0
            self.sent += 1

    def _warn_once(self) -> None:
        with self._warn_lock:
            if self.warned:
                return
            self.warned = True
        self.warn(
            "warning: train telemetry not exported "
            "[train-telemetry-failed]; the run is unaffected"
        )

    def close(self) -> None:
        """Wait at most the timeout (plus a little) for the queue to drain;
        what is still unsent then is given up, with the one warning."""
        try:
            self.queue.put(None, timeout=0.1)
        except queue.Full:
            pass
        self.thread.join(self.config.timeout + 0.5)
        if self.thread.is_alive():
            self._warn_once()


class TrainTelemetry:
    """An event sink exporting one run as one trace (see the module).

    :param config: Where to send (:func:`config_for`).
    :param warn: Prints the single warning line (stderr).
    """

    def __init__(
        self,
        config: TrainOtlpConfig,
        *,
        warn: Callable[[str], None] = lambda _m: None,
    ) -> None:
        self.config = config
        self.exporter = _Exporter(config, warn)
        self.trace_id = secrets.token_hex(16)
        self.root_id = secrets.token_hex(8)
        self.root_start: int | None = None
        self.root: dict[str, Any] = {}
        self.root_events: list[dict[str, Any]] = []
        self.trials: dict[Any, tuple[str, int]] = {}
        self.steps: dict[tuple[Any, str], tuple[str, int]] = {}
        self.closed = False

    # -------------------------------------------------------------- input

    def __call__(self, event: Mapping[str, Any]) -> None:
        """Observe one train event (never raises)."""
        try:
            self._event(event)
        except Exception:
            pass

    def _event(self, event: Mapping[str, Any]) -> None:
        kind = str(event.get("event"))
        at = _ns(event.get("at")) or time.time_ns()
        if kind == "run-started":
            self.root_start = at
            self.root = {
                "piceli.schema": SCHEMA,
                "piceli.train.name": event.get("train"),
                "piceli.train.run": event.get("run"),
                "piceli.train.base_commit": event.get("base"),
                "piceli.train.branches": list(event.get("branches") or ()),
            }
        elif kind == "trial-started":
            self.trials[event.get("trial")] = (secrets.token_hex(8), at)
        elif kind == "step-started":
            key = (event.get("trial"), str(event.get("step")))
            self.steps[key] = (secrets.token_hex(8), at)
        elif kind in {"step-finished", "published", "publish-failed"}:
            self._step(event, at)
        elif kind == "trial-finished":
            self._trial(event, at)
        elif kind in _ROOT_EVENTS:
            self.root_events.append(
                {
                    "timeUnixNano": str(at),
                    "name": kind,
                    "attributes": _attrs(
                        {
                            "piceli.train.branch": event.get("branch"),
                            "piceli.train.trial": event.get("trial"),
                            "piceli.train.step": event.get("step"),
                            "piceli.train.reason": event.get("reason"),
                            "piceli.train.tag": event.get("tag"),
                            "piceli.train.head": event.get("head"),
                            "piceli.train.remote": event.get("remote"),
                            "piceli.train.base_commit": event.get("base"),
                            "piceli.train.reached": event.get("reached"),
                            "piceli.train.left": event.get("left"),
                            "piceli.train.right": event.get("right"),
                            "piceli.train.steps": event.get("steps"),
                            "piceli.train.pruned.freed": event.get("freed"),
                            "piceli.train.pruned.removed": event.get("removed"),
                            "piceli.train.renamed": event.get("renamed"),
                        }
                    ),
                }
            )
        elif kind in {"run-finished", "run-failed"}:
            self._finish(event, at)

    # ------------------------------------------------------------- output

    def _span(
        self,
        name: str,
        span_id: str,
        parent: str | None,
        start: int,
        end: int,
        attributes: Mapping[str, Any],
        ok: bool,
        events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        span: dict[str, Any] = {
            "traceId": self.trace_id,
            "spanId": span_id,
            "name": name,
            "kind": 1,
            "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(max(start, end)),
            "attributes": _attrs(attributes),
            "status": {"code": 1 if ok else 2},
        }
        if parent:
            span["parentSpanId"] = parent
        if events:
            span["events"] = events
        return span

    def _resource(self) -> dict[str, Any]:
        return {
            "attributes": _attrs(
                {
                    **self.config.resource,
                    "service.name": self.config.service_name,
                    "telemetry.sdk.language": "python",
                    "telemetry.sdk.name": "piceli",
                }
            )
        }

    def _send_spans(self, spans: list[dict[str, Any]]) -> None:
        scope = {"name": "piceli.dev.train", "version": "1"}
        self.exporter.put(
            self.config.traces_url,
            "traces",
            {
                "resourceSpans": [
                    {
                        "resource": self._resource(),
                        "scopeSpans": [{"scope": scope, "spans": spans}],
                    }
                ]
            },
        )

    def _send_log(
        self,
        at: int,
        body: str,
        attributes: Mapping[str, Any],
        span_id: str,
        ok: bool,
    ) -> None:
        record = {
            "timeUnixNano": str(at),
            "observedTimeUnixNano": str(at),
            "severityNumber": 9 if ok else 17,
            "severityText": "INFO" if ok else "ERROR",
            "body": {"stringValue": body},
            "attributes": _attrs(attributes),
            "traceId": self.trace_id,
            "spanId": span_id,
            "flags": 1,
        }
        scope = {"name": "piceli.dev.train", "version": "1"}
        self.exporter.put(
            self.config.logs_url,
            "logs",
            {
                "resourceLogs": [
                    {
                        "resource": self._resource(),
                        "scopeLogs": [{"scope": scope, "logRecords": [record]}],
                    }
                ]
            },
        )

    def _step(self, event: Mapping[str, Any], at: int) -> None:
        trial = event.get("trial")
        step = str(event.get("step"))
        duration = event.get("duration")
        start_guess = at - int(float(duration or 0) * 1e9)
        span_id, start = self.steps.pop(
            (trial, step), (secrets.token_hex(8), start_guess)
        )
        parent = self.trials.get(trial, (self.root_id, 0))[0] if trial else self.root_id
        verdict = str(event.get("verdict"))
        ok = verdict in _OK_STEP
        selection = event.get("selection")
        attributes = {
            "piceli.train.name": self.root.get("piceli.train.name"),
            "piceli.train.run": self.root.get("piceli.train.run"),
            "piceli.train.trial": trial,
            "piceli.train.step": step,
            "piceli.train.step.kind": event.get("kind"),
            "piceli.train.step.phase": "test" if trial else "publish",
            "piceli.train.verdict": verdict,
            "piceli.train.host": event.get("host"),
            "piceli.train.duration_ms": int(float(duration or 0) * 1000),
            "piceli.train.branches": list(event.get("branches") or ()),
            "piceli.train.selection": (
                list(selection) if isinstance(selection, list) else selection
            ),
            "piceli.train.reused_from": (event.get("reused") or {}).get("run"),
        }
        flaky = [
            {
                "timeUnixNano": str(at),
                "name": "piceli.dev.train.flaky",
                "attributes": _attrs(
                    {
                        "piceli.train.test": item.get("test"),
                        "piceli.train.test.target": item.get("target"),
                    }
                ),
            }
            for item in event.get("flaky") or ()
            if isinstance(item, Mapping)
        ]
        self._send_spans(
            [
                self._span(
                    "piceli.dev.train.step",
                    span_id,
                    parent,
                    start,
                    at,
                    attributes,
                    ok,
                    flaky,
                )
            ]
        )
        tail = log_tail(event.get("log")) or str(event.get("tail") or "")
        self._send_log(
            at,
            tail or f"{step}: {verdict}",
            {**attributes, "event.name": "piceli.dev.train.step"},
            span_id,
            ok,
        )

    def _trial(self, event: Mapping[str, Any], at: int) -> None:
        trial = event.get("trial")
        span_id, start = self.trials.get(trial, (secrets.token_hex(8), at))
        verdict = str(event.get("verdict"))
        self._send_spans(
            [
                self._span(
                    "piceli.dev.train.trial",
                    span_id,
                    self.root_id,
                    start,
                    at,
                    {
                        "piceli.train.name": self.root.get("piceli.train.name"),
                        "piceli.train.run": self.root.get("piceli.train.run"),
                        "piceli.train.trial": trial,
                        "piceli.train.branches": list(event.get("branches") or ()),
                        "piceli.train.verdict": verdict,
                        "piceli.train.step": event.get("step"),
                        "piceli.train.duration_ms": int(
                            float(event.get("duration") or 0) * 1000
                        ),
                        "piceli.train.speculative": event.get("speculative"),
                    },
                    verdict == "passed",
                )
            ]
        )

    def _finish(self, event: Mapping[str, Any], at: int) -> None:
        summary = event.get("summary") or {}
        ok = bool(summary.get("ok", event.get("ok")))
        attributes = {
            **self.root,
            "piceli.train.state": event.get("state"),
            "piceli.train.reason": event.get("reason"),
            "piceli.train.ok": ok,
            "piceli.train.joined": list(summary.get("joined") or ()),
            "piceli.train.ejected": [
                f"{item.get('branch')}={item.get('step')}"
                for item in summary.get("ejected") or ()
            ],
            "piceli.train.tag": summary.get("tag"),
            "piceli.train.promote": summary.get("promote"),
            "piceli.train.push": summary.get("push"),
            "piceli.train.head": summary.get("head"),
            "piceli.train.flaky": [
                item.get("key") for item in summary.get("flaky") or ()
            ],
            "piceli.train.duration_ms": int(float(event.get("duration") or 0) * 1000),
        }
        start = self.root_start or at
        self._send_spans(
            [
                self._span(
                    "piceli.dev.train",
                    self.root_id,
                    None,
                    start,
                    at,
                    attributes,
                    ok,
                    self.root_events,
                )
            ]
        )
        from piceli.train.summary import headline

        self._send_log(
            at,
            headline(summary) if summary else f"train {event.get('state')}",
            {**attributes, "event.name": "piceli.dev.train.finished"},
            self.root_id,
            ok,
        )

    def close(self) -> None:
        """Flush (bounded by the timeout); later events are ignored."""
        if self.closed:
            return
        self.closed = True
        self.exporter.close()


def telemetry_for(
    telemetry: Otlp | None, warn: Callable[[str], None]
) -> TrainTelemetry | None:
    """A sink when export is configured, else ``None``; a configuration that
    cannot work (protobuf asked for by the standard variables without the
    extra, a CA file that cannot be read) only warns
    (``train-telemetry-failed``)."""
    try:
        config = config_for(telemetry)
    except DeployEventsConfigError:
        config = None
        unusable = True
    else:
        unusable = (
            config is not None
            and config.protocol == "http/protobuf"
            and not protobuf_available()
        )
    if unusable:
        warn("warning: train telemetry configuration unusable [train-telemetry-failed]")
        return None
    return None if config is None else TrainTelemetry(config, warn=warn)
