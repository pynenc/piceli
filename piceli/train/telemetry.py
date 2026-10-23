"""A train run as one OpenTelemetry trace, with logs (``piceli.dev.*``).

:class:`TrainTelemetry` is an event sink (:data:`~piceli.train.runner.EventSink`)
that turns a run's events into OTLP/HTTP requests (JSON encoding, no SDK):

- a root span ``piceli.dev.train`` for the run, with span events for each
  verdict (``branch-joined``, ``branch-ejected``, ``branch-included``) and
  for ``promoted``, ``promote-rejected``, ``pushed`` and ``push-failed``;
- a child span ``piceli.dev.train.trial`` per trial, and under it a span
  ``piceli.dev.train.step`` per step (publish steps hang off the root), with
  the branches, step, kind, host, verdict, duration and impact selection;
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
variables (as ``piceli deploy``). Headers come from
``OTEL_EXPORTER_OTLP_HEADERS`` only and are never printed.

Importing this module is side-effect free.
"""

from __future__ import annotations

import os
import queue
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from piceli.deploy_events import (
    DeployEventsConfigError,
    OtlpConfig,
    _attrs,
    _ns,
    _pairs,
    _post,
    resolve,
)

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
_OK_STEP = frozenset({"passed", "skipped"})
_ROOT_EVENTS = frozenset(
    {
        "branch-joined",
        "branch-ejected",
        "branch-included",
        "promoted",
        "promote-rejected",
        "pushed",
        "push-failed",
    }
)


def config_for(
    telemetry: Otlp | None, environ: Mapping[str, str] | None = None
) -> OtlpConfig | None:
    """The export configuration: the train's ``Otlp``, else the standard
    environment variables; ``None`` when neither asks for export.

    Raises :class:`~piceli.deploy_events.DeployEventsConfigError` for a
    configuration that cannot work.
    """
    env = os.environ if environ is None else environ
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return None
    if telemetry is None:
        found = resolve(None, env)
        return (
            None
            if found is None
            else replace(found, timeout=min(found.timeout, DEFAULT_TIMEOUT))
        )
    base = telemetry.endpoint.rstrip("/")
    name = env.get("OTEL_SERVICE_NAME", "").strip() or "piceli"
    return OtlpConfig(
        f"{base}/v1/traces",
        f"{base}/v1/logs",
        _pairs(env.get("OTEL_EXPORTER_OTLP_HEADERS", ""), header=True),
        name,
        _pairs(env.get("OTEL_RESOURCE_ATTRIBUTES", ""), header=False),
        DEFAULT_TIMEOUT,
    )


def log_tail(path: Any, lines: int = TAIL_LINES, limit: int = TAIL_BYTES) -> str:
    """The last ``lines`` lines (at most ``limit`` characters) of a log file."""
    from piceli.train.status import tail

    return tail(path, lines)[-limit:]


class _Exporter:
    """Posts requests from a bounded queue in a daemon thread."""

    def __init__(self, config: OtlpConfig, warn: Callable[[str], None]) -> None:
        self.config = config
        self.warn = warn
        self.queue: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue(
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

    def put(self, url: str, body: dict[str, Any]) -> None:
        if self.failures >= MAX_FAILURES:
            self.dropped += 1
            return
        try:
            self.queue.put_nowait((url, body))
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
            url, body = item
            try:
                _post(
                    url,
                    body,
                    self.config.headers,
                    time.monotonic() + self.config.timeout,
                )
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
        self, config: OtlpConfig, *, warn: Callable[[str], None] = lambda _m: None
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
    cannot work only warns (``train-telemetry-failed``)."""
    try:
        config = config_for(telemetry)
    except DeployEventsConfigError:
        warn("warning: train telemetry configuration unusable [train-telemetry-failed]")
        return None
    return None if config is None else TrainTelemetry(config, warn=warn)
