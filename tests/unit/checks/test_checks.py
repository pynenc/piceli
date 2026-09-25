"""piceli.checks: typed declarations, the runner and the context (no cluster)."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from piceli.app import App
from piceli.checks import (
    CheckContext,
    CheckFailed,
    Checks,
    CheckSpecError,
    ExecResult,
    HttpCheck,
    parse_checks,
    run_checks,
)
from piceli.checks import context as context_module
from piceli.checks.context import CheckError, supervised_forward

DIGEST = "sha256:" + "1" * 64


# ----------------------------------------------------------------- models
def test_import_is_side_effect_free() -> None:
    code = (
        "import sys; import piceli.checks; "
        "bad = [m for m in sys.modules if m.startswith(('kubernetes', "
        "'piceli.k8s', 'piceli.app'))]; assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_app_handles_become_targets_with_their_ports() -> None:
    app = App("shop")
    web = app.deployment("web", image=f"registry.test/web@{DIGEST}", ports=[3000])
    service = app.service(web, port=80, target_port=3000)
    by_deployment = Checks.http(web, "/login", expect=200)
    assert (by_deployment.target, by_deployment.port) == ("deployment/web", 3000)
    assert by_deployment.expect == (200, 200)
    by_service = Checks.http(service, "/login")
    assert (by_service.target, by_service.port) == ("service/web", 80)
    assert by_service.expect == (200, 299)
    assert by_service.label == "http-service-web-login"
    exec_check = Checks.exec(web, ["true"])
    assert exec_check.target == "deployment/web"
    assert Checks.metric(service, "up", op=">=", threshold=1).port == 80


def test_toml_and_python_forms_are_equal() -> None:
    (from_toml,) = parse_checks(
        [
            {
                "type": "http",
                "target": "Service/web",
                "path": "/login",
                "expect": [200, 399],
                "retries": 0,
            }
        ]
    )
    assert from_toml == Checks.http(
        "service/web", "/login", expect=(200, 399), retries=0
    )
    public = from_toml.public_dict()
    assert public["name"] == "http-service-web-login"
    assert parse_checks([public]) == (
        from_toml.model_copy(update={"name": public["name"]}),
    )


@pytest.mark.parametrize(
    "item",
    [
        {"type": "http", "target": "job/migrate"},
        {"type": "http", "target": "service/web", "path": "login"},
        {"type": "http", "target": "service/web", "expect": 700},
        {"type": "exec", "target": "service/web", "command": ["true"]},
        {"type": "exec", "target": "deployment/web", "command": []},
        {"type": "metric", "target": "service/prom", "query": "up"},
        {"type": "python", "call": "not an entry"},
        {"type": "smoke"},
        {"type": "http", "target": "service/web", "unknown": 1},
    ],
)
def test_invalid_declarations_are_refused(item: dict[str, Any]) -> None:
    with pytest.raises(CheckSpecError) as caught:
        parse_checks([item])
    assert caught.value.code == "check-invalid"


def test_duplicate_names_are_refused() -> None:
    with pytest.raises(CheckSpecError, match="two checks are named"):
        parse_checks(
            [
                {"type": "http", "target": "service/web"},
                {"type": "http", "target": "service/web"},
            ]
        )
    with pytest.raises(CheckSpecError):
        Checks.exec("deployment/web", "true")  # type: ignore[arg-type]


# ----------------------------------------------------------------- runner
class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, bytes]] = {}

    def log_message(self, *_: Any) -> None:
        pass

    def do_GET(self) -> None:
        status, body = self.routes.get(self.path, (404, b"missing"))
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server() -> Iterator[tuple[dict[str, tuple[int, bytes]], str]]:
    routes: dict[str, tuple[int, bytes]] = {}
    handler = type("Handler", (_Handler,), {"routes": routes})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield routes, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _context(
    url: str = "http://127.0.0.1:1", *, forwards: list | None = None, **kwargs: Any
) -> CheckContext:
    @contextmanager
    def forwarder(ctx: CheckContext, target: str, port: int) -> Iterator[str]:
        if forwards is not None:
            forwards.append((target, port))
        yield url

    return CheckContext(
        Path("/nonexistent/kubeconfig"),
        "kind-shop",
        "shop",
        "shop-1",
        {"web": f"registry.test/web@{DIGEST}"},
        forwarder=forwarder,
        **kwargs,
    )


def test_http_check_passes_and_fails_with_codes(server) -> None:
    routes, url = server
    routes["/login"] = (200, b"<form>sign in</form>")
    routes["/broken"] = (500, b"oops")
    forwards: list = []
    checks = [
        Checks.http("service/web", "/login", port=80, body_contains="sign in"),
        Checks.http("service/web", "/broken", port=80, name="broken", retries=2),
        Checks.http(
            "service/web",
            "/login",
            port=80,
            body_contains="nope",
            retries=0,
            name="text",
        ),
    ]
    slept: list[float] = []
    report = run_checks(checks, _context(url, forwards=forwards), sleep=slept.append)
    assert not report.passed
    first, broken, missing_text = report.results
    assert first.passed and first.code is None and first.detail.endswith("200")
    assert not broken.passed and broken.code == "check-failed"
    assert broken.attempts == 3 and slept == [2.0, 2.0]
    assert "returned 500, expected 200-299" in broken.detail
    assert missing_text.code == "check-failed" and missing_text.attempts == 1
    assert forwards[0] == ("service/web", 80)
    value = report.to_dict()
    assert value["failed"] == ["broken", "text"]
    assert set(value["results"][0]) == {
        "name",
        "type",
        "passed",
        "detail",
        "duration",
        "attempts",
        "code",
    }


def test_fail_fast_and_empty_reports() -> None:
    assert run_checks(None, _context()).passed
    assert run_checks([], _context()).results == []
    failing = Checks.python(lambda ctx: False, retries=0, name="one")
    other = Checks.python(lambda ctx: True, name="two")
    report = run_checks([failing, other], _context(), fail_fast=True)
    assert [item.name for item in report.results] == ["one"]


def test_retry_until_pass() -> None:
    calls: list[int] = []

    def flaky(ctx: CheckContext) -> bool:
        calls.append(1)
        return len(calls) >= 3

    result = run_checks(Checks.python(flaky), _context(), sleep=lambda _: None)
    assert result.passed and result.results[0].attempts == 3


def test_python_check_outcomes(tmp_path: Path) -> None:
    def boom(ctx: CheckContext) -> None:
        raise RuntimeError("secret-looking detail")

    def failed(ctx: CheckContext) -> None:
        raise CheckFailed("login page missing")

    def context_sees(ctx: CheckContext) -> bool:
        return (
            ctx.namespace == "shop"
            and ctx.release == "shop-1"
            and ctx.images["web"].endswith(DIGEST)
        )

    (tmp_path / "checks.py").write_text("def smoke(ctx):\n    return 'from file'\n")
    checks = [
        Checks.python(context_sees, name="context", retries=0),
        Checks.python(boom, name="boom", retries=0),
        Checks.python(failed, name="failed", retries=0),
        Checks.python(lambda ctx: 3, name="odd", retries=0),
        Checks.python("checks.py:smoke", name="file", retries=0),
        Checks.python("no_such_module_xyz:run", name="missing", retries=5),
    ]
    report = run_checks(checks, _context(base=tmp_path))
    by_name = {item.name: item for item in report.results}
    assert by_name["context"].passed
    assert by_name["boom"].code == "check-raised"
    assert "secret-looking" not in by_name["boom"].detail
    assert (by_name["failed"].code, by_name["failed"].detail) == (
        "check-failed",
        "login page missing",
    )
    assert by_name["odd"].code == "check-raised"
    assert (by_name["file"].code, by_name["file"].detail) == (
        "check-failed",
        "from file",
    )
    assert by_name["missing"].code == "check-callable-invalid"
    assert by_name["missing"].attempts == 1  # never retried


def _prometheus(values: list[str]) -> bytes:
    return json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {}, "value": [0, value]} for value in values],
            },
        }
    ).encode()


def test_metric_check(server) -> None:
    routes, url = server
    ok = "/api/v1/query?query=error_rate"
    routes[ok] = (200, _prometheus(["0.01", "0.02"]))
    routes["/api/v1/query?query=bad"] = (200, _prometheus(["0.01", "0.5"]))
    routes["/api/v1/query?query=none"] = (200, _prometheus([]))
    routes["/api/v1/query?query=junk"] = (200, b"not json")
    routes["/api/v1/query?query=one"] = (
        200,
        json.dumps(
            {"status": "success", "data": {"resultType": "scalar", "result": [0, "1"]}}
        ).encode(),
    )

    def metric(query: str, **kwargs: Any) -> Any:
        return Checks.metric(
            "service/prom", query, port=9090, retries=0, name=query, **kwargs
        )

    report = run_checks(
        [
            metric("error_rate", op="<", threshold=0.05),
            metric("bad", op="<", threshold=0.05),
            metric("none", op="<", threshold=1),
            metric("none", op="<", threshold=1, empty="pass").model_copy(
                update={"name": "none-ok"}
            ),
            metric("junk", threshold=1),
            metric("one", op="==", threshold=1),
        ],
        _context(url),
    )
    by_name = {item.name: item for item in report.results}
    assert by_name["error_rate"].passed
    assert by_name["bad"].code == "check-failed"
    assert "1 of 2 samples" in by_name["bad"].detail
    assert by_name["none"].code == "check-failed"
    assert by_name["none-ok"].passed
    assert by_name["junk"].code == "check-metric-invalid"
    assert by_name["one"].passed


def test_exec_check_uses_the_executor() -> None:
    seen: list[tuple] = []

    def executor(ctx, target, command, container, timeout) -> ExecResult:
        seen.append((target, tuple(command), container, timeout))
        return ExecResult(0 if command[0] == "ok" else 3, stdout="ready\n")

    context = _context(executor=executor)
    report = run_checks(
        [
            Checks.exec("deployment/api", ["ok"], output_contains="ready"),
            Checks.exec("pod/api-0", ["fail"], container="api", retries=0),
            Checks.exec("deployment/api", ["ok"], expect_exit=1, retries=0, name="x"),
        ],
        context,
    )
    ok, failed, wrong = report.results
    assert ok.passed
    assert failed.code == "check-failed" and "exited 3, expected 0" in failed.detail
    assert wrong.code == "check-failed"
    assert seen[1] == ("pod/api-0", ("fail",), "api", 10.0)


# ------------------------------------------------------------- context
class _Response:
    def __init__(self, value: Any) -> None:
        self.data = json.dumps(value).encode()


class _Client:
    """Answers ``call_api`` from a path → object table (404 otherwise)."""

    def __init__(self, objects: dict[str, Any]) -> None:
        self.objects = objects
        self.calls: list[tuple[str, list]] = []

    def call_api(self, path: str, method: str, **kwargs: Any) -> Any:
        from kubernetes.client.exceptions import ApiException

        self.calls.append((path, kwargs.get("query_params") or []))
        if path not in self.objects:
            raise ApiException(status=404)
        return (_Response(self.objects[path]), 200, {})


def _pod(name: str, created: str, ready: bool = True) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "creationTimestamp": created},
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
    }


def test_context_resolves_ports_and_ready_pods() -> None:
    ns = "/api/v1/namespaces/shop"
    apps = "/apis/apps/v1/namespaces/shop"
    client = _Client(
        {
            f"{ns}/services/web": {"spec": {"ports": [{"port": 80}]}},
            f"{ns}/services/bare": {"spec": {}},
            f"{apps}/deployments/api": {
                "spec": {
                    "selector": {"matchLabels": {"app": "api"}},
                    "template": {
                        "spec": {"containers": [{"ports": [{"containerPort": 8080}]}]}
                    },
                }
            },
            f"{ns}/pods": {
                "items": [
                    _pod("api-old", "2026-01-01T00:00:00Z"),
                    _pod("api-new", "2026-01-02T00:00:00Z"),
                    _pod("api-starting", "2026-01-03T00:00:00Z", ready=False),
                ]
            },
        }
    )
    context = _context(api_client=client)
    assert context.resolve_port("service/web") == 80
    assert context.resolve_port("deployment/api") == 8080
    with pytest.raises(CheckError) as caught:
        context.resolve_port("service/bare")
    assert caught.value.code == "check-port-unknown"
    with pytest.raises(CheckError) as caught:
        context.resolve_port("service/absent")
    assert caught.value.code == "check-target-not-found"
    assert context.ready_pod("deployment/api") == "api-new"
    assert client.calls[-1][1] == [("labelSelector", "app=api")]
    context.close()  # an injected client stays open (caller-owned)


def test_context_needs_explicit_context_and_never_reads_ambient(tmp_path) -> None:
    with pytest.raises(ValueError):
        CheckContext(tmp_path / "kubeconfig", "", "shop", "shop-1")
    context = CheckContext(tmp_path / "missing", "kind-shop", "shop", "shop-1")
    with pytest.raises(CheckError) as caught:
        context.api_client()
    assert caught.value.code == "check-api-unavailable"
    # A plain target without a port needs the API: the failure is a result.
    report = run_checks(Checks.http("service/web", retries=0), context)
    assert report.results[0].code == "check-api-unavailable"


class _FakeSocket:
    """A ``v4.channel.k8s.io`` exec websocket replaying canned frames."""

    def __init__(self, frames: list[tuple[int, bytes]]) -> None:
        self.frames = list(frames)
        self.closed = False
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def recv_data(self) -> tuple[int, bytes]:
        import websocket

        if not self.frames:
            return websocket.ABNF.OPCODE_CLOSE, b""
        return self.frames.pop(0)

    def close(self) -> None:
        self.closed = True


def _frames(stdout: str, status: dict[str, Any]) -> list[tuple[int, bytes]]:
    import websocket

    binary = websocket.ABNF.OPCODE_BINARY
    half = len(stdout) // 2
    return [
        (binary, b"\x01" + stdout[:half].encode()),
        (binary, b"\x02"),
        (binary, b"\x01" + stdout[half:].encode()),
        (binary, b"\x03" + json.dumps(status).encode()),
    ]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"status": "Success"}, 0),
        (
            {
                "status": "Failure",
                "reason": "NonZeroExitCode",
                "details": {"causes": [{"reason": "ExitCode", "message": "7"}]},
            },
            7,
        ),
        ({"status": "Failure", "reason": "InternalError"}, None),
    ],
)
def test_api_exec_parses_the_exit_status(monkeypatch, status, expected) -> None:
    import piceli.checks.context as context_module

    captured: dict[str, Any] = {}
    socket_ = _FakeSocket(_frames("hello\n", status))

    def fake_open(client, namespace, pod, command, container, timeout):
        captured.update(
            namespace=namespace, pod=pod, command=command, container=container
        )
        return socket_

    monkeypatch.setattr(context_module, "open_exec_socket", fake_open)
    client = _Client({"/api/v1/namespaces/shop/pods/api-0": {"metadata": {}}})
    context = _context(api_client=client)
    if expected is None:
        with pytest.raises(CheckError) as caught:
            context.exec("pod/api-0", ["true"])
        assert caught.value.code == "check-exec-unavailable"
        assert socket_.closed
        return
    result = context.exec("pod/api-0", ["sh", "-c", "exit"], container="api")
    assert (result.exit_code, result.stdout) == (expected, "hello\n")
    assert captured["command"] == ["sh", "-c", "exit"]
    assert (captured["pod"], captured["namespace"]) == ("api-0", "shop")
    assert captured["container"] == "api"
    assert socket_.closed


def test_api_exec_times_out_on_a_silent_command() -> None:
    import websocket

    from piceli.checks.context import read_exec

    class Silent(_FakeSocket):
        def recv_data(self) -> tuple[int, bytes]:
            raise websocket.WebSocketTimeoutException("timed out")

    socket_ = Silent([])
    with pytest.raises(CheckError) as caught:
        read_exec(socket_, "pod/api-0", 5)
    assert caught.value.code == "check-timed-out"
    assert socket_.closed


def test_exec_socket_uses_the_clients_tls_context_and_auth(monkeypatch) -> None:
    """Factory clients keep TLS material in the pool, not in Configuration."""
    import ssl
    from types import SimpleNamespace

    import websocket

    from piceli.checks.context import open_exec_socket

    tls = ssl.create_default_context()
    configuration = SimpleNamespace(
        host="https://127.0.0.1:6443",
        tls_server_name="kubernetes",
        ssl_ca_cert=None,
        cert_file=None,
        key_file=None,
    )

    def update_params_for_auth(headers, _queries, _settings):
        headers["authorization"] = "Bearer not-a-real-token"

    client = SimpleNamespace(
        configuration=configuration,
        update_params_for_auth=update_params_for_auth,
        rest_client=SimpleNamespace(
            pool_manager=SimpleNamespace(connection_pool_kw={"ssl_context": tls})
        ),
    )
    captured: dict[str, Any] = {}

    def create_connection(url, **options):
        captured.update(options, url=url)
        return "socket"

    monkeypatch.setattr(websocket, "create_connection", create_connection)

    assert (
        open_exec_socket(client, "shop", "api-0", ["sh", "-c", "x y"], "api", 3)
        == "socket"
    )
    assert captured["url"] == (
        "wss://127.0.0.1:6443/api/v1/namespaces/shop/pods/api-0/exec?"
        "command=sh&command=-c&command=x+y&stdout=true&stderr=true&container=api"
    )
    assert captured["sslopt"] == {"context": tls, "server_hostname": "kubernetes"}
    assert captured["header"] == ["authorization: Bearer not-a-real-token"]
    assert captured["subprotocols"] == ["v4.channel.k8s.io"]
    assert captured["timeout"] == 3


FAKE_KUBECTL = """\
#!{python}
# Stands in for `kubectl port-forward`: serves HTTP on the requested local port.
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

args = sys.argv[1:]
assert args[args.index("--kubeconfig") + 1].endswith("check.kubeconfig"), args
assert args[args.index("--context") + 1] == "kind-shop", args
local = int(args[args.index("port-forward") + 2].split(":")[0])

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        body = ("forwarded " + args[args.index("port-forward") + 1]).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

HTTPServer(("127.0.0.1", local), Handler).serve_forever()
"""


def test_default_forwarder_supervises_a_kubectl_port_forward(tmp_path) -> None:
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(FAKE_KUBECTL.format(python=sys.executable))
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
    context = CheckContext(
        tmp_path / "check.kubeconfig",
        "kind-shop",
        "shop",
        "shop-1",
        kubectl=str(kubectl),
    )
    report = run_checks(
        Checks.http(
            "service/web", "/login", port=80, body_contains="forwarded service/web"
        ),
        context,
    )
    assert report.passed, report.to_dict()
    with supervised_forward(context, "deployment/web", 8080) as url:
        response = context_module.http_get(url, "/", timeout=5)
        assert response.text == "forwarded deployment/web"
    # The forward's process group is stopped afterwards.
    with pytest.raises(CheckError):
        context_module.http_get(url, "/", timeout=1)


def test_default_forwarder_reports_an_unhealthy_forward(tmp_path) -> None:
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(f"#!{sys.executable}\nimport sys; sys.exit(1)\n")
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
    context = CheckContext(
        tmp_path / "check.kubeconfig",
        "kind-shop",
        "shop",
        "shop-1",
        kubectl=str(kubectl),
        forward_seconds=1.0,
    )
    with pytest.raises(CheckError) as caught:
        with supervised_forward(context, "service/web", 80):
            pass
    assert caught.value.code == "check-forward-unavailable"


def test_http_check_model_is_frozen() -> None:
    check = HttpCheck(target="service/web")
    with pytest.raises(ValueError):
        check.path = "/other"  # type: ignore[misc]


def test_checks_example_parses_and_renders(tmp_path: Path) -> None:
    import shutil

    from piceli.k8s.release_spec import ImageRef, ReleaseSpec

    root = Path(__file__).resolve().parents[3] / "examples" / "checks"
    for name in ("composition.py", "checks.py", "release.toml"):
        shutil.copy(root / name, tmp_path / name)
    spec = ReleaseSpec.from_toml(tmp_path / "release.toml")
    assert spec.model.release.rollback_on_failed_checks is True
    assert [check.label for check in spec.model.checks] == [
        "login",
        "login-file",
        "login-python",
    ]
    assert callable(spec.model.checks[2].resolve(spec.base))  # type: ignore[union-attr]
    image = ImageRef("web", DIGEST, "registry.test/shop/web", None, DIGEST)
    composition = spec.load_composition()(spec.context({"web": image}, {}))
    kinds = sorted(
        resource.ref.kind
        for component in composition.components
        for resource in component.resources
    )
    assert kinds == ["Deployment", "Service"]


def test_stateful_set_and_daemon_set_handles_keep_their_kind() -> None:
    # Regression: every workload handle became "deployment/<name>", so an
    # exec check on a StatefulSet looked for a Deployment and failed.
    app = App("shop")
    image = f"registry.test/db@{DIGEST}"
    store = app.stateful_set("store", image=image, ports=[6379])
    agent = app.daemon_set("agent", image=image, ports=[9100])
    assert Checks.exec(store, ["true"]).target == "statefulset/store"
    assert Checks.exec(agent, ["true"]).target == "daemonset/agent"
    # A StatefulSet cannot be an HTTP target: refused, not silently retargeted.
    with pytest.raises(ValidationError, match="target must be one of"):
        Checks.http(store, "/")
    job = app.job("migrate", image=image)
    with pytest.raises(ValidationError, match="target must be one of"):
        Checks.exec(job, ["true"])
