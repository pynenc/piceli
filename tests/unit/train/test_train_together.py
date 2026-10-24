"""Where speed (build hosts, reuse, halves, promote-run, pruning) meets
observation (events, ``piceli trains``, the trace, the queue, run-failed)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.dev.hosts import HostState
from piceli.k8s.cli import app
from piceli.train.engine import TrainRun, locate
from piceli.train.git import Git
from piceli.train.hosts import build_runner
from piceli.train.model import Retention, TrainError
from piceli.train.prune import prune
from piceli.train.queue import TrainQueue
from piceli.train.state import TrainState
from piceli.train.status import current, table
from tests.train_toy import Toy, make_toy
from tests.unit.dev.local_port import LocalPort
from tests.unit.train.test_train_observe import attrs, receiver, spans
from tests.unit.train.test_train_speed import REMOTE, host_spec


@pytest.fixture
def toy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Toy:
    monkeypatch.setenv("TOY_PUBLISH_OUT", str(tmp_path / "published"))
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    for name in list(os.environ):
        if name.startswith("OTEL_"):
            monkeypatch.delenv(name)
    return make_toy(tmp_path)


def cli(toy: Toy, *args: str) -> tuple[int, Any, str]:
    result = CliRunner().invoke(app, [*args, "--state-dir", str(toy.root / "state")])
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    lines = result.stdout.strip().splitlines()
    return result.exit_code, json.loads(lines[-1]) if lines else None, result.stderr


def state(toy: Toy) -> TrainState:
    return TrainState(toy.root / "state" / "toy")


def with_telemetry(source: str, url: str) -> str:
    source = source.replace(
        "from piceli.train import",
        "from piceli.infra import Otlp\nfrom piceli.train import",
    )
    return source.replace(
        'tested="tested",',
        f'tested="tested",\n    telemetry=Otlp({url!r}, protocol="http/protobuf"),',
    )


def host_train(
    toy: Toy,
    tmp_path: Path,
    branches: list[str],
    *,
    up: bool = True,
    fallback: bool = True,
) -> TrainRun:
    """A run of the toy's host spec on a fake build host ``server``; the run
    also records what ``piceli trains`` showed while each step ran."""
    loaded, repo, spec_path = locate(toy.spec, toy.repo)

    def port(host: Any, _dev: Any) -> LocalPort:
        return LocalPort(tmp_path / "hosts" / host.name)

    def probe(host: Any) -> HostState:
        return HostState(host, up, "" if up else "engine does not answer",
                         platforms=("linux/amd64",))  # fmt: skip

    runner = build_runner(loaded.train, repo, port=port, probe=probe)
    if not fallback:
        runner.fallback = None
    seen: list[dict[str, Any]] = []

    def watch(event: Any) -> None:
        if event["event"] == "step-placed":
            live = current(state(toy))
            seen.append(live["step"] if live else {})

    run = TrainRun(
        loaded, repo=repo, spec_path=spec_path, branches=branches,
        state=state(toy), runner=runner, events=watch,
    )  # fmt: skip
    run.seen = seen  # type: ignore[attr-defined]
    return run


def step_span(found: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(
        attrs(s["attributes"])
        for s in found
        if s["name"] == "piceli.dev.train.step"
        and attrs(s["attributes"])["piceli.train.step"] == name
    )


# ------------------------------------------------------------- build hosts


def test_a_step_on_a_build_host_shows_the_host_in_trains_and_the_trace(
    toy: Toy, tmp_path: Path
) -> None:
    with receiver() as (url, requests):
        toy.commit(
            {
                "train_spec.py": with_telemetry(host_spec(), url),
                "scripts/remote.py": REMOTE,
            },
            "hosts, observed",
        )
        toy.branch("feat-a", {"src/a.txt": "a\n"})
        run = host_train(toy, tmp_path, ["feat-a"])
        body = run.run()
    assert body["ok"], body
    events = state(toy).events(body["run"])
    # The step started here (where the train is) and was placed on the server.
    placed = [e for e in events if e["event"] == "step-placed"]
    assert {(e["step"], e["host"]) for e in placed} >= {
        ("remote", "server"),
        ("fitness", "local"),
    }
    finished = {e["step"]: e for e in events if e["event"] == "step-finished"}
    assert finished["remote"]["host"] == "server"
    # While it ran, piceli trains showed it running on the server.
    assert {"name": "remote", "host": "server"}.items() <= run.seen[0].items()  # type: ignore[attr-defined]
    # After: the run's row names the hosts, in JSON and in the table.
    code, view, err = cli(toy, "trains", "toy", "--json")
    assert code == 0, err
    row = view["trains"][0]["runs"][0]
    assert row["run"] == body["run"] and row["hosts"] == ["server", "local"]
    assert any(
        body["run"] in line and "on server, local" in line for line in table(view)
    )
    # The trace: the step's span names the server.
    assert step_span(spans(requests), "remote")["piceli.train.host"] == "server"
    assert step_span(spans(requests), "fitness")["piceli.train.host"] == "local"


def test_a_reused_step_is_reused_in_status_and_the_trace(
    toy: Toy, tmp_path: Path
) -> None:
    with receiver() as (url, requests):
        toy.commit(
            {
                "train_spec.py": with_telemetry(host_spec(), url),
                "scripts/remote.py": REMOTE,
            },
            "hosts, observed",
        )
        toy.branch("feat-a", {"src/a.txt": "a\n"})
        first = host_train(toy, tmp_path, ["feat-a"]).run()
        toy.branch("feat-b", {"README.md": "# b\n"}, base="feat-a")
        del requests[:]
        again = host_train(toy, tmp_path, ["feat-b"]).run()
    assert first["ok"] and again["ok"], again
    events = state(toy).events(again["run"])
    remote = next(
        e for e in events if e["event"] == "step-finished" and e["step"] == "remote"
    )
    assert remote["verdict"] == "reused"
    assert remote["reused"]["run"] == first["run"]
    assert remote["reused"]["host"] == "server"
    # No command ran for it: nothing was placed.
    assert not [
        e for e in events if e["event"] == "step-placed" and e["step"] == "remote"
    ]
    code, view, _ = cli(toy, "trains", "toy", "--json")
    assert code == 0
    row = view["trains"][0]["runs"][0]
    assert row["run"] == again["run"] and row["reused"] == ["remote"]
    assert "remote" not in row["hosts"]
    # A reused step is no execution: it does not drag the trend down.
    (trend,) = [t for t in view["trains"][0]["trends"] if t["step"] == "remote"]
    assert trend["samples"] == 1 and trend["last_run"] == first["run"]
    # While the run was live, the step done showed where it came from.
    with state(toy).lock(again["run"]):
        live = current(state(toy))
    assert live is not None
    (done,) = [d for d in live["done"] if d["name"] == "remote"]
    assert done["verdict"] == "reused" and done["reused"]["run"] == first["run"]
    # The trace: an ok span with the run it came from.
    span = next(
        s
        for s in spans(requests)
        if s["name"] == "piceli.dev.train.step"
        and attrs(s["attributes"])["piceli.train.step"] == "remote"
    )
    values = attrs(span["attributes"])
    assert values["piceli.train.verdict"] == "reused"
    assert values["piceli.train.reused_from"] == first["run"]
    assert span["status"]["code"] == 1


def test_a_build_host_that_is_gone_fails_the_run_with_run_failed(
    toy: Toy, tmp_path: Path
) -> None:
    toy.commit({"train_spec.py": host_spec(), "scripts/remote.py": REMOTE}, "hosts")
    toy.branch("feat-a", {"src/a.txt": "a\n"})
    run = host_train(toy, tmp_path, ["feat-a"], up=False, fallback=False)
    with pytest.raises(TrainError):
        run.run()
    record = state(toy).record(run.run_id)
    assert record is not None and record["state"] == "failed"
    assert record["reason"] == "train-host-unavailable"
    events = state(toy).events(run.run_id)
    assert events[-1]["event"] == "run-failed"
    assert events[-1]["reason"] == "train-host-unavailable"
    code, waited, _ = cli(toy, "train-wait", run.run_id, "--json")
    assert code == 1 and waited["reason"] == "train-host-unavailable"


# ----------------------------------------------------------- parallel halves


def test_parallel_halves_are_seen_in_events_status_and_the_trace(toy: Toy) -> None:
    with receiver() as (url, requests):
        source = (toy.repo / "train_spec.py").read_text()
        source = with_telemetry(source, url).replace(
            'tested="tested",', 'tested="tested", parallel="on", local_slots=2,'
        )
        toy.commit({"train_spec.py": source}, "parallel, observed")
        for name in ("a", "b", "c", "d", "e", "f"):
            broken = name in ("b", "e")
            toy.branch(name, {f"src/{name}.txt": "BROKEN\n" if broken else f"{name}\n"})
        code, body, err = cli(
            toy, "train", toy.spec, "--branches", "a", "b", "c", "d", "e", "f", "--json"
        )
    assert code == 1, err
    events = state(toy).events(body["run"])
    kinds = [e["event"] for e in events]
    assert "halves-parallel" in kinds and "trial-discarded" in kinds
    assert "pruned" in kinds
    speculative = [
        e for e in events if e["event"] == "trial-finished" and e.get("speculative")
    ]
    assert speculative
    discarded = next(e for e in events if e["event"] == "trial-discarded")
    # Replayed as a live view: the discarded trial is named.
    with state(toy).lock(body["run"]):
        live = current(state(toy))
    assert live is not None and discarded["trial"] in live["discarded"]
    root = next(s for s in spans(requests) if s["name"] == "piceli.dev.train")
    names = {e["name"] for e in root["events"]}
    assert {"halves-parallel", "trial-discarded", "pruned"} <= names
    trials = [s for s in spans(requests) if s["name"] == "piceli.dev.train.trial"]
    assert any(attrs(t["attributes"]).get("piceli.train.speculative") for t in trials)


def test_two_steps_running_at_once_are_both_shown(tmp_path: Path) -> None:
    root = tmp_path / "state"
    run_id = "20261023T100000Z-abcdef"
    train = TrainState(root / "toy")
    for event in (
        {"event": "run-started", "at": "2026-10-23T10:00:00.000000Z", "branches": ["a", "b", "c"]},
        {"event": "trial-started", "at": "2026-10-23T10:00:01.000000Z", "trial": 2, "branches": ["a"], "base": "c" * 40},
        {"event": "halves-parallel", "at": "2026-10-23T10:00:01.000000Z", "left": ["a"], "right": ["b", "c"]},
        {"event": "trial-started", "at": "2026-10-23T10:00:01.000000Z", "trial": 3, "branches": ["b", "c"], "speculative": True},
        {"event": "step-started", "at": "2026-10-23T10:00:02.000000Z", "trial": 2, "step": "unit", "kind": "command", "host": "local", "log": "x"},
        {"event": "step-started", "at": "2026-10-23T10:00:03.000000Z", "trial": 3, "step": "unit", "kind": "command", "host": "local", "log": "y"},
        {"event": "step-placed", "at": "2026-10-23T10:00:03.000000Z", "trial": 3, "step": "unit", "host": "server", "log": "y"},
    ):  # fmt: skip
        train.append_event(run_id, event)
    with train.lock(run_id):
        live = current(train, now=1792749660.0)
    assert live is not None
    assert [t["id"] for t in live["trials"]] == [2, 3]
    assert live["trials"][1]["speculative"] is True
    assert [(s["trial"], s["host"]) for s in live["steps"]] == [
        (2, "local"),
        (3, "server"),
    ]


# ------------------------------------------------------- queue, promote-run


def test_a_queued_promote_run_is_served_in_its_turn(toy: Toy) -> None:
    toy.branch("feat-a", {"src/a.txt": "a\n"})
    code, first, err = cli(toy, "train", toy.spec, "--branches", "feat-a", "--json")
    assert code == 0, err
    head = first["head"]
    # Another run holds the train: --promote-run last --queue waits its turn,
    # and ``last`` is the run the caller saw, resolved now.
    with state(toy).lock("20261023T100000Z-0000aa"):
        code, queued, err = cli(
            toy, "train", toy.spec, "--promote-run", "last", "--queue", "--json"
        )
        assert code == 0, err
        assert queued["state"] == "queued" and queued["promote_run"] == first["run"]
        code, listing, _ = cli(toy, "train-queue", "toy", "--json")
        assert listing["requests"][0]["promote_run"] == first["run"]
        _, _, err = cli(toy, "train-queue", "toy")
        assert f"--promote-run {first['run']}" in err
        code, view, _ = cli(toy, "trains", "toy", "--json")
        assert view["trains"][0]["queue"][0]["promote_run"] == first["run"]
    assert toy.rev("main") == first["start"]  # nothing moved while queued
    code, _drained, err = cli(toy, "train-queue", "toy", "--drain", "--json")
    assert code == 0, err
    code, waited, err = cli(toy, "train-wait", queued["queue_id"], "--json")
    assert code == 0, err
    assert waited["promoted"] is True and waited["queue"] == queued["queue_id"]
    assert toy.rev("main") == head == toy.rev("tested")
    record = state(toy).record(waited["run"])
    assert record is not None
    assert record["promoted_from"] == first["run"]
    assert record["queue"] == queued["queue_id"]
    events = state(toy).events(waited["run"])
    assert events[0]["event"] == "run-started"
    assert events[0]["promote_run"] == first["run"]
    assert events[0]["verdicts"] == {"feat-a": "joined"}
    assert events[-1]["event"] == "run-finished" and events[-1]["summary"]["promoted"]


def test_a_promote_run_that_raises_midway_reports_run_failed(
    toy: Toy, monkeypatch: pytest.MonkeyPatch
) -> None:
    toy.branch("feat-a", {"src/a.txt": "a\n"})
    code, first, err = cli(toy, "train", toy.spec, "--branches", "feat-a", "--json")
    assert code == 0, err

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise TrainError("train-git-failed", "git tag failed")

    monkeypatch.setattr(Git, "tag", broken)
    code, failed, err = cli(
        toy, "train", toy.spec, "--promote-run", first["run"], "--json"
    )
    assert code == 1 and failed["reason"] == "train-git-failed", err
    record = state(toy).record(failed["run"])
    assert record is not None and record["state"] == "failed"
    assert record["promoted_from"] == first["run"]
    events = state(toy).events(failed["run"])
    assert events[-1]["event"] == "run-failed"
    assert events[-1]["summary"]["reason"] == "train-git-failed"
    assert state(toy).owner() is None


# ------------------------------------------------------------------- prune


def test_prune_keeps_the_queue_events_and_the_runs_views_need(
    tmp_path: Path,
) -> None:
    train = TrainState(tmp_path / "state" / "toy")
    old = time.time() - 90 * 86400

    def made(run_id: str, **record: Any) -> Path:
        train.write_record(run_id, {"run": run_id, "state": "joined", **record})
        train.append_event(run_id, {"event": "run-started", "run": run_id})
        path = train.runs / run_id
        for item in path.iterdir():
            os.utime(item, (old, old))
        os.utime(path, (old, old))
        return path

    promoted = made(
        "20260101T000000Z-000001",
        promote={"state": "promoted", "tag": "tested/2026-01-01.1"},
    )
    target = made("20260102T000000Z-000002")
    served = made("20260103T000000Z-000003")
    holding = made("20260104T000000Z-000004")
    plain = made("20251231T000000Z-000000")  # the oldest
    queue = TrainQueue(train)
    running = queue.enqueue(
        entry="spec.py:train", cwd=str(tmp_path), branches=[], promote=True,
        push=False, promote_run=target.name,
    )  # fmt: skip
    waiting = queue.enqueue(
        entry="spec.py:train", cwd=str(tmp_path), branches=["x"], promote=True,
        push=False,
    )  # fmt: skip
    claimed = queue.claim()  # the oldest, served by this process
    assert claimed is not None and claimed["id"] == running["id"]
    queue.update(running["id"], run=served.name)
    files_before = sorted(p.relative_to(queue.root) for p in queue.root.rglob("*"))
    with train.lock(holding.name):
        done = prune(
            "toy",
            cache=tmp_path / "cache",
            state_root=train.root,
            retention=Retention(keep_runs=1, log_days=1),
        )
    removed = {Path(item["path"]).name for item in done["removed"]}
    assert removed == {plain.name}
    for kept in (promoted, target, served, holding):
        assert (kept / "events.jsonl").is_file() and (kept / "run.json").is_file()
    assert sorted(p.relative_to(queue.root) for p in queue.root.rglob("*")) == (
        files_before
    )
    assert [r["id"] for r in queue.pending()] == [running["id"], waiting["id"]]
    assert (train.root / "lock").exists()
