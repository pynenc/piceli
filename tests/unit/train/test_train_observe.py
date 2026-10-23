"""Seeing a train: events, ``piceli trains``, ``train-wait``, OTel, notifications.

Runs use the toy repository (:mod:`tests.train_toy`) in ``tmp_path``; the
OTLP collector and the webhook are in-process HTTP receivers.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.infra import Otlp
from piceli.k8s.cli import app
from piceli.train import Notify, Step, Train, TrainError
from piceli.train.notify import desktop_argv, send_all
from piceli.train.state import TrainState
from piceli.train.status import current, deployed, overview, table, trends
from piceli.train.summary import headline, summarize
from piceli.train.telemetry import TrainTelemetry, config_for
from tests.train_toy import Toy, make_toy

# ---------------------------------------------------------------- helpers


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
    return [
        span
        for path, _headers, body in requests
        if path == "/v1/traces"
        for resource in body["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]


def logs(requests: Any) -> list[dict[str, Any]]:
    return [
        record
        for path, _headers, body in requests
        if path == "/v1/logs"
        for resource in body["resourceLogs"]
        for scope in resource["scopeLogs"]
        for record in scope["logRecords"]
    ]


@pytest.fixture
def toy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Toy:
    monkeypatch.setenv("TOY_PUBLISH_OUT", str(tmp_path / "published"))
    for name in list(os.environ):
        if name.startswith("OTEL_"):
            monkeypatch.delenv(name)
    return make_toy(tmp_path)


def observe_spec(toy: Toy, observers: str) -> None:
    """Give the toy's train ``observers`` (Python keyword arguments) and commit."""
    spec = toy.repo / "train_spec.py"
    source = spec.read_text()
    source = source.replace(
        "from piceli.train import",
        "from piceli.infra import Otlp\nfrom piceli.train import Notify,",
    )
    source = source.replace('tested="tested",', f'tested="tested",\n    {observers}')
    toy.commit({"train_spec.py": source}, "observe the train")
    toy.git("push", "-q", "origin", "main")


def run(toy: Toy, *args: str) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(
        app,
        ["train", toy.spec, *args, "--json", "--state-dir", str(toy.root / "state")],
    )
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    lines = result.stdout.strip().splitlines()
    return result.exit_code, json.loads(lines[-1]) if lines else {}, result.stderr


def cli(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(app, list(args))
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result.exit_code, result.stdout, result.stderr


def good(toy: Toy, name: str) -> None:
    toy.branch(name, {f"src/{name}.txt": f"{name}\n"})


def bad(toy: Toy, name: str) -> None:
    toy.branch(name, {f"src/{name}.txt": "BROKEN\n"})


# ------------------------------------------------------- events and status


def test_every_event_is_recorded_with_where_each_step_runs(toy: Toy) -> None:
    good(toy, "feat-a")
    bad(toy, "fix-c")
    status, body, _ = run(toy, "--branches", "feat-a", "fix-c")
    assert status == 1
    state = TrainState(toy.root / "state" / "toy")
    events = state.events(body["run"])
    kinds = [e["event"] for e in events]
    assert kinds[0] == "run-started" and kinds[-1] == "run-finished"
    started = next(e for e in events if e["event"] == "step-started")
    assert started["host"] == "local" and started["log"].endswith("01-unit.log")
    assert started["branches"] == ["feat-a", "fix-c"]
    failed = next(
        e for e in events if e["event"] == "step-finished" and e["verdict"] == "failed"
    )
    assert failed["kind"] == "command" and "BROKEN" in failed["tail"]
    finished = events[-1]
    assert finished["summary"]["ejected"] == [
        {"branch": "fix-c", "step": "unit", "reason": "train-step-failed"}
    ]
    assert finished["summary"]["joined"] == ["feat-a"]
    # The lock is released and names nobody.
    assert state.owner() is None


def test_trains_shows_runs_trends_and_flaky_tests(toy: Toy) -> None:
    good(toy, "feat-a")
    bad(toy, "fix-c")
    run(toy, "--branches", "feat-a", "fix-c", "--promote")
    good(toy, "feat-b")
    run(toy, "--branches", "feat-b")
    state = TrainState(toy.root / "state" / "toy")
    state.record_flakes(
        [
            {
                "target": "-p api --lib",
                "test": "retries",
                "tail": "boom",
                "step": "cargo-test",
            }
        ],
        "r1",
        "2026-10-23T10:00:00Z",
    )
    code, out, err = cli(
        "trains", "toy", "--json", "--state-dir", str(toy.root / "state")
    )
    assert code == 0, err
    document = json.loads(out)
    assert document["schema"] == "piceli.trains.v1"
    (train,) = document["trains"]
    assert train["state"] == "idle" and train["current"] is None
    first, second = train["runs"][1], train["runs"][0]
    assert (
        first["state"] == "ejected"
        and first["tag"] == "tested/" + first["started"][:10] + ".1"
    )
    assert {b["branch"]: b["verdict"] for b in first["branches"]} == {
        "feat-a": "joined",
        "fix-c": "ejected",
    }
    assert second["state"] == "joined" and set(second["steps"]) >= {"unit", "fitness"}
    steps = {row["step"]: row for row in train["trends"]}
    assert steps["unit"]["samples"] == 4  # 3 trials, then 1
    assert steps["fitness"]["samples"] == 2  # unit failed first in two trials
    assert train["flaky"][0]["key"] == "-p api --lib retries"
    assert (
        "recent runs:" in err
        and "flaky tests:" in err
        and "1x -p api --lib retries" in err
    )
    # Every train when no name is given.
    code, out, _ = cli("trains", "--json", "--state-dir", str(toy.root / "state"))
    assert [t["train"] for t in json.loads(out)["trains"]] == ["toy"]


def test_unknown_train_is_rejected(tmp_path: Path) -> None:
    code, out, _ = cli("trains", "nope", "--state-dir", str(tmp_path))
    assert code == 2 and json.loads(out)["reason"] == "train-unknown"


def _running(root: Path, run_id: str) -> tuple[TrainState, Path]:
    """A run in progress: its events and a step log (the caller holds the lock)."""
    state = TrainState(root / "toy")
    log = state.run_dir(run_id) / "01-cargo-test.log"
    log.write_text("".join(f"line {n}\n" for n in range(30)))
    for event in (
        {"event": "run-started", "at": "2026-10-23T10:00:00.000000Z", "branches": ["a", "b"]},
        {"event": "trial-started", "at": "2026-10-23T10:00:01.000000Z", "trial": 1, "branches": ["a", "b"], "base": "c" * 40},
        {"event": "step-started", "at": "2026-10-23T10:00:02.000000Z", "trial": 1, "step": "cargo-check", "kind": "cargo-check", "host": "local", "log": "x"},
        {"event": "step-finished", "at": "2026-10-23T10:00:30.000000Z", "trial": 1, "step": "cargo-check", "verdict": "passed", "duration": 28.0, "host": "local"},
        {"event": "step-started", "at": "2026-10-23T10:00:30.000000Z", "trial": 1, "step": "cargo-test", "kind": "cargo-test", "host": "builder-1", "log": str(log)},
    ):  # fmt: skip
        state.append_event(run_id, event)
    return state, log


def test_the_running_step_with_its_host_elapsed_time_and_log_tail(
    tmp_path: Path,
) -> None:
    run_id = "20261023T100000Z-abcdef"
    state, _log = _running(tmp_path, run_id)
    with state.lock(run_id):
        live = current(state, now=1792749660.0)  # 2026-10-23T10:01:00Z
        assert live is not None
        assert live["run"] == run_id and live["branches"] == ["a", "b"]
        assert live["trial"]["id"] == 1
        step = live["step"]
        assert step["name"] == "cargo-test" and step["host"] == "builder-1"
        assert step["elapsed"] == 30.0
        assert (
            step["tail"].splitlines()[-1] == "line 29"
            and len(step["tail"].splitlines()) == 20
        )
        assert [d["name"] for d in live["done"]] == ["cargo-check"]
        assert live["elapsed"] == 60.0
        document = overview(tmp_path, ["toy"], now=1792749660.0)
        assert document["trains"][0]["state"] == "running"
        lines = table(document)
        assert any("cargo-test" in line and "builder-1" in line for line in lines)
        assert any("| line 29" in line for line in lines)
    assert current(state) is None


def test_a_killed_run_is_interrupted(tmp_path: Path) -> None:
    state = TrainState(tmp_path / "toy")
    state.write_record(
        "20261023T100000Z-abcdef",
        {"run": "20261023T100000Z-abcdef", "state": "running", "trials": []},
    )
    # A lock file left by a process that no longer exists.
    (state.root / "lock").write_text(json.dumps({"run": "x", "pid": 2**22 + 12345}))
    assert state.owner() is None
    view = overview(tmp_path)["trains"][0]
    assert view["runs"][0]["state"] == "interrupted"
    code, out, _ = cli(
        "train-wait", "20261023T100000Z-abcdef", "--state-dir", str(tmp_path), "--json"
    )
    assert code == 1 and json.loads(out)["reason"] == "train-interrupted"


def test_trends_compare_the_last_execution_with_the_median() -> None:
    def record(run: str, seconds: float) -> dict[str, Any]:
        steps = [
            {"name": "merge", "kind": "merge", "duration": 0.1},
            {
                "name": "unit",
                "kind": "command",
                "verdict": "passed",
                "duration": seconds,
            },
        ]
        return {"run": run, "trials": [{"id": 1, "steps": steps}]}

    rows = trends(
        [record("r4", 30.0), record("r3", 10.0), record("r2", 11.0), record("r1", 9.0)]
    )
    (unit,) = rows
    assert unit["median"] == 10.5 and unit["last"] == 30.0 and unit["trend"] == "slower"
    assert unit["last_run"] == "r4"
    assert trends([record("r2", 5.0), record("r1", 10.0)], 2)[0]["trend"] == "faster"


def test_deployed_joins_targets_with_the_run_that_promoted_them() -> None:
    head = "a" * 40
    histories = {
        "shop": [
            {"run": "r2", "head": head, "promote": {"state": "promoted", "tag": "tested/2026-10-23.1", "head": head},
             "branches": [{"branch": "feat-a", "verdict": "joined"}], "push": {"state": "pushed"}},
            {"run": "r1", "promote": {"state": "not-requested"}},
        ]
    }  # fmt: skip
    targets = {
        "targets": [
            {"kind": "nix-host", "name": "web-1", "state": "deployed", "revision": {"commit": "f" * 40, "tag": "tested/2026-10-23.1"}},
            {"kind": "environment", "name": "main", "state": "deployed", "revision": {"commit": head[:12], "tag": None}},
            {"kind": "environment", "name": "prod", "state": "deployed", "revision": {"commit": "e" * 40, "tag": None, "commits": {"infra": "e" * 40}}, "pinned": {"source": "product", "commit": head}},
            {"kind": "environment", "name": "old", "state": "deployed", "revision": {"commit": "9" * 40}},
        ]
    }  # fmt: skip
    rows = {row["name"]: row for row in deployed(targets, histories)}
    assert rows["web-1"]["matched"] == "tag" and rows["web-1"]["train"]["run"] == "r2"
    assert rows["main"]["matched"] == "commit"
    assert rows["prod"]["matched"] == "pinned"
    assert (
        rows["prod"]["train"]["joined"] == ["feat-a"]
        and rows["prod"]["train"]["pushed"]
    )
    assert rows["old"]["train"] is None and rows["old"]["matched"] is None
    assert deployed(None, histories) == []


# ------------------------------------------------------------- train-wait


def test_train_wait_exits_with_the_verdict(toy: Toy) -> None:
    good(toy, "feat-a")
    _, body, _ = run(toy, "--branches", "feat-a")
    code, out, err = cli(
        "train-wait", body["run"], "--state-dir", str(toy.root / "state"), "--json"
    )
    assert code == 0
    summary = json.loads(out)
    assert summary["schema"] == "piceli.train.summary.v1" and summary["joined"] == [
        "feat-a"
    ]
    assert "feat-a joined" in err
    bad(toy, "fix-c")
    run(toy, "--branches", "fix-c")
    # A train's name waits for its newest run.
    code, out, _ = cli(
        "train-wait", "toy", "--state-dir", str(toy.root / "state"), "--json"
    )
    assert code == 1 and json.loads(out)["ejected"][0]["branch"] == "fix-c"
    code, out, _ = cli("train-wait", "nope", "--state-dir", str(toy.root / "state"))
    assert code == 2 and json.loads(out)["reason"] == "train-run-not-found"


def test_train_wait_blocks_until_the_run_ends_or_times_out(tmp_path: Path) -> None:
    run_id = "20261023T100000Z-abcdef"
    state, _ = _running(tmp_path, run_id)
    root = str(tmp_path)
    with state.lock(run_id):
        code, out, _ = cli(
            "train-wait",
            run_id,
            "--state-dir",
            root,
            "--timeout",
            "0.3",
            "--interval",
            "0.1",
        )
        assert code == 1 and json.loads(out)["reason"] == "train-wait-timeout"
        # The run finishes while train-wait waits.
        record = {
            "run": run_id,
            "train": "toy",
            "state": "joined",
            "ok": True,
            "branches": [{"branch": "a", "verdict": "joined"}],
        }
        timer = threading.Timer(0.3, lambda: state.write_record(run_id, record))
        timer.start()
        try:
            code, out, _ = cli(
                "train-wait", "toy", "--state-dir", root, "--interval", "0.1", "--json"
            )
        finally:
            timer.join()
    assert code == 0 and json.loads(out)["joined"] == ["a"]


# ----------------------------------------------------------- notifications


def test_notifications_receive_the_summary_and_never_change_the_result(
    toy: Toy, tmp_path: Path
) -> None:
    out = tmp_path / "notified.json"
    keep = tmp_path / "keep.py"
    keep.write_text(
        f"import pathlib, sys\npathlib.Path({str(out)!r}).write_text(sys.stdin.read())\n"
    )
    with receiver() as (url, requests):
        observe_spec(
            toy,
            f"notify=[Notify.command([sys.executable, {str(keep)!r}]),"
            f" Notify.command([sys.executable, '-c', 'raise SystemExit(3)']),"
            f" Notify.webhook({url + '/hook'!r}),"
            f" Notify.command([sys.executable, '-c', 'raise SystemExit(4)'], when='failure')],",
        )
        good(toy, "feat-a")
        status, body, err = run(toy, "--branches", "feat-a", "--promote")
    assert status == 0 and body["ok"] is True
    summary = json.loads(out.read_text())
    assert summary["joined"] == ["feat-a"] and summary["promoted"] is True
    assert summary["tag"] == body["promote"]["tag"] and summary["run"] == body["run"]
    ((path, headers, posted),) = requests
    assert path == "/hook" and headers["Content-Type"] == "application/json"
    assert posted == summary
    assert "[train-notify-failed]" in err and err.count("[train-notify-failed]") == 1
    states = [item["state"] for item in body["notify"]]
    assert states == ["sent", "failed", "sent", "not-wanted"]
    events = TrainState(toy.root / "state" / "toy").events(body["run"])
    assert [e["notify"] for e in events if e["event"] == "notify-failed"] == [1]


def test_webhook_reads_its_url_and_headers_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = {"train": "shop", "ok": False, "joined": [], "ejected": []}
    with receiver(status=500) as (url, requests):
        monkeypatch.setenv("HOOK_URL", url)
        monkeypatch.setenv("HOOK_TOKEN", "Bearer s3cr3t")
        hook = Notify.webhook(
            url_env="HOOK_URL", headers_env={"Authorization": "HOOK_TOKEN"}, timeout=2
        )
        warnings: list[str] = []
        (outcome,) = send_all([hook], summary, warnings.append)
    assert requests[0][1]["Authorization"] == "Bearer s3cr3t"
    assert outcome["state"] == "failed" and "s3cr3t" not in "".join(warnings)
    assert "HOOK_URL" in hook.label() and url not in str(hook.to_dict())
    monkeypatch.delenv("HOOK_URL")
    (outcome,) = send_all([hook], summary, warnings.append)
    assert outcome["state"] == "failed"


def test_a_notifier_past_its_timeout_fails_alone() -> None:
    slow = Notify.command(
        [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3
    )
    warnings: list[str] = []
    began = time.monotonic()
    (outcome,) = send_all([slow], {"ok": True}, warnings.append)
    assert time.monotonic() - began < 3
    assert outcome["state"] == "failed" and "(timeout)" in warnings[0]


def test_desktop_notifications_pass_text_as_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: f"/bin/{name}")
    argv = desktop_argv('train "x"', "a\nb", platform="darwin")
    assert (
        argv is not None
        and argv[0] == "/bin/osascript"
        and argv[-2:] == ['train "x"', "a\nb"]
    )
    assert "on run argv" in argv
    assert desktop_argv("t", "b", platform="linux") == [
        "/bin/notify-send",
        "--app-name=piceli",
        "t",
        "b",
    ]
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert desktop_argv("t", "b", platform="linux") is None
    (outcome,) = send_all([Notify.desktop()], {"ok": True}, lambda _m: None)
    assert outcome["state"] == "skipped"


def test_observer_declarations_are_checked() -> None:
    steps = [Step.command("unit", ["true"])]
    with pytest.raises(TrainError, match="OTLP/HTTP"):
        Train("t", steps=steps, telemetry=Otlp("http://localhost:4317"))
    with pytest.raises(TrainError, match="cluster Secrets"):
        Train(
            "t",
            steps=steps,
            telemetry=Otlp(
                "http://localhost:4318", protocol="http/protobuf", headers_secret="otel"
            ),
        )
    with pytest.raises(TrainError, match="notify"):
        Train("t", steps=steps, notify=Notify.desktop())  # type: ignore[arg-type]
    with pytest.raises(TrainError, match="url"):
        Notify.webhook("ftp://example.invalid")
    with pytest.raises(TrainError, match="one of url"):
        Notify.webhook()
    with pytest.raises(TrainError, match="empty"):
        Notify.command([])
    with pytest.raises(TrainError, match="when"):
        Notify.desktop(when="sometimes")  # type: ignore[arg-type]
    train = Train(
        "t",
        steps=steps,
        telemetry=Otlp("http://localhost:4318", protocol="http/protobuf"),
        notify=[
            Notify.command(["cat"]),
            Notify.webhook(url_env="HOOK"),
            Notify.desktop(when="failure"),
        ],
    )
    assert Train.from_dict(json.loads(json.dumps(train.to_dict()))) == train


def test_summary_headline() -> None:
    result = {
        "train": "shop", "run": "r", "state": "ejected", "ok": False, "duration": 412.7, "head": "h",
        "branches": [{"branch": "feat-a", "verdict": "joined"}, {"branch": "fix-c", "verdict": "ejected", "step": "fitness", "reason": "train-step-failed", "tail": "secret output"}],
        "promote": {"state": "promoted", "tag": "tested/2026-10-23.1"}, "push": {"state": "pushed"},
        "flaky": [{"key": "k", "count": 2}],
    }  # fmt: skip
    summary = summarize(result)
    assert "secret output" not in json.dumps(summary)
    assert headline(summary) == (
        "train shop: feat-a joined, fix-c ejected (fitness); promoted "
        "tested/2026-10-23.1, pushed, 1 flaky, 6.9 min"
    )


# -------------------------------------------------------------------- OTel


def test_a_run_is_one_trace_with_trial_and_step_spans_and_logs(toy: Toy) -> None:
    with receiver() as (url, requests):
        observe_spec(toy, f'telemetry=Otlp({url!r}, protocol="http/protobuf"),')
        good(toy, "feat-a")
        bad(toy, "fix-c")
        status, body, err = run(toy, "--branches", "feat-a", "fix-c", "--promote")
    assert status == 1 and "train-telemetry-failed" not in err
    found = spans(requests)
    (root,) = [s for s in found if s["name"] == "piceli.dev.train"]
    trials = [s for s in found if s["name"] == "piceli.dev.train.trial"]
    steps = [s for s in found if s["name"] == "piceli.dev.train.step"]
    assert {s["traceId"] for s in found} == {root["traceId"]}
    assert "parentSpanId" not in root and root["status"]["code"] == 2
    root_attrs = attrs(root["attributes"])
    assert root_attrs["piceli.train.run"] == body["run"]
    assert root_attrs["piceli.train.joined"]["values"] == [{"stringValue": "feat-a"}]
    assert {e["name"] for e in root["events"]} >= {
        "branch-joined",
        "branch-ejected",
        "promoted",
    }
    assert len(trials) == 3 and all(t["parentSpanId"] == root["spanId"] for t in trials)
    trial_ids = {t["spanId"] for t in trials}
    unit = [s for s in steps if attrs(s["attributes"])["piceli.train.step"] == "unit"]
    assert len(unit) == 3 and all(s["parentSpanId"] in trial_ids for s in unit)
    failed = [
        s for s in unit if attrs(s["attributes"])["piceli.train.verdict"] == "failed"
    ]
    assert failed and all(s["status"]["code"] == 2 for s in failed)
    first = attrs(unit[0]["attributes"])
    assert first["piceli.train.host"] == "local" and "piceli.train.duration_ms" in first
    publish = [
        s
        for s in steps
        if attrs(s["attributes"])["piceli.train.step.phase"] == "publish"
    ]
    assert publish and publish[0]["parentSpanId"] == root["spanId"]
    records = logs(requests)
    step_logs = [
        r
        for r in records
        if attrs(r["attributes"])["event.name"] == "piceli.dev.train.step"
    ]
    assert any("BROKEN" in r["body"]["stringValue"] for r in step_logs)
    (done,) = [
        r
        for r in records
        if attrs(r["attributes"])["event.name"] == "piceli.dev.train.finished"
    ]
    assert done["body"]["stringValue"].startswith(
        "train toy: feat-a joined, fix-c ejected (unit)"
    )
    assert done["spanId"] == root["spanId"]


def test_flaky_tests_are_span_events() -> None:
    with receiver() as (url, requests):
        config = config_for(Otlp(url, protocol="http/protobuf"), {})
        assert config is not None
        sink = TrainTelemetry(config)
        sink(
            {
                "event": "run-started",
                "at": "2026-10-23T10:00:00Z",
                "run": "r",
                "train": "t",
                "branches": ["a"],
            }
        )
        sink(
            {
                "event": "trial-started",
                "at": "2026-10-23T10:00:01Z",
                "trial": 1,
                "branches": ["a"],
            }
        )
        sink(
            {
                "event": "step-started",
                "at": "2026-10-23T10:00:02Z",
                "trial": 1,
                "step": "cargo-test",
            }
        )
        sink({"event": "step-finished", "at": "2026-10-23T10:01:02Z", "trial": 1, "step": "cargo-test", "kind": "cargo-test",
              "verdict": "passed", "duration": 60.0, "host": "local", "selection": ["api", "core"],
              "flaky": [{"target": "-p api --lib", "test": "retries"}]})  # fmt: skip
        sink.close()
    (step,) = spans(requests)
    assert step["events"][0]["name"] == "piceli.dev.train.flaky"
    assert attrs(step["events"][0]["attributes"]) == {
        "piceli.train.test": "retries",
        "piceli.train.test.target": "-p api --lib",
    }
    assert attrs(step["attributes"])["piceli.train.selection"]["values"] == [
        {"stringValue": "api"},
        {"stringValue": "core"},
    ]
    assert step["startTimeUnixNano"] < step["endTimeUnixNano"]


def test_a_collector_that_is_down_or_slow_never_slows_the_train(
    toy: Toy, tmp_path: Path
) -> None:
    with receiver(delay=30) as (url, _requests):
        observe_spec(toy, f'telemetry=Otlp({url!r}, protocol="http/protobuf"),')
        good(toy, "feat-a")
        began = time.monotonic()
        status, body, err = run(toy, "--branches", "feat-a")
        slow = time.monotonic() - began
    assert status == 0 and body["ok"] is True
    assert err.count("[train-telemetry-failed]") == 1
    # The run's own work takes a few seconds; the export adds at most ~2.5 s.
    assert slow < body["duration"] + 6
    # Nobody listening at all.
    sink = TrainTelemetry(
        config_for(Otlp("http://127.0.0.1:9", protocol="http/protobuf"), {})
    )  # type: ignore[arg-type]
    for n in range(500):  # more than the queue holds
        sink({"event": "trial-started", "trial": n})
        sink({"event": "trial-finished", "trial": n, "verdict": "passed"})
    began = time.monotonic()
    sink.close()
    assert time.monotonic() - began < 3
    assert sink.exporter.dropped > 0


def test_standard_environment_configures_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert config_for(None, {}) is None
    found = config_for(
        None,
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
            "OTEL_EXPORTER_OTLP_HEADERS": "x-key=v",
        },
    )
    assert found is not None and found.traces_url == "http://127.0.0.1:4318/v1/traces"
    assert found.headers == {"x-key": "v"} and found.timeout <= 2.0
    otlp = Otlp("http://127.0.0.1:4318/", protocol="http/protobuf")
    assert config_for(otlp, {"OTEL_SDK_DISABLED": "true"}) is None
    assert config_for(otlp, {}).logs_url == "http://127.0.0.1:4318/v1/logs"  # type: ignore[union-attr]
