"""The train queue (``piceli train --queue``, ``piceli train-queue``) and runs
that fail midway (``run-failed``).

Each test builds the toy repository (:mod:`tests.train_toy`) in ``tmp_path``.
A "busy" train is simulated by holding its lock in the test process.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app
from piceli.train.engine import TrainRun, locate
from piceli.train.git import Git
from piceli.train.model import TrainError
from piceli.train.queue import TrainQueue
from piceli.train.state import TrainState
from tests.train_toy import Toy, make_toy
from tests.unit.train.test_train_observe import attrs, logs, receiver, spans


@pytest.fixture
def toy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Toy:
    monkeypatch.setenv("TOY_PUBLISH_OUT", str(tmp_path / "published"))
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


def train(toy: Toy, *args: str) -> tuple[int, Any, str]:
    return cli(toy, "train", toy.spec, *args, "--json")


def good(toy: Toy, name: str) -> None:
    toy.branch(name, {f"src/{name}.txt": f"{name}\n"})


def bad(toy: Toy, name: str) -> None:
    toy.branch(name, {f"src/{name}.txt": "BROKEN\n"})


def state(toy: Toy) -> TrainState:
    return TrainState(toy.root / "state" / "toy")


# ------------------------------------------------------------------ queue


def test_a_busy_train_queues_the_request_and_returns_at_once(toy: Toy) -> None:
    good(toy, "feat-a")
    good(toy, "feat-b")
    with state(toy).lock("20261023T100000Z-abcdef"):
        code, body, err = train(toy, "--branches", "feat-a")
        assert code == 2 and body["reason"] == "train-busy" and "--queue" in err
        code, first, _ = train(toy, "--branches", "feat-a", "--queue")
        assert code == 0
        assert (
            first["schema"] == "piceli.train.queued.v1" and first["state"] == "queued"
        )
        assert first["position"] == 1 and first["branches"] == ["feat-a"]
        code, second, _ = train(toy, "feat-b", "--promote", "--push", "--queue")
        assert second["position"] == 2 and second["promote"] and second["push"]
        code, listing, err = cli(toy, "train-queue", "toy", "--json")
        assert code == 0 and listing["schema"] == "piceli.train.queue.v1"
        assert [r["id"] for r in listing["requests"]] == [
            first["queue_id"],
            second["queue_id"],
        ]
        assert listing["requests"][1]["push"] is True and "feat-b" in err
        code, view, _ = cli(toy, "trains", "toy", "--json")
        queue = view["trains"][0]["queue"]
        assert [r["branches"] for r in queue] == [["feat-a"], ["feat-b"]]
        assert queue[0]["queued_at"] and queue[0]["state"] == "waiting"
        code, cancelled, _ = cli(
            toy, "train-queue", "toy", "--cancel", second["queue_id"], "--json"
        )
        assert code == 0 and cancelled["state"] == "cancelled"
        code, again, _ = cli(toy, "train-queue", "toy", "--cancel", second["queue_id"])
        assert code == 2 and again["reason"] == "train-queue-not-waiting"
        code, missing, _ = cli(toy, "train-queue", "toy", "--cancel", "q-nope")
        assert code == 2 and missing["reason"] == "train-queue-not-found"
        code, waited, _ = cli(toy, "train-wait", second["queue_id"], "--json")
        assert code == 1 and waited["reason"] == "train-queue-cancelled"
    # Nothing ran while the train was busy.
    assert not (state(toy).runs).exists() or not list(state(toy).runs.iterdir())


def test_the_running_train_drains_the_queue_with_each_requests_flags(toy: Toy) -> None:
    good(toy, "feat-a")
    good(toy, "feat-b")
    good(toy, "feat-c")
    with state(toy).lock("20261023T100000Z-abcdef"):
        _, first, _ = train(toy, "--branches", "feat-a", "--queue")
        _, second, _ = train(
            toy, "--branches", "feat-b", "--promote", "--push", "--queue"
        )
    # A plain run takes the lock, then serves the queue in order before it exits.
    code, own, err = train(toy, "--branches", "feat-c")
    assert code == 0 and own["branches"][0]["branch"] == "feat-c"
    records = {r.get("queue"): r for r in state(toy).history()}
    assert records[first["queue_id"]]["promote"] == {"state": "not-requested"}
    pushed = records[second["queue_id"]]
    assert (
        pushed["promote"]["state"] == "promoted" and pushed["push"]["state"] == "pushed"
    )
    assert toy.remote_refs()["refs/heads/main"] == pushed["head"]
    assert TrainQueue(state(toy)).waiting() == []
    code, waited, _ = cli(toy, "train-wait", second["queue_id"], "--json")
    assert code == 0 and waited["run"] == pushed["run"] and waited["pushed"] is True
    assert waited["queue"] == second["queue_id"]


def test_queue_on_a_free_train_runs_now(toy: Toy) -> None:
    good(toy, "feat-a")
    code, body, _ = train(toy, "--branches", "feat-a", "--queue")
    assert code == 0 and body["schema"] == "piceli.train.v1" and body["queue"]
    assert body["branches"][0]["verdict"] == "joined"


def test_a_request_whose_branch_is_gone_is_rejected_when_it_starts(toy: Toy) -> None:
    good(toy, "feat-a")
    good(toy, "gone")
    with state(toy).lock("20261023T100000Z-abcdef"):
        _, queued, _ = train(toy, "--branches", "gone", "--queue")
        _, kept, _ = train(toy, "--branches", "feat-a", "--queue")
    toy.git("branch", "-D", "gone")
    code, _, err = cli(toy, "train-queue", "toy", "--drain")
    assert code == 0, err
    code, waited, _ = cli(toy, "train-wait", queued["queue_id"], "--json")
    assert code == 1 and waited["reason"] == "train-branch-unknown"
    code, waited, _ = cli(toy, "train-wait", kept["queue_id"], "--json")
    assert code == 0


def test_a_request_whose_process_died_is_resumed(toy: Toy) -> None:
    good(toy, "feat-a")
    queue = TrainQueue(state(toy))
    request = queue.enqueue(
        entry=toy.spec,
        cwd=str(toy.repo),
        branches=["feat-a"],
        promote=False,
        push=False,
    )
    claimed = queue.claim()
    assert claimed is not None and claimed["id"] == request["id"]
    # The process serving it died: its claim names a pid that no longer exists.
    queue.update(claimed["id"], pid=2**22 + 4321)
    code, listing, _ = cli(toy, "train-queue", "toy", "--json")
    assert listing["requests"][0]["state"] == "stale"
    code, _, err = cli(toy, "train-queue", "toy", "--drain")
    assert code == 0, err
    done = queue.find(request["id"])
    assert done is not None and done["state"] == "done" and done["resumed"] == 1
    code, waited, _ = cli(toy, "train-wait", request["id"], "--json")
    assert code == 0 and waited["joined"] == ["feat-a"]


# ------------------------------------------------------------- run-failed


def _run(toy: Toy, **options: Any) -> TrainRun:
    loaded, repo, spec_path = locate(toy.spec, toy.repo)
    run = TrainRun(
        loaded, repo=repo, spec_path=spec_path, branches=["feat-a"],
        state=state(toy), **options,
    )  # fmt: skip
    run.prepare()
    return run


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TrainError("train-git-failed", "git worktree add failed"), "train-git-failed"),
        (KeyboardInterrupt(), "train-run-cancelled"),
        (RuntimeError("boom"), "train-run-failed"),
    ],
)
def test_a_run_that_raises_midway_reports_run_failed(
    toy: Toy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    reason: str,
) -> None:
    good(toy, "feat-a")
    out = tmp_path / "notified.json"
    keep = tmp_path / "keep.py"
    keep.write_text(
        f"import pathlib, sys\npathlib.Path({str(out)!r}).write_text(sys.stdin.read())\n"
    )

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    with receiver() as (url, requests):
        source = (toy.repo / "train_spec.py").read_text()
        source = source.replace(
            "from piceli.train import",
            "from piceli.infra import Otlp\nfrom piceli.train import Notify,",
        )
        source = source.replace(
            'tested="tested",',
            f'tested="tested",\n    telemetry=Otlp({url!r}, protocol="http/protobuf"),\n    notify=[Notify.command([sys.executable, {str(keep)!r}])],',
        )
        toy.commit({"train_spec.py": source}, "observe")
        run = _run(toy)
        monkeypatch.setattr(Git, "merge", explode)
        with pytest.raises(type(error)):
            run.run()
    record = state(toy).record(run.run_id)
    assert record is not None and record["state"] == "failed" and record["ok"] is False
    assert record["reason"] == reason
    assert state(toy).owner() is None
    events = state(toy).events(run.run_id)
    failed = (
        events[-1]
        if events[-1]["event"] == "run-failed"
        else next(e for e in events if e["event"] == "run-failed")
    )
    assert (
        failed["summary"]["state"] == "failed" and failed["summary"]["reason"] == reason
    )
    summary = json.loads(out.read_text())
    assert (
        summary["state"] == "failed"
        and summary["reason"] == reason
        and summary["joined"] == []
    )
    (root,) = [s for s in spans(requests) if s["name"] == "piceli.dev.train"]
    assert (
        attrs(root["attributes"])["piceli.train.reason"] == reason
        and root["status"]["code"] == 2
    )
    (done,) = [
        r
        for r in logs(requests)
        if attrs(r["attributes"])["event.name"] == "piceli.dev.train.finished"
    ]
    assert reason in done["body"]["stringValue"]
    code, waited, _ = cli(toy, "train-wait", run.run_id, "--json")
    assert code == 1 and waited["state"] == "failed" and waited["reason"] == reason
    code, view, _ = cli(toy, "trains", "toy", "--json")
    assert view["trains"][0]["runs"][0]["state"] == "failed"
    assert view["trains"][0]["runs"][0]["reason"] == reason


def test_cli_reports_a_run_that_failed_midway(
    toy: Toy, monkeypatch: pytest.MonkeyPatch
) -> None:
    good(toy, "feat-a")

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise TrainError("train-git-failed", "git merge failed")

    queue = TrainQueue(state(toy))
    waiting = queue.enqueue(
        entry=toy.spec,
        cwd=str(toy.repo),
        branches=["feat-a"],
        promote=False,
        push=False,
    )
    monkeypatch.setattr(Git, "merge", explode)
    code, body, _ = train(toy, "--branches", "feat-a")
    assert code == 1 and body["reason"] == "train-git-failed" and body["run"]
    assert state(toy).record(body["run"])["state"] == "failed"  # type: ignore[index]
    # The queue is still served after a run that failed.
    served = queue.find(waiting["id"])
    assert served is not None and served["state"] == "done"
    assert served["outcome"] == "failed" and served["reason"] == "train-git-failed"
