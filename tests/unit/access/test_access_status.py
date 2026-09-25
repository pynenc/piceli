"""``piceli access`` / ``piceli status``: targets, health, conflicts (no cluster).

The live cluster is replaced by an in-memory :class:`FakeReader`; forwards are
supervised with a tiny local TCP relay standing in for ``kubectl
port-forward`` (it relays ``127.0.0.1:LOCAL`` to ``127.0.0.1:REMOTE``).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import jsonschema
import pytest
from typer.testing import CliRunner

from piceli.k8s import access as access_module
from piceli.k8s.access import (
    AccessTargetError,
    access_ui_config,
    collect_status,
    forward_status,
    port_conflicts,
    resolve_target,
    select_shortcuts,
    workload_status,
)
from piceli.k8s.cli import access as cli_access
from piceli.k8s.cli import app
from piceli.k8s.port_owner import PortOwner, ProcessInfo, is_piceli_forward
from piceli.k8s.ui_config import UiConfig, UiShortcut

REPO = Path(__file__).resolve().parents[3]
SCHEMA = json.loads(
    (REPO / "docs" / "schemas" / "piceli-status-v1.schema.json").read_text()
)
DIGEST = "sha256:" + "a" * 64
RUNNING = "sha256:" + "b" * 64
IMAGE = f"registry.example/shop/api@{DIGEST}"

FAKE_KUBECTL = textwrap.dedent(
    """\
    import re, socket, sys, threading

    ports = next(a for a in sys.argv[1:] if re.fullmatch(r"\\d+:\\d+", a))
    local, remote = (int(p) for p in ports.split(":"))

    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def handle(client):
        try:
            upstream = socket.create_connection(("127.0.0.1", remote), timeout=2)
        except OSError:
            client.close()
            return
        threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
        pump(upstream, client)

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", local))
    listener.listen(64)
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()
    """
)

APP_MODULE = textwrap.dedent(
    """\
    from piceli import App


    def build(ctx):
        app = App("shop")
        api = app.deployment("api", image=ctx.image("api"), ports=[8080])
        app.service(
            api,
            port=ctx.values["upstream"],
            target_port=8080,
            access=app.access.forward(
                local=ctx.values["local"], path="/", health="/healthz"
            ),
        )
        web = app.deployment("web", image=ctx.image("api"), ports=[3000])
        app.service(
            web,
            port=3000,
            access=app.access.forward(local=ctx.values["spare"], path="/login"),
        )
        app.deployment("worker", image=ctx.image("api"))
        return app


    def composition(ctx):
        return build(ctx).composition(ctx)
    """
)

KUBECONFIG = textwrap.dedent(
    """\
    apiVersion: v1
    kind: Config
    clusters: [{name: demo, cluster: {server: "https://127.0.0.1:1"}}]
    users: [{name: demo, user: {token: not-a-real-token}}]
    contexts: [{name: demo, context: {cluster: demo, user: demo, namespace: shop}}]
    """
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Healthz(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def upstream() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Healthz)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    yield int(server.server_address[1])
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def fake_kubectl(tmp_path: Path) -> Path:
    script = tmp_path / "fake-kubectl"
    script.write_text(f"#!{sys.executable}\n{FAKE_KUBECTL}")
    script.chmod(0o755)
    return script


def write_spec(
    root: Path,
    *,
    local: int,
    upstream: int,
    spare: int | None = None,
    entry: str = "build",
) -> Path:
    (root / "shop_app.py").write_text(APP_MODULE)
    (root / "kubeconfig").write_text(KUBECONFIG)
    spec = root / "release.toml"
    spec.write_text(
        textwrap.dedent(
            f"""
            [target]
            kubeconfig = "kubeconfig"
            context = "demo"
            namespace = "shop"

            [release]
            name = "shop"
            owner = "shop"
            field_manager = "shop"
            composition = "shop_app.py:{entry}"
            state_dir = "state"

            [images]
            api = "{IMAGE}"

            [values]
            local = {local}
            upstream = {upstream}
            spare = {spare or _free_port()}
            """
        )
    )
    return spec


# ------------------------------------------------------------ live fakes


def deployment(
    name: str,
    *,
    desired: int = 1,
    ready: int = 1,
    updated: int | None = None,
    current: int | None = None,
    generation: int = 2,
    observed: int = 2,
    image: str = IMAGE,
) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "generation": generation},
        "spec": {
            "replicas": desired,
            "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
            "template": {"spec": {"containers": [{"name": name, "image": image}]}},
        },
        "status": {
            "observedGeneration": observed,
            "readyReplicas": ready,
            "updatedReplicas": desired if updated is None else updated,
            "availableReplicas": ready,
            "replicas": desired if current is None else current,
        },
    }


def pod(
    name: str,
    app_name: str,
    *,
    image_id: str = f"registry.example/shop/api@{DIGEST}",
    waiting: str | None = None,
    restarts: int = 0,
) -> dict[str, Any]:
    status: dict[str, Any] = {"name": app_name, "imageID": image_id}
    status["restartCount"] = restarts
    if waiting:
        status["state"] = {"waiting": {"reason": waiting}}
    if restarts:
        status["lastState"] = {"terminated": {"reason": "Error"}}
    return {
        "metadata": {"name": name, "labels": {"app.kubernetes.io/name": app_name}},
        "status": {"phase": "Running", "containerStatuses": [status]},
    }


class FakeReader:
    def __init__(
        self,
        objects: Mapping[tuple[str, str], dict[str, Any]],
        pods: list[dict[str, Any]] = (),  # type: ignore[assignment]
        *,
        fail: set[str] = frozenset(),  # type: ignore[assignment]
    ) -> None:
        self.objects = dict(objects)
        self.pod_items = list(pods)
        self.fail = set(fail)
        self.closed = False

    def workload(self, kind: str, namespace: str, name: str) -> dict[str, Any] | None:
        assert namespace == "shop"
        if name in self.fail:
            raise RuntimeError("token=secret-do-not-print")
        return self.objects.get((kind, name))

    def pods(self, namespace: str, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        if "pods" in self.fail:
            raise RuntimeError("boom")
        return [
            item
            for item in self.pod_items
            if all(item["metadata"]["labels"].get(k) == v for k, v in labels.items())
        ]

    def close(self) -> None:
        self.closed = True


def healthy_reader() -> FakeReader:
    return FakeReader(
        {
            ("Deployment", "api"): deployment("api", desired=2, ready=2),
            ("Deployment", "web"): deployment("web"),
            ("Deployment", "worker"): deployment("worker"),
        },
        [
            pod("api-1", "api"),
            pod("api-2", "api"),
            pod("web-1", "web"),
            pod("worker-1", "worker"),
        ],
    )


# --------------------------------------------------------------- targets


def test_release_spec_target_reads_access_from_the_returned_app(tmp_path: Path) -> None:
    spec = write_spec(tmp_path, local=18090, upstream=80, spare=18091)
    target = resolve_target(str(spec))
    assert target.source == "release-spec"
    assert (target.name, target.namespace, target.context) == ("shop", "shop", "demo")
    assert target.kubeconfig == tmp_path / "kubeconfig"
    assert [
        (s.id, s.target, s.local_port, s.remote_port) for s in target.shortcuts
    ] == [
        ("api", "service/api", 18090, 80),
        ("web", "service/web", 18091, 3000),
    ]
    assert all(item.namespace == "shop" for item in target.shortcuts)
    assert target.workloads == (
        ("Deployment", "api"),
        ("Deployment", "web"),
        ("Deployment", "worker"),
    )
    assert target.spec is not None
    # Relative to the working directory, as typed on the command line.
    assert resolve_target("release.toml", tmp_path).name == "shop"


def test_a_composition_function_hides_access_but_keeps_workloads(
    tmp_path: Path,
) -> None:
    spec = write_spec(tmp_path, local=18090, upstream=80, entry="composition")
    target = resolve_target(str(spec))
    assert target.shortcuts == ()
    assert target.name == "shop"
    assert [name for _, name in target.workloads] == ["api", "web", "worker"]


def test_duck_typed_pipeline_target(tmp_path: Path) -> None:
    (tmp_path / "kc").write_text(KUBECONFIG)
    (tmp_path / "shop_pipeline.py").write_text(
        textwrap.dedent(
            f"""
            from types import SimpleNamespace
            from piceli import App

            app = App("shop")
            api = app.deployment("api", image="{IMAGE}", ports=[8080], node="primary")
            app.service(api, port=80, access=app.access.forward(local=18095))

            class Pipeline:
                app = app
                target = SimpleNamespace(kubeconfig="kc", context="demo", namespace="shop")

                def last_checks(self):
                    return {{"state": "passed", "checks": 2}}

            pipeline = Pipeline()
            no_target = SimpleNamespace(app=app)
            no_context = SimpleNamespace(
                app=app,
                target=SimpleNamespace(kubeconfig="kc", context=None, namespace="shop"),
            )
            """
        )
    )
    target = resolve_target("shop_pipeline.py:pipeline", tmp_path)
    assert target.source == "pipeline"
    assert target.kubeconfig == Path("kc")
    assert target.context == "demo"
    assert target.shortcuts[0].target == "service/api"
    # A node pin does not need verified nodes just to list workloads.
    assert target.workloads == (("Deployment", "api"),)
    assert target.checks is not None and target.checks()["state"] == "passed"
    for bad in ("no_target", "no_context", "app", "missing"):
        with pytest.raises(AccessTargetError) as raised:
            resolve_target(f"shop_pipeline.py:{bad}", tmp_path)
        assert raised.value.code == "access-target-invalid"


def test_invalid_targets(tmp_path: Path) -> None:
    for entry in ("missing.toml", "nomodule_xyz:thing", "no-colon"):
        with pytest.raises(AccessTargetError):
            resolve_target(entry, tmp_path)
    (tmp_path / "bad.toml").write_text("[target]\n")
    with pytest.raises(AccessTargetError, match="invalid release spec"):
        resolve_target("bad.toml", tmp_path)


def test_select_shortcuts(tmp_path: Path) -> None:
    target = resolve_target(str(write_spec(tmp_path, local=18090, upstream=80)))
    chosen, unknown = select_shortcuts(target.shortcuts, ["web", "nope"])
    assert [item.id for item in chosen] == ["web"] and unknown == ["nope"]
    assert select_shortcuts(target.shortcuts, None)[0] == target.shortcuts


# --------------------------------------------------------------- health


def test_workload_health_rules() -> None:
    assert workload_status("Deployment", "api", deployment("api"))["health"] == "ready"
    stale = deployment("api", generation=3, observed=2)
    assert workload_status("Deployment", "api", stale)["health"] == "progressing"
    rolling = deployment("api", desired=2, ready=2, current=3)
    assert workload_status("Deployment", "api", rolling)["health"] == "progressing"
    down = deployment("api", ready=0)
    assert workload_status("Deployment", "api", down)["health"] == "unavailable"
    idle = workload_status("Deployment", "api", deployment("api", desired=0, ready=0))
    assert (idle["health"], idle["ready"]) == ("idle", True)
    missing = workload_status("Deployment", "api", None)
    assert (missing["health"], missing["ready"]) == ("missing", False)
    daemon = {
        "metadata": {"generation": 1},
        "spec": {},
        "status": {
            "observedGeneration": 1,
            "desiredNumberScheduled": 2,
            "numberReady": 2,
            "updatedNumberScheduled": 2,
            "numberAvailable": 2,
            "currentNumberScheduled": 2,
        },
    }
    assert workload_status("DaemonSet", "agent", daemon)["health"] == "ready"


def test_images_digests_and_problems() -> None:
    tagged = deployment("api", image="registry.example/shop/api:v1")
    status = workload_status(
        "Deployment",
        "api",
        tagged,
        [
            pod("api-1", "api", image_id=f"registry.example/shop/api@{RUNNING}"),
            pod("api-2", "api", waiting="CrashLoopBackOff", restarts=3),
        ],
    )
    (image,) = status["images"]
    assert image["pinned"] is False
    assert image["running"] == sorted([DIGEST, RUNNING])
    assert image["digest"] is None  # two different digests are running
    assert status["problems"] == [
        "api-2/api: CrashLoopBackOff",
        "api-2/api: 3 restarts (last: Error)",
    ]
    pinned = workload_status("Deployment", "api", deployment("api"), [pod("a", "api")])
    assert pinned["images"][0]["digest"] == DIGEST
    single = workload_status(
        "Deployment", "api", tagged, [pod("a", "api", image_id=RUNNING)]
    )
    assert single["images"][0]["digest"] == RUNNING


def _shortcut(port: int, **extra: Any) -> UiShortcut:
    return UiShortcut(
        id="api",
        label="API",
        target="service/api",
        namespace="shop",
        local_port=port,
        remote_port=80,
        **extra,
    )


def _piceli_owner(port: int) -> PortOwner:
    return PortOwner(
        port,
        4242,
        f"/usr/bin/kubectl --kubeconfig /k --context kind-shop --namespace shop "
        f"port-forward service/api {port}:80 --address 127.0.0.1",
        ProcessInfo(4200, "/venv/bin/python /venv/bin/piceli access release.toml"),
    )


def test_forward_status_up_down_unhealthy(upstream: int) -> None:
    http = {"type": "http", "path": "/healthz"}
    up = forward_status(
        _shortcut(upstream, health=http), context="kind-shop", owner=_piceli_owner
    )
    assert up["forward"] == "up" and up["error"] is None
    assert up["owner"]["pid"] == 4242
    down = forward_status(_shortcut(_free_port()), context="kind-shop")
    assert down["forward"] == "down" and down["owner"] is None
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        busy = listener.getsockname()[1]
        bad = forward_status(
            _shortcut(busy, health={"type": "http", "path": "/", "timeout": 0.3}),
            context="kind-shop",
            owner=_piceli_owner,
        )
    assert bad["forward"] == "unhealthy"
    assert bad["owner"] == _piceli_owner(busy).to_dict()


def test_forward_status_never_reports_a_foreign_listener_as_up(
    upstream: int,
) -> None:
    """Any process answering on the port is not Piceli's forward (B8)."""
    http = {"type": "http", "path": "/healthz"}
    foreign = PortOwner(
        upstream,
        777,
        "kubectl --context other-project port-forward svc/x 18080:80",
        ProcessInfo(700, "make dev-secret-thing"),
    )
    for owner in (lambda port: foreign, lambda port: None):
        status = forward_status(
            _shortcut(upstream, health=http), context="kind-shop", owner=owner
        )
        assert status["forward"] == "occupied"
        assert status["error"] == "status-port-occupied"
        assert "other-project" not in json.dumps(status)
        assert "dev-secret-thing" not in json.dumps(status)
    assert status["owner"] is None
    status = forward_status(
        _shortcut(upstream, health=http), context="kind-shop", owner=lambda p: foreign
    )
    assert status["owner"] == {
        "port": upstream,
        "pid": 777,
        "command": None,
        "parent": None,
    }
    # The real lookup: the upstream is this test process, not a piceli forward.
    assert (
        forward_status(_shortcut(upstream, health=http), context="kind-shop")["forward"]
        == "occupied"
    )
    # Without a context nothing can be verified.
    assert (
        forward_status(_shortcut(upstream, health=http), owner=_piceli_owner)["forward"]
        == "occupied"
    )


def test_is_piceli_forward_matches_only_the_exact_argv() -> None:
    port = 18080
    owner = _piceli_owner(port)
    match = {
        "context": "kind-shop",
        "namespace": "shop",
        "target": "service/api",
        "local_port": port,
        "remote_port": 80,
    }
    assert is_piceli_forward(owner, **match)
    assert not is_piceli_forward(None, **match)
    for change in (
        {"context": "prod"},
        {"namespace": "other"},
        {"target": "service/web"},
        {"local_port": 18081},
        {"remote_port": 81},
    ):
        assert not is_piceli_forward(owner, **{**match, **change})
    for parent in (None, ProcessInfo(1, None), ProcessInfo(9, "bash ./dev.sh")):
        assert not is_piceli_forward(
            PortOwner(port, 4242, owner.command, parent), **match
        )
    assert is_piceli_forward(
        PortOwner(
            port, 4242, owner.command, ProcessInfo(9, "python -m piceli access t")
        ),
        **match,
    )
    assert not is_piceli_forward(
        PortOwner(
            port, 4242, "nc -l 18080 port-forward service/api 18080:80", owner.parent
        ),
        **match,
    )
    truncated = PortOwner(port, 4242, owner.command[:40] + "…", owner.parent)
    assert not is_piceli_forward(truncated, **match)


def test_human_status_never_prints_a_foreign_command_line() -> None:
    document = {
        "app": "shop",
        "state": "up",
        "namespace": "shop",
        "context": "kind-shop",
        "release": None,
        "workloads": [],
        "access": {
            "state": "down",
            "forwards": [
                {
                    "id": "web",
                    "forward": "occupied",
                    "url": "http://127.0.0.1:18080/",
                    "target": "service/web",
                    "remote_port": 3000,
                    "local_port": 18080,
                    "owner": {
                        "port": 18080,
                        "pid": 777,
                        "command": None,
                        "parent": None,
                    },
                }
            ],
        },
        "checks": None,
        "errors": [],
    }
    text = cli_access._human_status(document, "release.toml")
    assert "held by pid 777, not by piceli (status-port-occupied)" in text
    assert max(len(line) for line in text.splitlines()) <= 120


def test_collect_status_document(
    tmp_path: Path, upstream: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(access_module, "is_piceli_forward", lambda owner, **_: True)
    spec = write_spec(tmp_path, local=upstream, upstream=80)
    target = resolve_target(str(spec))
    document = collect_status(target, healthy_reader())
    jsonschema.validate(document, SCHEMA)
    assert document["state"] == "up"
    assert document["release"]["state"] == "not-deployed"
    assert [item["health"] for item in document["workloads"]] == ["ready"] * 3
    assert document["workloads"][0]["images"][0]["digest"] == DIGEST
    forwards = {item["id"]: item for item in document["access"]["forwards"]}
    assert forwards["api"]["forward"] == "up"
    assert forwards["web"]["forward"] == "down"
    assert document["access"]["state"] == "partial"
    assert document["checks"] is None and document["errors"] == []


def test_collect_status_degraded_down_and_unreadable(tmp_path: Path) -> None:
    target = resolve_target(str(write_spec(tmp_path, local=_free_port(), upstream=80)))
    reader = healthy_reader()
    reader.objects[("Deployment", "web")] = deployment("web", ready=0)
    assert collect_status(target, reader)["state"] == "degraded"
    assert collect_status(target, FakeReader({}))["state"] == "down"
    failing = healthy_reader()
    failing.fail = {"web"}
    document = collect_status(target, failing)
    jsonschema.validate(document, SCHEMA)
    assert document["state"] == "unknown"
    assert document["errors"] == ["status-cluster-unreadable"]
    assert "secret-do-not-print" not in json.dumps(document)
    pods_down = healthy_reader()
    pods_down.fail = {"pods"}
    document = collect_status(target, pods_down)
    assert document["state"] == "up"
    assert document["workloads"][0]["error"] == "status-cluster-unreadable"
    offline = collect_status(target, None, reader_error="access-kubeconfig-invalid")
    jsonschema.validate(offline, SCHEMA)
    assert offline["state"] == "unknown"
    assert {item["health"] for item in offline["workloads"]} == {"unknown"}


def test_release_state_and_checks_come_from_the_state_dir(tmp_path: Path) -> None:
    target = resolve_target(str(write_spec(tmp_path, local=_free_port(), upstream=80)))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (state / "history.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "at": "2026-09-24T10:00:00+00:00",
                        "release": "shop-0123456789ab",
                        "intent": "apply",
                        "mode": "create",
                        "execution_id": "e1",
                        "state": "ready",
                        "checks": {"state": "passed"},
                    }
                ],
            }
        )
    )
    document = collect_status(target, healthy_reader())
    jsonschema.validate(document, SCHEMA)
    assert document["release"]["state"] == "ready"
    assert document["release"]["current"] == "shop-0123456789ab"
    assert document["release"]["latest"]["intent"] == "apply"
    assert document["checks"] == {"state": "passed"}
    (state / "history.json").write_text("not json")
    broken = collect_status(target, healthy_reader())["release"]
    assert broken["error"] == "status-release-unreadable"


# ---------------------------------------------------------------- conflicts


def test_port_conflicts_name_the_owner() -> None:
    shortcut = UiShortcut(
        id="api", label="API", target="service/api", local_port=18090, remote_port=80
    )
    owner = PortOwner(18090, 4242, "kubectl port-forward service/api 18090:80")
    (conflict,) = port_conflicts(
        [shortcut], in_use=lambda port: True, owner=lambda port: owner
    )
    assert conflict.to_dict()["owner"]["pid"] == 4242
    assert "held by pid 4242 (kubectl port-forward" in conflict.describe()
    assert port_conflicts([shortcut], in_use=lambda port: False) == []


def test_dashboard_config_uses_the_model_shortcuts(tmp_path: Path) -> None:
    target = resolve_target(str(write_spec(tmp_path, local=18090, upstream=80)))
    config = access_ui_config(UiConfig(), target)
    assert [item.id for item in config.shortcuts] == ["api", "web"]
    (tier,) = config.tiers
    assert tier.name == "shop"
    assert [(c.name, c.shortcut) for c in tier.components] == [
        ("api", "api"),
        ("web", "web"),
        ("worker", None),
    ]
    override = UiShortcut(
        id="web", label="Web (TOML)", target="service/web", local_port=9, remote_port=1
    )
    merged = access_ui_config(UiConfig(shortcuts=(override,)), target)
    assert [(item.id, item.label) for item in merged.shortcuts] == [
        ("api", "api"),
        ("web", "Web (TOML)"),
    ]


# ---------------------------------------------------------------- CLI status

runner = CliRunner()


def test_cli_status_json_is_the_schema_and_exit_code_follows_state(
    tmp_path: Path, upstream: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(access_module, "is_piceli_forward", lambda owner, **_: True)
    spec = write_spec(tmp_path, local=upstream, upstream=80)
    reader = healthy_reader()
    seen: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeReader:
        seen.update(kwargs)
        return reader

    monkeypatch.setattr(cli_access, "_workload_reader", factory)
    result = runner.invoke(app, ["status", str(spec), "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    jsonschema.validate(document, SCHEMA)
    assert document["state"] == "up"
    assert seen["kubeconfig"] == tmp_path / "kubeconfig"
    assert seen["context"] == "demo"
    assert reader.closed
    reader.objects[("Deployment", "web")] = deployment("web", ready=0)
    result = runner.invoke(app, ["status", str(spec)])
    assert result.exit_code == 1
    text = result.stdout
    assert text.startswith("shop is DEGRADED  (namespace shop, context demo)")
    assert "unavailable  Deployment/web     0/1" in text  # columns aligned
    assert f"api={DIGEST[7:19]}" in text
    assert f"up        api          http://127.0.0.1:{upstream}/" in text
    assert f"Start the forwards with: piceli access {spec}" in text


def test_cli_status_with_an_unusable_kubeconfig(tmp_path: Path) -> None:
    spec = write_spec(tmp_path, local=_free_port(), upstream=80)
    (tmp_path / "kubeconfig").unlink()
    result = runner.invoke(app, ["status", str(spec), "--json"])
    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert document["errors"] == ["access-kubeconfig-invalid"]
    assert document["state"] == "unknown"


def test_cli_status_rejects_an_invalid_target(tmp_path: Path) -> None:
    result = runner.invoke(app, ["status", str(tmp_path / "missing.toml"), "--json"])
    assert result.exit_code == 2
    body = json.loads(result.stdout)
    assert (body["state"], body["reason"]) == ("rejected", "access-target-invalid")
    assert body["message"]


# ---------------------------------------------------------------- CLI access


def _access(*args: str) -> Any:
    return runner.invoke(app, ["access", *args])


def test_cli_access_rejections(tmp_path: Path, fake_kubectl: Path) -> None:
    spec = write_spec(tmp_path, local=_free_port(), upstream=80)
    kubectl = ("--kubectl", str(fake_kubectl))
    result = _access(str(spec), "--only", "nope", *kubectl)
    assert result.exit_code == 2
    body = json.loads(result.stdout)
    assert (body["state"], body["reason"]) == ("rejected", "access-unknown-forward")
    assert body["unknown"] == ["nope"] and body["message"]
    result = _access(str(spec), "--kubectl", str(tmp_path / "no-kubectl"))
    assert json.loads(result.stdout)["reason"] == "access-kubectl-missing"
    (tmp_path / "hidden").mkdir()
    hidden = write_spec(tmp_path / "hidden", local=1, upstream=80, entry="composition")
    result = _access(str(hidden), *kubectl)
    assert json.loads(result.stdout)["reason"] == "access-none-declared"
    (tmp_path / "kubeconfig").write_text(
        KUBECONFIG.replace("name: demo, context", "name: other, context")
    )
    result = _access(str(spec), *kubectl)
    assert json.loads(result.stdout)["reason"] == "access-kubeconfig-invalid"
    result = _access(str(tmp_path / "missing.toml"))
    assert json.loads(result.stdout)["reason"] == "access-target-invalid"


def test_cli_access_refuses_a_taken_port_and_names_its_owner(
    tmp_path: Path, fake_kubectl: Path
) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        busy = listener.getsockname()[1]
        spec = write_spec(tmp_path, local=busy, upstream=80)
        result = _access(str(spec), "--json", "--kubectl", str(fake_kubectl))
    assert result.exit_code == 2
    rejection = json.loads(result.stdout)
    assert rejection["state"] == "rejected"
    assert rejection["reason"] == "access-port-conflict"
    (conflict,) = rejection["conflicts"]
    assert conflict["id"] == "api" and conflict["local_port"] == busy
    if sys.platform.startswith("linux") or shutil.which("lsof"):
        assert conflict["owner"]["pid"] == os.getpid()
        assert f"held by pid {os.getpid()}" in result.stderr


def _read_events(
    process: subprocess.Popen[str], until: Any, timeout: float = 20
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            break
        events.append(json.loads(line))
        if until(events[-1]):
            break
    return events


def _get(url: str, token: str | None = None) -> Any:
    headers = {"X-Piceli-Local-Token": token} if token else {}
    with urlopen(Request(url, headers=headers), timeout=5) as response:
        return response.read().decode()


def test_cli_access_supervises_declared_forwards_and_serves_the_dashboard(
    tmp_path: Path, fake_kubectl: Path, upstream: int
) -> None:
    local, dashboard = _free_port(), _free_port()
    spec = write_spec(tmp_path, local=local, upstream=upstream)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "piceli",
            "access",
            str(spec),
            "--only",
            "api",
            "--json",
            "--kubectl",
            str(fake_kubectl),
            "--poll",
            "0.2",
            "--dashboard",
            str(dashboard),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO,
        env={**os.environ, "PICELI__UI_CONFIG": ""},
    )
    try:
        events = _read_events(process, lambda event: event.get("health") == "healthy")
        started = events[0]
        assert started["event"] == "started"
        assert started["forwards"] == [
            {
                "id": "api",
                "url": f"http://127.0.0.1:{local}/",
                "target": "service/api",
                "local_port": local,
                "remote_port": upstream,
            }
        ]
        assert started["dashboard"] == f"http://127.0.0.1:{dashboard}"
        assert events[-1]["health"] == "healthy", events
        assert _get(f"http://127.0.0.1:{local}/healthz") == "ok"
        page = _get(f"http://127.0.0.1:{dashboard}/")
        token = json.loads(page.split("const token = ", 1)[1].split(";", 1)[0])
        shortcuts = json.loads(
            _get(f"http://127.0.0.1:{dashboard}/v1/shortcuts", token)
        )
        assert [item["id"] for item in shortcuts["shortcuts"]] == ["api"]
        assert shortcuts["shortcuts"][0]["health"] == "healthy"
        # A second `piceli access` must not take the port over: it refuses and
        # names the owner (the relay started by the first one).
        second = _access(str(spec), "--only", "api", "--kubectl", str(fake_kubectl))
        assert second.exit_code == 2
        assert json.loads(second.stdout)["reason"] == "access-port-conflict"
    finally:
        process.send_signal(signal.SIGINT)
        try:
            out, _ = process.communicate(timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
    last = json.loads(out.strip().splitlines()[-1])
    assert last == {"event": "stopped", "state": "stopped"}
    assert process.returncode == 0
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and access_module.local_port_in_use(local):
        time.sleep(0.05)
    assert not access_module.local_port_in_use(local)  # no leftover relay


def test_human_status_timestamp_has_no_microseconds() -> None:
    from piceli.k8s.cli.access import _seconds

    assert _seconds("2026-09-25T06:46:30.456827+00:00") == "2026-09-25T06:46:30+00:00"
    assert _seconds("") == ""
    assert _seconds("not a time") == "not a time"


def test_deliver_line_shortens_the_digest() -> None:
    from piceli.pipeline.runner import _short_reference

    digest = "a" * 64
    assert _short_reference(f"127.0.0.1:5001/web@sha256:{digest}") == (
        "127.0.0.1:5001/web@sha256:aaaaaaaaaaaa…"
    )
    assert _short_reference("web:1.0") == "web:1.0"
