"""OpenTelemetry of the composition controller (in-memory exporters, no cluster).

Git, contracts, digests, triggers and the controller are real; builds and
deployments are fakes that write run journals like the pipeline runner.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from piceli.gitops import otel
from piceli.gitops.state import DirectoryChannel, approve_request, request
from piceli.infra.builders import BuildItem, BuiltImage, build_failed
from piceli.infra.composition import load_composition
from piceli.infra.controller import (
    CompositionConfig,
    CompositionController,
)
from piceli.infra.sources import SourceSet
from tests.unit.infra.git_support import Repo, example_files
from tests.unit.infra.test_controller import EXAMPLE, FakeBuilder

SECRET = "Bearer s3cr3t-telemetry-token"
DEPLOY_TAIL = "\n".join(f"step {n}: compiling" for n in range(1, 31))


def _iso(seconds: float) -> str:
    return (
        datetime.fromtimestamp(seconds, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class Crash(BaseException):
    """The process dies here (nothing after it runs)."""


class Builder(FakeBuilder):
    def __init__(self, clock: dict[str, float]) -> None:
        super().__init__()
        self.clock = clock
        self.fail: str | None = None  # "error" or "crash"

    def build(self, items: Sequence[BuildItem], checkout: Any) -> dict[str, BuiltImage]:
        self.clock["now"] += 30  # a build takes time
        if self.fail == "crash":
            raise Crash
        if self.fail == "error":
            error = RuntimeError("cargo failed")
            error.output_tail = DEPLOY_TAIL  # type: ignore[attr-defined]
            raise build_failed("the component build Job failed", error)
        return super().build(items, checkout)


class Ports:
    """Deploys by writing a run journal (stages with real start/finish times)."""

    def __init__(self, state: Path, clock: dict[str, float]) -> None:
        self.state = state
        self.clock = clock
        self.builder = Builder(clock)
        self.checks = "pass"  # "pass", "fail" (rolled back)
        self.prune = False
        self.timed = False  # the journal times rollback and prune (0.16)
        self.stopped: list[str] = []
        self.started: list[str] = []
        self.runs = 0

    def pipeline(self, composition: Any, env: Any, contracts: Any, images: Any) -> Any:
        return {"env": env.name, "images": dict(images)}

    def prepare_env(self, pipeline: Any, name: str) -> str:
        return f"ns-{name}"

    def env_up(self, pipeline: Any, name: str, **kwargs: Any) -> dict[str, Any]:
        if kwargs["approve"] is None:
            return {"state": "approval-required", "plan_hash": "sha256:" + "d" * 64}
        self.runs += 1
        run_id = f"20261004T10000{self.runs}000000Z-0000000{self.runs}"
        t0 = self.clock["now"]
        failing = self.checks == "fail"
        changes = [
            {"operation": "update", "kind": "Deployment", "name": "web"},
            {"operation": "no-op", "kind": "Deployment", "name": "api"},
            {"operation": "create", "kind": "StatefulSet", "name": "cache"},
        ]
        if self.prune:
            changes.append({"operation": "delete", "kind": "Deployment", "name": "old"})
        results = [
            {"name": "web-up", "type": "http", "passed": True, "detail": "200", "duration": 1.0},
            {
                "name": "api-up",
                "type": "http",
                "passed": not failing,
                "detail": "HTTP 503 from /healthz" if failing else "200",
                "duration": 2.0,
                **({"code": "check-failed"} if failing else {}),
            },
        ]  # fmt: skip
        checks: dict[str, Any] = {
            "state": "failed" if failing else "done",
            "started_at": _iso(t0 + 4),
            "finished_at": _iso(t0 + 9),
            "output": {"passed": not failing, "results": results},
        }
        if failing:
            checks["reason"] = "pipeline-checks-failed"
            checks["output"]["rollback"] = {"state": "ready", "release": "shop-r1"}
            if self.timed:
                checks["output"]["rollback"].update(
                    started_at=_iso(t0 + 7.5), finished_at=_iso(t0 + 8.5)
                )
        journal = {
            "schema": "piceli.deploy-run.v1",
            "run_id": run_id,
            "created_at": _iso(t0),
            "updated_at": _iso(t0 + 9),
            "state": "rolled-back" if failing else "ready",
            "stages": {
                "inputs": {"state": "skipped", "finished_at": _iso(t0)},
                "plan": {
                    "state": "done",
                    "started_at": _iso(t0),
                    "finished_at": _iso(t0 + 1),
                    "output": {"release": f"shop-r{self.runs + 1}", "changes": changes},
                },
                "apply": {
                    "state": "done",
                    "started_at": _iso(t0 + 1),
                    "finished_at": _iso(t0 + 4),
                    "output": {"release": f"shop-r{self.runs + 1}"},
                },
                "checks": checks,
            },
        }
        directory = self.state / "pipelines" / name / "runs"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{run_id}.json").write_text(json.dumps(journal))
        self.clock["now"] = t0 + 10
        if failing:
            return {
                "state": "failed",
                "reason": "pipeline-checks-failed",
                "result": {"run_id": run_id},
            }
        return {
            "state": "ready",
            "namespace": f"ns-{name}",
            "result": {
                "run_id": run_id,
                "stages": {"apply": "done"},
                **(
                    {"pruned": {"deleted": [{"kind": "Deployment", "name": "old"}]}}
                    if self.prune
                    else {}
                ),
            },
        }

    def env_down(self, pipeline: Any, name: str) -> None:
        pass

    def env_stop(self, pipeline: Any, name: str, reason: str = "idle") -> None:
        self.stopped.append(name)

    def env_start(self, pipeline: Any, name: str) -> None:
        self.started.append(name)


class Signals:
    def __init__(self) -> None:
        self.spans = InMemorySpanExporter()
        self.logs = InMemoryLogRecordExporter()
        self.readers: list[InMemoryMetricReader] = []

    @property
    def reader(self) -> InMemoryMetricReader:
        return self.readers[-1]

    def telemetry(
        self, state: Path, clock: dict[str, float], log: Any = None
    ) -> otel.ControllerTelemetry:
        providers = otel.build_providers(
            otel.resource_attributes(
                version="0.16.0", cluster_uid="uid-1", cluster_name="my-cluster"
            ),
            span_exporter=self.spans,
            log_exporter=self.logs,
            metric_reader=self._reader(),
            batch=False,
        )
        return otel.ControllerTelemetry(
            state,
            providers,
            cluster="my-cluster",
            composition="shop",
            version="0.16.0",
            clock=lambda: clock["now"],
            log=log or (lambda _m: None),
        )

    def _reader(self) -> InMemoryMetricReader:
        self.readers.append(InMemoryMetricReader())
        return self.readers[-1]

    def roots(self) -> list[Any]:
        return [s for s in self.spans.get_finished_spans() if s.parent is None]

    def tree(self, root: Any) -> dict[str, Any]:
        return {
            s.name: s
            for s in self.spans.get_finished_spans()
            if s.context.trace_id == root.context.trace_id
        }

    def events(self, name: str | None = None) -> list[Any]:
        found = [item.log_record for item in self.logs.get_finished_logs()]
        return [r for r in found if name is None or r.event_name == name]

    def metrics(self) -> dict[str, list[Any]]:
        data = self.reader.get_metrics_data()
        found: dict[str, list[Any]] = {}
        for resource in data.resource_metrics if data else []:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    found.setdefault(metric.name, []).extend(metric.data.data_points)
        return found


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    shop = Repo(tmp_path / "remotes", "shop", example_files(EXAMPLE / "shop"))
    catalog = Repo(tmp_path / "remotes", "catalog", example_files(EXAMPLE / "catalog"))
    catalog.tag("v0.1.0")
    monkeypatch.setenv("COMPOSITION_SHOP_URL", shop.url)
    monkeypatch.setenv("COMPOSITION_CATALOG_URL", catalog.url)
    composition = load_composition(EXAMPLE / "infra.py")
    config = CompositionConfig(composition=composition.to_dict(), poll_seconds=10)
    state = tmp_path / "state"
    clock = {"now": 1_790_000_000.0}
    ports = Ports(state, clock)
    channel = DirectoryChannel(state)
    signals = Signals()
    logs: list[str] = []

    def make() -> CompositionController:
        return CompositionController(
            config,
            state_dir=state,
            sources=SourceSet(composition.sources, state / "sources"),
            ports=ports,
            channel=channel,
            clock=lambda: clock["now"],
            log=logs.append,
            telemetry=signals.telemetry(state, clock, logs.append),
        )

    controller = make()
    yield {
        "shop": shop,
        "controller": controller,
        "make": make,
        "ports": ports,
        "channel": channel,
        "clock": clock,
        "signals": signals,
        "state": state,
        "logs": logs,
    }
    controller.telemetry.shutdown(1.0)


def _manual(controller: CompositionController) -> None:
    from piceli.envs import Environment

    composition = controller.composition
    main = composition.environment("main")
    manual = Environment(
        "main",
        namespace=main.namespace,
        stack=main.stack,
        follow={source: list(items) for source, items in main.sources},
    )
    object.__setattr__(
        composition, "environments", (manual, *composition.environments[1:])
    )


def _main_root(signals: Signals) -> Any:
    roots = [r for r in signals.roots() if r.name == "SYNC shop/main"]
    assert len(roots) == 1, [r.name for r in signals.roots()]
    return roots[0]


def _child_of(span: Any, parent: Any) -> bool:
    return (
        span.parent is not None
        and span.parent.span_id == parent.context.span_id
        and span.context.trace_id == parent.context.trace_id
    )


# ------------------------------------------------------------------ traces


def test_a_successful_deploy_is_one_trace_with_a_span_per_stage(
    world: dict[str, Any],
) -> None:
    world["ports"].prune = True
    status = world["controller"].poll_once()
    assert status["envs"]["main"]["state"] == "deployed"
    signals: Signals = world["signals"]
    root = _main_root(signals)
    tree = signals.tree(root)
    attrs = dict(root.attributes)
    assert root.kind.name == "SERVER" and root.status.status_code.name == "OK"
    assert attrs["cicd.pipeline.name"] == "shop/main"
    assert attrs["cicd.pipeline.action.name"] == "SYNC"
    assert attrs["cicd.pipeline.result"] == "success"
    assert attrs["piceli.deploy.result"] == "success"
    assert attrs["deployment.environment.name"] == "main"
    assert attrs["deployment.status"] == "succeeded"
    assert attrs["piceli.trigger.kind"] == "push"
    assert attrs["piceli.approval.via"] == "policy"
    assert attrs["vcs.repository.name"] == "catalog"
    assert attrs["vcs.ref.head.name"] == "main"
    assert attrs["vcs.ref.head.type"] == "branch"
    assert list(attrs["piceli.sources"]) == ["catalog", "shop"]
    revision = status["envs"]["main"]["revision"]
    assert list(attrs["piceli.source.revisions"]) == [
        revision["catalog"],
        revision["shop"],
    ]
    assert attrs["vcs.ref.head.revision"] == revision["catalog"]
    assert attrs["k8s.namespace.name"] == "ns-main"
    assert attrs["k8s.deployment.name"] == "web"
    assert list(attrs["piceli.k8s.deployments"]) == ["web"]
    assert attrs["k8s.statefulset.name"] == "cache"
    assert attrs["piceli.release.id"] == "shop-r2"
    resource = dict(root.resource.attributes)
    assert resource["service.name"] == "piceli-controller"
    assert resource["service.version"] == "0.16.0"
    assert resource["k8s.cluster.uid"] == "uid-1"
    assert resource["k8s.cluster.name"] == "my-cluster"
    for image in ("web", "api", "catalog", "cache"):
        build = tree[f"build {image}"]
        assert _child_of(build, root)
        assert build.attributes["cicd.pipeline.task.type"] == "build"
        assert build.attributes["piceli.image.digest"].startswith("sha256:")
        assert build.end_time - build.start_time == 30 * 10**9 or image == "cache"
    for stage in ("plan", "apply", "checks"):
        assert _child_of(tree[stage], root), stage
        assert tree[stage].attributes["cicd.pipeline.task.run.result"] == "success"
    assert _child_of(tree["prune"], tree["apply"])
    assert list(tree["prune"].attributes["piceli.pruned"]) == ["Deployment/old"]
    checks = tree["checks"]
    web, api = tree["check web-up"], tree["check api-up"]
    assert _child_of(web, checks) and _child_of(api, checks)
    assert web.start_time == checks.start_time
    assert web.end_time - web.start_time == 10**9
    assert api.start_time == web.end_time
    # Real times: the journal's plan stage, not the controller's clock.
    plan = tree["plan"]
    assert plan.end_time - plan.start_time == 10**9
    assert root.start_time <= min(s.start_time for s in tree.values())
    assert root.end_time >= max(s.end_time for s in tree.values())
    rolled = signals.events("piceli.deploy.rolled")
    assert len(rolled) == 1
    assert rolled[0].trace_id == root.context.trace_id
    assert rolled[0].span_id == tree["apply"].context.span_id
    assert "web" in rolled[0].attributes["piceli.components"]
    assert signals.events("piceli.deploy.pruned")[0].attributes["piceli.pruned"] == (
        "Deployment/old",
    )
    approved = signals.events("piceli.deploy.approved")
    assert [e.attributes["piceli.approval.via"] for e in approved] == ["policy"]
    started = signals.events("piceli.controller.started")
    assert started and started[0].attributes["piceli.controller.version"] == "0.16.0"


def test_failed_checks_with_rollback(world: dict[str, Any]) -> None:
    world["ports"].checks = "fail"
    world["controller"].poll_once()
    signals: Signals = world["signals"]
    root = _main_root(signals)
    tree = signals.tree(root)
    assert root.status.status_code.name == "ERROR"
    assert root.attributes["piceli.deploy.result"] == "rolled-back"
    assert root.attributes["cicd.pipeline.result"] == "failure"
    assert root.attributes["error.type"] == "pipeline-checks-failed"
    assert tree["checks"].status.status_code.name == "ERROR"
    failing = tree["check api-up"]
    assert failing.status.status_code.name == "ERROR"
    assert failing.attributes["piceli.check.detail"] == "HTTP 503 from /healthz"
    assert failing.attributes["error.type"] == "check-failed"
    assert tree["check web-up"].status.status_code.name == "OK"
    rollback = tree["rollback"]
    assert _child_of(rollback, root)
    assert rollback.start_time == failing.end_time
    assert rollback.end_time == tree["checks"].end_time
    assert rollback.attributes["piceli.rollback.to"] == "shop-r1"
    assert rollback.attributes["piceli.rollback.from"] == "shop-r2"
    failed = signals.events("piceli.deploy.checks.failed")[0]
    assert failed.attributes["piceli.checks.failed"] == ("api-up",)
    assert "HTTP 503" in failed.attributes["piceli.checks.detail"][0]
    assert failed.span_id == tree["checks"].context.span_id
    back = signals.events("piceli.deploy.rollback")[0]
    assert back.attributes["piceli.rollback.from"] == "shop-r2"
    assert back.attributes["piceli.rollback.to"] == "shop-r1"
    assert back.span_id == rollback.context.span_id
    points = signals.metrics()["piceli.check.runs"]
    results = {
        (p.attributes["piceli.check.name"], p.attributes["piceli.check.result"])
        for p in points
    }
    assert ("api-up", "failed") in results and ("web-up", "passed") in results
    errors = signals.metrics()["cicd.pipeline.run.errors"]
    assert errors[0].attributes["error.type"] == "pipeline-checks-failed"


def test_a_build_failure_closes_the_run_with_the_log_tail(
    world: dict[str, Any],
) -> None:
    world["ports"].builder.fail = "error"
    status = world["controller"].poll_once()
    assert status["envs"]["main"]["state"] == "retrying"
    signals: Signals = world["signals"]
    root = _main_root(signals)
    tree = signals.tree(root)
    assert root.attributes["piceli.deploy.result"] == "failure"
    assert root.attributes["error.type"] == "component-build-failed"
    build = tree["build web"]
    assert build.status.status_code.name == "ERROR"
    assert build.end_time - build.start_time == 30 * 10**9
    event = signals.events("piceli.build.failed")[0]
    tail = event.attributes["piceli.build.log_tail"].splitlines()
    # 0.17.0: up to 80 lines, so all 30 of this log (20 before).
    assert len(tail) == 30 and tail[-1] == "step 30: compiling"
    assert event.trace_id == root.context.trace_id
    # The retry is a run of its own (attempt 2).
    world["ports"].builder.fail = None
    world["clock"]["now"] += 100
    world["controller"].poll_once()
    roots = [r for r in signals.roots() if r.name == "SYNC shop/main"]
    assert [r.attributes["piceli.run.attempt"] for r in roots] == [1, 2]
    assert roots[1].attributes["piceli.deploy.result"] == "success"
    assert roots[0].context.trace_id != roots[1].context.trace_id


def test_an_approval_wait_is_part_of_the_same_trace(world: dict[str, Any]) -> None:
    controller, signals = world["controller"], world["signals"]
    _manual(controller)
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "approval-required"
    assert not [r for r in signals.roots() if r.name == "SYNC shop/main"]
    required = signals.events("piceli.deploy.approval.required")[0]
    world["clock"]["now"] += 600  # the owner takes ten minutes
    plan = status["envs"]["main"]["plan_hash"]
    world["channel"].add_request(*approve_request("main", plan, via="cli"))
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "deployed"
    root = _main_root(signals)
    tree = signals.tree(root)
    wait = tree["approval wait"]
    assert _child_of(wait, root)
    assert wait.end_time - wait.start_time == 600 * 10**9
    assert wait.attributes["piceli.approval.via"] == "cli"
    assert root.attributes["piceli.approval.via"] == "cli"
    assert required.trace_id == root.context.trace_id
    assert required.span_id == wait.context.span_id
    approved = signals.events("piceli.deploy.approved")[0]
    assert approved.attributes["piceli.approval.via"] == "cli"
    assert approved.span_id == wait.context.span_id
    assert tree["build web"].start_time < wait.start_time < tree["plan"].start_time
    waits = signals.metrics()["piceli.deploy.approval.wait"]
    assert waits[0].sum == pytest.approx(600)
    durations = {
        p.attributes["cicd.pipeline.run.state"]: p.sum
        for p in signals.metrics()["cicd.pipeline.run.duration"]
    }
    assert durations["pending"] == pytest.approx(600)


def test_stop_and_start_have_their_traces_and_events(world: dict[str, Any]) -> None:
    controller, channel, signals = (
        world["controller"],
        world["channel"],
        world["signals"],
    )
    controller.poll_once()
    channel.add_request(
        *request("stop", env="main", via="cli", at=_iso(world["clock"]["now"]))
    )
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "stopped"
    stopped = signals.events("piceli.environment.stopped")[0]
    assert stopped.attributes["piceli.stop.reason"] == "requested"
    stop = next(r for r in signals.roots() if r.name == "STOP shop/main")
    assert stopped.trace_id == stop.context.trace_id
    world["clock"]["now"] += 60
    channel.add_request(
        *request("start", env="main", via="cli", at=_iso(world["clock"]["now"]))
    )
    controller.poll_once()
    started = signals.events("piceli.environment.started")[0]
    start = next(r for r in signals.roots() if r.name == "START shop/main")
    assert started.trace_id == start.context.trace_id
    assert start.status.status_code.name == "OK"
    assert world["ports"].stopped == ["main"] and world["ports"].started == ["main"]


def test_a_restart_mid_run_closes_it_interrupted_in_the_same_trace(
    world: dict[str, Any],
) -> None:
    controller, signals = world["controller"], world["signals"]
    world["ports"].builder.fail = "crash"
    controller.telemetry.step_finished = lambda *a, **k: None  # the process died
    with pytest.raises(Crash):
        controller.poll_once()
    stored = json.loads((world["state"] / "telemetry" / "runs.json").read_text())
    run = stored["runs"]["main"]
    assert run["in_step"] is True
    controller.telemetry.heartbeat()
    beat = int((world["state"] / "telemetry" / "heartbeat").read_text())
    assert not [r for r in signals.roots() if r.name == "SYNC shop/main"]
    world["ports"].builder.fail = None
    world["clock"]["now"] += 120
    world["make"]()  # the controller starts again on the same volume
    root = _main_root(signals)
    assert f"{root.context.trace_id:032x}" == run["trace_id"]
    assert root.context.trace_id == otel.trace_id_of("my-cluster", "main", run["run"])
    assert root.attributes["piceli.deploy.result"] == "interrupted"
    assert root.attributes["cicd.pipeline.result"] == "error"
    assert root.end_time == max(beat, run["step_started_ns"])
    build = signals.tree(root)["build web"]
    assert build.attributes["cicd.pipeline.task.run.result"] == "error"
    interrupted = signals.events("piceli.deploy.interrupted")[0]
    assert interrupted.trace_id == root.context.trace_id
    assert interrupted.attributes["piceli.event.id"] == (
        f"my-cluster:main/{run['run']}:interrupted"
    )
    stored = json.loads((world["state"] / "telemetry" / "runs.json").read_text())
    assert stored["runs"] == {}
    assert len(signals.events("piceli.controller.started")) == 2


def test_ids_are_stable_and_events_carry_them(world: dict[str, Any]) -> None:
    world["controller"].poll_once()
    signals: Signals = world["signals"]
    root = _main_root(signals)
    run = root.attributes["cicd.pipeline.run.id"].split("/", 1)[1]
    assert root.context.trace_id == otel.trace_id_of("my-cluster", "main", run)
    tree = signals.tree(root)
    assert tree["plan"].context.span_id == otel.span_id_of(
        root.context.trace_id, "s1/plan"
    )
    ids = [e.attributes["piceli.event.id"] for e in signals.events()]
    assert len(ids) == len(set(ids))
    rolled = signals.events("piceli.deploy.rolled")[0]
    assert rolled.attributes["piceli.event.id"] == f"my-cluster:main/{run}:rolled:1"
    # The same run rebuilt (a resend) has the same ids.
    assert otel.trace_id_of("my-cluster", "main", run) == root.context.trace_id


def test_metrics_cover_runs_builds_workers_and_heartbeat(
    world: dict[str, Any],
) -> None:
    world["controller"].poll_once()
    world["controller"].telemetry.heartbeat()
    found = world["signals"].metrics()
    for name in (
        "cicd.pipeline.run.duration",
        "cicd.pipeline.run.active",
        "cicd.worker.count",
        "piceli.build.duration",
        "piceli.check.runs",
        "piceli.deploy.last_success.age",
        "piceli.deploy.queue.wait",
        "piceli.deploy.queue.size",
        "piceli.controller.heartbeat",
        "piceli.controller.heartbeats",
    ):
        assert found.get(name), name
    active = {
        p.attributes["cicd.pipeline.run.state"]: p.value
        for p in found["cicd.pipeline.run.active"]
    }
    assert set(active.values()) == {0}
    workers = {
        p.attributes["cicd.worker.state"]: p.value for p in found["cicd.worker.count"]
    }
    assert workers == {"available": 1, "busy": 0}
    builds = {
        p.attributes["piceli.image"]: p.sum for p in found["piceli.build.duration"]
    }
    assert builds["web"] == pytest.approx(30)
    ages = {
        p.attributes["deployment.environment.name"]
        for p in found["piceli.deploy.last_success.age"]
    }
    assert "main" in ages


def test_the_heartbeat_ticks_while_a_step_runs(tmp_path: Path) -> None:
    signals = Signals()
    clock = {"now": 1_790_000_000.0}
    telemetry = signals.telemetry(tmp_path, clock)
    telemetry.start_heartbeat(0.01)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            points = signals.metrics().get("piceli.controller.heartbeats") or []
            if points and points[0].value >= 3:
                break
            time.sleep(0.02)  # the main thread is busy (a long build)
        assert points and points[0].value >= 3
        assert (tmp_path / "telemetry" / "heartbeat").is_file()
    finally:
        assert telemetry.shutdown(2.0)


# ------------------------------------------------------------------ failure


@pytest.fixture
def black_hole() -> Iterator[str]:
    """A TCP endpoint that accepts and never answers (an unreachable collector)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(64)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                held.append(server.accept()[0])
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}"
    finally:
        stop.set()
        thread.join(2)
        for item in held:
            item.close()
        server.close()


@pytest.mark.parametrize("protocol", ["grpc", "http/protobuf"])
def test_an_unreachable_endpoint_never_slows_or_fails_a_deploy(
    world: dict[str, Any], black_hole: str, protocol: str
) -> None:
    settings = otel.Settings(
        endpoint=black_hole, protocol=protocol, insecure=True, timeout=10.0
    )
    providers = otel.otlp_providers(
        settings, otel.resource_attributes(version="0.16.0")
    )
    clock = world["clock"]
    telemetry = otel.ControllerTelemetry(
        world["state"],
        providers,
        cluster="my-cluster",
        composition="shop",
        clock=lambda: clock["now"],
    )
    controller = world["make"]()
    controller.telemetry.shutdown(1.0)
    controller.telemetry = telemetry
    telemetry.attach(controller)
    try:
        world["ports"].checks = "fail"
        began = time.monotonic()
        status = controller.poll_once()
        world["ports"].checks = "pass"
        world["shop"].commit({"web/index.html": "<h1>v2</h1>\n"})
        clock["now"] += 100
        controller.state["envs"]["main"]["next_attempt_at"] = None
        status = controller.poll_once()
        took = time.monotonic() - began
        assert status["envs"]["main"]["state"] == "deployed"
        # Exports wait in the background: one blocking export alone would
        # take the 10 s timeout (two runs, dozens of spans and records).
        assert took < 5, took
        processor = providers.tracer_provider._active_span_processor._span_processors[0]
        queue = processor._batch_processor._queue
        assert queue.maxlen == otel.MAX_QUEUE
    finally:
        assert telemetry.shutdown(1.0) in (True, False)  # bounded either way


def test_the_queue_stays_bounded_while_the_endpoint_is_down(
    tmp_path: Path, black_hole: str
) -> None:
    providers = otel.build_providers(
        otel.resource_attributes(version="0.16.0"),
        span_exporter=otel._exporters(
            otel.Settings(
                endpoint=black_hole,
                protocol="http/protobuf",
                insecure=True,
                timeout=1.0,
            )
        )[0],
        log_exporter=InMemoryLogRecordExporter(),
        metric_reader=InMemoryMetricReader(),
        max_queue=64,
        schedule_delay=0.05,
    )
    tracer = providers.tracer_provider.get_tracer("t")
    try:
        began = time.monotonic()
        for _ in range(5000):
            tracer.start_span("x").end()
        assert time.monotonic() - began < 2
        processor = providers.tracer_provider._active_span_processor._span_processors[0]
        assert len(processor._batch_processor._queue) <= 64
    finally:
        providers.shutdown(1.0)


def test_secrets_never_reach_attributes_or_logs(
    world: dict[str, Any], tmp_path: Path
) -> None:
    headers = tmp_path / "headers"
    headers.mkdir()
    (headers / "authorization").write_text(SECRET + "\n")
    settings = otel.resolve(
        {
            "endpoint": "https://collector.example:4317",
            "protocol": "grpc",
            "headers_secret": "otlp-auth",
        },
        {},
        headers_dir=headers,
    )
    assert settings is not None and settings.headers == {"authorization": SECRET}
    assert SECRET not in repr(settings)
    world["ports"].builder.fail = "error"
    world["controller"].poll_once()
    signals: Signals = world["signals"]
    exported = [
        json.dumps(dict(s.attributes), default=str)
        for s in signals.spans.get_finished_spans()
    ] + [item.to_json() for item in signals.logs.get_finished_logs()]
    assert exported and not any(SECRET in item for item in exported)
    assert not any(SECRET in line for line in world["logs"])


def test_a_bad_configuration_turns_telemetry_off_with_one_line(tmp_path: Path) -> None:
    lines: list[str] = []
    telemetry = otel.for_controller(
        tmp_path,
        {"endpoint": "ftp://collector", "protocol": "grpc"},
        cluster=None,
        composition=None,
        version="0.16.0",
        log=lines.append,
        environ={},
    )
    assert not telemetry.enabled
    assert len(lines) == 1 and lines[0].startswith("telemetry: off")


def test_the_environment_variables_are_the_fallback() -> None:
    assert otel.resolve(None, {}) is None
    found = otel.resolve(None, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"})
    assert found is not None and found.from_environment
    assert found.protocol == "http/protobuf"
    grpc = otel.resolve(
        None,
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        },
    )
    assert grpc is not None and grpc.protocol == "grpc"
    off = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://x:4317", "OTEL_SDK_DISABLED": "true"}
    assert otel.resolve({"endpoint": "https://x:4317"}, off) is None


def test_disabled_telemetry_changes_nothing(
    world: dict[str, Any], tmp_path: Path
) -> None:
    disabled = otel.ControllerTelemetry.disabled(tmp_path)
    for hook in (
        lambda: disabled.step_started({"branch": "main"}, "deploy"),
        lambda: disabled.build_started(["web"]),
        lambda: disabled.built("web", "sha256:x"),
        lambda: disabled.approved({"branch": "main"}, "cli"),
        lambda: disabled.step_finished({"branch": "main"}, "deploy"),
        disabled.heartbeat,
    ):
        hook()
    assert not (tmp_path / "telemetry").exists()
    assert disabled.shutdown(0.1)


def test_a_failing_hook_never_fails_the_step(world: dict[str, Any]) -> None:
    telemetry = world["controller"].telemetry

    def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("boom")

    telemetry._spans = broken
    status = world["controller"].poll_once()
    assert status["envs"]["main"]["state"] == "deployed"
    assert telemetry.failures >= 1
    assert sum("telemetry: step end skipped" in line for line in world["logs"]) == 1


def test_journal_times_of_rollback_and_prune_win(world: dict[str, Any]) -> None:
    world["ports"].checks = "fail"
    world["ports"].timed = True
    world["controller"].poll_once()
    signals: Signals = world["signals"]
    root = _main_root(signals)
    rollback = signals.tree(root)["rollback"]
    assert rollback.end_time - rollback.start_time == 10**9
    assert rollback.start_time == otel._iso_ns(_iso(1_790_000_000.0 + 30 + 7.5))


def test_a_deploy_emits_start_stage_and_end_events(world: dict[str, Any]) -> None:
    """IH draws these as changes on its timeline (0.17.0)."""
    world["controller"].poll_once()
    signals: Signals = world["signals"]
    root = _main_root(signals)
    (started,) = [
        e
        for e in signals.events("piceli.deploy.started")
        if e.trace_id == root.context.trace_id
    ]
    assert started.attributes["piceli.run.attempt"] == 1
    assert started.attributes["piceli.trigger"]
    stages = [
        e
        for e in signals.events("piceli.deploy.stage")
        if e.trace_id == root.context.trace_id
    ]
    assert [
        (e.attributes["piceli.stage"], e.attributes["piceli.stage.result"])
        for e in stages
    ] == [("plan", "success"), ("apply", "success"), ("checks", "success")]
    # Each stage event is stamped when its stage finished.
    assert stages[1].timestamp == otel._iso_ns(_iso(world["clock"]["now"] - 10 + 4))
    (finished,) = [
        e
        for e in signals.events("piceli.deploy.finished")
        if e.trace_id == root.context.trace_id
    ]
    assert finished.attributes["piceli.deploy.result"] == "success"
    assert started.timestamp < finished.timestamp


def test_a_failed_step_emits_a_retry_event_with_its_next_attempt(
    world: dict[str, Any],
) -> None:
    world["ports"].builder.fail = "error"
    status = world["controller"].poll_once()
    main = status["envs"]["main"]
    signals: Signals = world["signals"]
    (retry,) = signals.events("piceli.deploy.retry")
    assert retry.attributes["piceli.run.attempt"] == 1
    assert retry.attributes["error.type"] == "component-build-failed"
    assert (
        retry.attributes["piceli.retry.at"] == _iso(main["next_attempt_at"])[:19] + "Z"
    )
    (finished,) = signals.events("piceli.deploy.finished")
    assert finished.attributes["piceli.deploy.result"] == "failure"
