"""Deploy events: ``piceli deploy`` and ``piceli release`` export to OTLP/HTTP.

An in-process OTLP receiver records the JSON requests. The deploy itself runs
against the fake API server (see ``test_deploy_pipeline``).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from piceli.deploy_events import (
    DeployEventsConfigError,
    resolve,
)
from tests.acceptance.fake_api import TARGET
from tests.acceptance.test_deploy_pipeline import FakeBackend, deploy, with_checks
from tests.acceptance.test_release_cli import (  # noqa: F401 (fixture)
    DIGEST_2,
    _receipt,
    _run,
    release_env,
)

SECRET = "s3cr3t-token-value"


@contextmanager
def receiver(status: int = 200, delay: float = 0) -> Iterator[Any]:
    requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, dict(self.headers), json.loads(raw)))
            time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Length", "2")
            self.end_headers()
            try:
                self.wfile.write(b"{}")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args: Any) -> None:
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


def attrs(items: list[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for item in items:
        (value,) = item["value"].values()
        result[item["key"]] = value
    return result


def spans(requests: Any) -> list[dict[str, Any]]:
    (body,) = [b for path, _h, b in requests if path == "/v1/traces"]
    return body["resourceSpans"][0]["scopeSpans"][0]["spans"]


def logs(requests: Any) -> list[dict[str, Any]]:
    (body,) = [b for path, _h, b in requests if path == "/v1/logs"]
    return body["resourceLogs"][0]["scopeLogs"][0]["logRecords"]


def test_off_unless_configured(shop, monkeypatch) -> None:
    _api, tmp_path = shop
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    code, _events, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stderr
    assert "deploy events" not in result.stderr


def test_deploy_exports_one_trace_and_a_result_log(shop, monkeypatch) -> None:
    _api, tmp_path = shop
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", f"authorization=Bearer {SECRET}")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "shop-deployer")
    with receiver() as (url, requests):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", url)
        code, events, result = deploy(tmp_path, "--auto-approve")
        assert code == 0, result.stderr
        final = events[-1]
        (root, *children) = spans(requests)
        assert root["name"] == "piceli.deploy" and "parentSpanId" not in root
        names = {c["name"] for c in children}
        assert {
            "piceli.deploy.inputs",
            "piceli.deploy.build",
            "piceli.deploy.deliver",
            "piceli.deploy.plan",
            "piceli.deploy.apply",
        } <= names
        assert all(c["parentSpanId"] == root["spanId"] for c in children)
        assert all(c["traceId"] == root["traceId"] for c in children)
        top = attrs(root["attributes"])
        assert top["piceli.app"] == "shop"
        assert top["k8s.namespace.name"] == TARGET.namespace
        assert top["piceli.run.id"] == final["run_id"]
        assert top["piceli.release.id"] == final["release"]
        assert top["piceli.plan.hash"] == final["combined_hash"]
        assert top["piceli.outcome"] == "ready"
        assert top["piceli.image.digests"]["values"] == [
            {"stringValue": f"{n}={d}"} for n, d in sorted(final["images"].items())
        ]
        assert int(top["piceli.duration_ms"]) >= 0
        stage = {c["name"]: attrs(c["attributes"]) for c in children}
        assert stage["piceli.deploy.apply"]["piceli.stage.state"] == "done"
        (record,) = logs(requests)
        assert record["body"] == {"stringValue": "piceli.deploy.ready"}
        assert record["traceId"] == root["traceId"]
        assert record["spanId"] == root["spanId"]
        assert attrs(record["attributes"])["event.name"] == "piceli.deploy"
        resource = requests[0][2]["resourceSpans"][0]["resource"]
        assert attrs(resource["attributes"])["service.name"] == "shop-deployer"
        assert requests[0][1]["authorization"] == f"Bearer {SECRET}"
        # Nothing secret leaves: not the header, not the credentials value.
        wire = json.dumps([b for _p, _h, b in requests])
        assert SECRET not in wire and "password" not in wire.lower()
        assert SECRET not in result.stderr and SECRET not in result.stdout


def test_plan_only_run_exports_a_trace_but_no_result_marker(shop, monkeypatch) -> None:
    _api, tmp_path = shop
    with receiver() as (url, requests):
        code, _events, result = deploy(tmp_path, "--plan", "--otlp-endpoint", url)
        assert code == 0, result.stderr
        assert attrs(spans(requests)[0]["attributes"])["piceli.outcome"] == "planned"
        assert not [1 for path, _h, _b in requests if path == "/v1/logs"]


def test_failed_checks_roll_back_and_the_events_say_so(shop, monkeypatch) -> None:
    _api, tmp_path = shop
    with_checks(tmp_path)
    assert deploy(tmp_path, "--auto-approve")[0] == 0
    (tmp_path / "src" / "main.txt").write_text("broken\n")
    FakeBackend.image_id = "sha256:" + "3" * 64
    FakeBackend.passing[0] = False
    with receiver() as (url, requests):
        code, _events, result = deploy(
            tmp_path, "--auto-approve", "--json", "--otlp-endpoint", url
        )
        assert code == 1, result.stderr
        by_name = {s["name"]: s for s in spans(requests)}
        root = attrs(by_name["piceli.deploy"]["attributes"])
        assert root["piceli.outcome"] == "rolled-back"
        assert root["piceli.reason"] == "pipeline-checks-failed"
        assert by_name["piceli.deploy"]["status"]["code"] == 2
        checks = attrs(by_name["piceli.deploy.checks"]["attributes"])
        assert checks["piceli.stage.state"] == "failed"
        rollback = attrs(by_name["piceli.deploy.rollback"]["attributes"])
        assert rollback["piceli.stage.state"] == "ready"
        (record,) = logs(requests)
        assert record["severityText"] == "ERROR"


def test_dead_endpoint_does_not_fail_or_slow_the_deploy(shop, monkeypatch) -> None:
    _api, tmp_path = shop
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    started = time.monotonic()
    code, events, result = deploy(
        tmp_path, "--auto-approve", "--otlp-endpoint", f"http://127.0.0.1:{port}"
    )
    assert code == 0 and events[-1]["state"] == "ready"
    assert "deploy-events-export-failed" in result.stderr
    assert time.monotonic() - started < 20


def test_slow_endpoint_is_bounded(shop, monkeypatch) -> None:
    _api, tmp_path = shop
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "300")
    with receiver(delay=5) as (url, _requests):
        started = time.monotonic()
        code, events, result = deploy(
            tmp_path, "--auto-approve", "--otlp-endpoint", url
        )
        elapsed = time.monotonic() - started
    assert code == 0 and events[-1]["state"] == "ready"
    assert "deploy-events-export-failed" in result.stderr
    assert elapsed < 4.5


def test_error_status_and_bad_configuration_only_warn(shop, monkeypatch) -> None:
    _api, tmp_path = shop
    with receiver(status=500) as (url, _requests):
        code, _e, result = deploy(tmp_path, "--auto-approve", "--otlp-endpoint", url)
    assert code == 0 and "deploy-events-export-failed" in result.stderr
    code, _e, result = deploy(
        tmp_path, "--auto-approve", "--otlp-endpoint", f"http://user:{SECRET}@x"
    )
    assert code == 0 and "deploy-events-config-invalid" in result.stderr
    assert SECRET not in result.stderr


def test_resolution_follows_the_standard_environment() -> None:
    assert resolve(None, {}) is None
    assert (
        resolve(
            None,
            {
                "OTEL_SDK_DISABLED": "true",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318",
            },
        )
        is None
    )
    config = resolve(
        None,
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318/",
            "OTEL_EXPORTER_OTLP_HEADERS": "a=b,c=d",
            "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment=prod",
        },
    )
    assert config is not None
    assert config.traces_url == "http://collector:4318/v1/traces"
    assert config.logs_url == "http://collector:4318/v1/logs"
    assert config.headers == {"a": "b", "c": "d"}
    assert SECRET not in repr(config)
    explicit = resolve("http://x:1", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://y:2"})
    assert explicit is not None and explicit.traces_url == "http://x:1/v1/traces"
    for bad in (
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "ftp://x"},
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://x",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        },
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://x",
            "OTEL_EXPORTER_OTLP_HEADERS": "novalue",
        },
    ):
        with pytest.raises(DeployEventsConfigError):
            resolve(None, bad)


def test_release_apply_and_rollback_export_events(
    release_env,  # noqa: F811
    monkeypatch,
) -> None:
    _api, tmp_path = release_env
    with receiver() as (url, requests):
        code, applied, result = _run(
            tmp_path, "apply", "--auto-approve", "--otlp-endpoint", url
        )
        assert code == 0, result.output
        (root, *children) = spans(requests)
        assert root["name"] == "piceli.release"
        top = attrs(root["attributes"])
        assert top["piceli.release.id"] == applied["release"]
        assert top["piceli.outcome"] == "ready"
        assert top["piceli.release.intent"] == "apply"
        assert len(top["piceli.plan.hash"]) == 64
        assert top["k8s.namespace.name"] and top["piceli.app"]
        assert [c["name"] for c in children] == ["piceli.release.apply"]
        (record,) = logs(requests)
        assert record["body"] == {"stringValue": "piceli.release.ready"}
    _receipt(tmp_path / "build.receipt.json", DIGEST_2)
    assert _run(tmp_path, "apply", "--auto-approve")[0] == 0
    with receiver() as (url, requests):
        code, rolled, result = _run(
            tmp_path, "rollback", "previous", "--auto-approve", "--otlp-endpoint", url
        )
        assert code == 0, result.output
        top = attrs(spans(requests)[0]["attributes"])
        assert top["piceli.release.intent"] == "rollback"
        assert top["piceli.release.id"] == rolled["release"]


def test_release_dead_endpoint_only_warns(
    release_env,  # noqa: F811
    monkeypatch,
) -> None:
    _api, tmp_path = release_env
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{port}")
    code, _applied, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0 and "deploy-events-export-failed" in result.stderr
