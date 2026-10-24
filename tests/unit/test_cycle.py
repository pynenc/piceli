"""0.28.0: cycle metrics (``piceli.cycle``) and ``piceli cycle report``.

Pure records and report math first, then the command on local records
(a controller state directory, train run records, host switches): no
cluster, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.cycle import local
from piceli.cycle.record import (
    MAX_PHASES,
    cycle,
    deploy_cycle,
    environment_cycle,
    host_cycle,
    iso,
    release_cycle,
    train_cycle,
)
from piceli.cycle.report import (
    collect,
    markdown,
    parse_since,
    percentile,
    report,
    text,
)
from piceli.gitops import history
from piceli.k8s.cli import app

T0 = 1_800_000_000.0  # a fixed clock


def at(seconds: float) -> str:
    return iso(T0 + seconds)


def done(
    target: str, seconds: float, phases: dict[str, float], **extra: Any
) -> dict[str, Any]:
    return cycle(
        target=target,
        kind=extra.pop("kind", "environment"),
        commit=extra.pop("commit", "c" * 40),
        seen_at=at(extra.pop("start", 0)),
        checked_at=at(extra.pop("start_end", 0) + seconds),
        ok=extra.pop("ok", True),
        phases=phases,
        **extra,
    )


@pytest.fixture(autouse=True)
def _own_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


# ------------------------------------------------------------------ records


def test_a_deploy_with_a_host_build_records_each_phase() -> None:
    found = deploy_cycle(
        target="main",
        commit="a" * 40,
        seen_at=at(0),
        first_step_at=at(5),
        step_started_at=at(400),
        finished_at=at(460),
        state="deployed",
        checked=True,
        host_build={
            "requested_at": at(6),
            "claimed_at": at(36),
            "built_at": at(336),
            "delivered_at": at(340),
        },
        stages={
            "plan": {"seconds": 4.0},
            "apply": {"seconds": 26.0},
            "checks": {"seconds": 20.0},
        },
    )
    assert found["phases"] == {
        "queued": 5.0,
        "waiting-for-host": 30.0,
        "building": 300.0,
        "delivering": 4.0,
        "applying": 30.0,
        "checking": 20.0,
    }
    assert found["seconds"] == 460.0 and found["ok"] and found["from"] == "seen"
    assert found["checked_at"] == at(460) and found["pushed_at"] is None


def test_in_cluster_builds_split_their_push_out() -> None:
    found = deploy_cycle(
        target="web-1",
        commit="b" * 40,
        seen_at=at(0),
        first_step_at=at(0),
        step_started_at=at(0),
        finished_at=at(200),
        state="failed",
        checked=False,
        waited_since=None,
        builds={
            "api": {"started_at": at(10), "finished_at": at(110), "state": "done",
                    "timings": {"push_seconds": 15.0}},
            "web": {"started_at": at(20), "finished_at": at(90), "state": "done"},
            "old": {"started_at": at(0), "finished_at": at(1), "state": "cached"},
        },
        stages={"build": {"seconds": 99.0}, "checks": {"seconds": 3.0}},
    )  # fmt: skip
    assert found["phases"] == {
        "queued": 0.0, "building": 85.0, "delivering": 15.0, "checking": 3.0,
    }  # fmt: skip
    assert not found["ok"] and found["seconds"] is None and found["checked_at"] is None


def test_a_cycle_record_is_bounded() -> None:
    phases = {f"testing step-{n:03d}" + "x" * 80: float(n) for n in range(100)}
    found = cycle(
        target="t" * 300, kind="train", commit="c" * 200, seen_at=at(0),
        checked_at=at(10), ok=True, phases=phases,
    )  # fmt: skip
    assert len(found["phases"]) == MAX_PHASES
    assert all(len(name) <= 48 for name in found["phases"])
    assert min(found["phases"].values()) == 100 - MAX_PHASES  # the longest kept
    assert len(found["target"]) == 48 and len(found["commit"]) == 64
    assert len(json.dumps(found)) < 3000


def test_the_history_keeps_the_cycle_of_a_step() -> None:
    record = {"state": "deployed", "components": {}, "branch": "main"}
    own = done("main", 60, {"applying": 10.0})
    entry = history.event(
        record, started_at=at(0), finished_at=at(60), trigger="push",
        approval=None, outcome=None, built=[], cycle=own,
    )  # fmt: skip
    assert entry["cycle"] == own
    (run,) = history.environment_runs("main", [entry], [])
    assert run["cycle"] == own
    assert environment_cycle("main", run) == own


def test_an_older_history_run_gives_a_cycle_from_its_stages() -> None:
    run = {
        "kind": "run", "state": "deployed", "started_at": at(0),
        "finished_at": at(90), "sources": {"shop": {"commit": "d" * 40}},
        "stages": {"apply": {"seconds": 30.0}, "checks": {"seconds": 12.0}},
        "builds": {}, "approval_wait": {"seconds": 20.0},
    }  # fmt: skip
    found = environment_cycle("main", run)
    assert found is not None
    assert found["phases"] == {"applying": 30.0, "approval": 20.0, "checking": 12.0}
    assert found["seconds"] == 90.0 and found["commit"] == "d" * 40
    assert environment_cycle("main", {"kind": "stop"}) is None


def test_a_train_run_records_queued_merging_testing_promoting() -> None:
    result = {
        "train": "shop", "head": "e" * 40, "started": at(60), "duration": 300.0,
        "ok": True, "state": "joined",
        "trials": [
            {"steps": [{"name": "merge", "kind": "merge", "duration": 2.0},
                       {"name": "unit", "kind": "command", "duration": 100.0},
                       {"name": "fitness", "kind": "command", "duration": 50.0}]},
            {"discarded": True,
             "steps": [{"name": "unit", "kind": "command", "duration": 999.0}]},
            {"steps": [{"name": "merge", "kind": "merge", "duration": 1.0},
                       {"name": "unit", "kind": "command", "duration": 80.0}]},
        ],
        "promote": {"state": "promoted", "publish": [
            {"name": "images", "kind": "images", "duration": 40.0},
            {"name": "notes", "kind": "command", "duration": 5.0}]},
    }  # fmt: skip
    found = train_cycle(
        result, queued_at=at(0), timings={"promoting": 50.0, "pushing": 3.0}
    )
    assert found["phases"] == {
        "queued": 60.0,
        "merging": 3.0,
        "testing unit": 180.0,
        "testing fitness": 50.0,
        "images": 40.0,
        "publishing": 5.0,
        "promoting": 5.0,
        "pushing": 3.0,
    }
    assert found["kind"] == "train" and found["target"] == "shop"
    assert found["seconds"] == 360.0 and found["seen_at"] == at(0)


def test_a_host_switch_starts_at_the_release_of_its_commit() -> None:
    attempt = {
        "commit": "f" * 40, "published_at": at(100), "started_at": at(130),
        "ended_at": at(200), "state": "deployed",
        "stages": [{"name": "resolve", "seconds": 1.0},
                   {"name": "verify", "seconds": 2.0},
                   {"name": "realise", "seconds": 20.0},
                   {"name": "activate", "seconds": 30.0},
                   {"name": "checks", "seconds": 10.0},
                   {"name": "boot", "seconds": 5.0}],
    }  # fmt: skip
    alone = host_cycle("web-1", attempt)
    assert alone["phases"] == {
        "polling": 30.0, "downloading": 23.0, "activating": 30.0,
        "checking": 10.0, "boot": 5.0,
    }  # fmt: skip
    assert alone["seconds"] == 100.0
    release = {
        "commit": "f" * 40, "state": "published", "requested_at": at(0),
        "finished_at": at(110),
        "stages": {"queue": {"seconds": 10.0}, "fetch": {"seconds": 5.0},
                   "build": {"seconds": 80.0}, "publish": {"seconds": 15.0}},
    }  # fmt: skip
    joined = host_cycle("web-1", attempt, [release])
    assert joined["seconds"] == 200.0 and joined["seen_at"] == at(0)
    assert joined["phases"]["building"] == 80.0
    assert joined["phases"]["polling"] == 20.0
    published = release_cycle("web-1", release)
    assert published["kind"] == "nix-release" and published["seconds"] == 110.0


# ------------------------------------------------------------------ report


def test_percentiles_are_nearest_rank() -> None:
    values = [float(n) for n in range(1, 11)]
    assert percentile(values, 0.5) == 5.0
    assert percentile(values, 0.9) == 9.0
    assert percentile([7.0], 0.9) == 7.0
    assert percentile([], 0.5) is None


def test_p50_p90_and_the_slowest_phase_per_target() -> None:
    cycles = [
        done(
            "main",
            100 * n,
            {"building": 60.0 * n, "applying": 10.0},
            commit=f"{n:040d}",
        )
        for n in range(1, 11)
    ]
    cycles.append(done("main", 0, {}, ok=False, state="failed"))
    document = report(cycles, now=T0 + 10_000)
    (main,) = document["targets"]
    assert main["cycles"] == 10 and main["failed"] == 1
    assert main["p50_seconds"] == 500.0 and main["p90_seconds"] == 900.0
    assert main["phases_p50"] == {"applying": 10.0, "building": 300.0}
    assert main["slowest_phase"] == {"name": "building", "p50_seconds": 300.0}
    assert main["slowest_cycle"]["seconds"] == 1000.0
    assert main["slowest_cycle"]["commit"] == f"{10:040d}"
    assert main["slowest_cycle"]["slowest_phase"] == "building"


def test_since_and_target_filter_the_cycles() -> None:
    now = T0 + 100_000
    old = done("main", 50, {}, start=0)
    new = done("main", 70, {}, start=now - 3600, start_end=now - 3600)
    other = done("shop", 30, {}, kind="train", start=now - 60, start_end=now - 60)
    assert parse_since("24h", now) == now - 86400
    assert parse_since("30m", now) == now - 1800
    assert parse_since("2026-10-01T00:00:00Z", now) < now
    with pytest.raises(ValueError):
        parse_since("yesterday", now)
    recent = report([old, new, other], now=now, since=parse_since("2h", now))
    assert [(t["target"], t["cycles"]) for t in recent["targets"]] == [
        ("main", 1),
        ("shop", 1),
    ]
    only = report([old, new, other], now=now, target="shop")
    assert [t["target"] for t in only["targets"]] == ["shop"]
    assert only["targets"][0]["kind"] == "train"


def test_unknown_push_time_is_said() -> None:
    document = report([done("main", 90, {"applying": 30.0})], now=T0 + 1000)
    (main,) = document["targets"]
    assert main["push_unknown"] == 1 and main["first_seen_at"] == at(0)
    lines = "\n".join(text(document))
    assert "push time unknown for 1 of 1 cycle(s)" in lines
    assert f"first at {at(0)}" in lines
    pushed = cycle(
        target="main", kind="environment", commit="a" * 40, seen_at=at(30),
        pushed_at=at(0), checked_at=at(90), ok=True, phases={},
    )  # fmt: skip
    assert pushed["seconds"] == 90.0 and pushed["from"] == "pushed"
    assert report([pushed], now=T0 + 1000)["targets"][0]["push_unknown"] == 0


def test_markdown_is_a_generated_table() -> None:
    document = report(
        [done("main", 250, {"building": 200.0}), done("main", 130, {"building": 90.0})],
        now=T0 + 1000,
    )
    table = markdown(document)
    lines = table.splitlines()
    assert lines[0].startswith("<!-- generated by `piceli cycle report --markdown`")
    assert "not written by hand" in lines[0]
    assert lines[2] == (
        "| Target | Kind | Cycles | p50 | p90 | Slowest phase (p50) | Slowest cycle |"
    )
    assert lines[4] == (
        "| main | environment | 2 | 2m 10s | 4m 10s | building (1m 30s) | "
        "4m 10s (cccccccccccc) |"
    )
    assert "- main: push time unknown for 2 of 2 cycle(s)" in table


def test_collect_reads_every_source() -> None:
    entry = history.event(
        {"state": "deployed", "components": {}}, started_at=at(0),
        finished_at=at(60), trigger="push", approval=None, outcome=None,
        built=[], cycle=done("main", 60, {"applying": 10.0}),
    )  # fmt: skip
    document = {
        "envs": {"main": {"runs": history.environment_runs("main", [entry], [])}},
        "nix_releases": {"web-1": {"runs": [
            {"commit": "f" * 40, "state": "published", "requested_at": at(0),
             "finished_at": at(100), "stages": {"build": {"seconds": 80.0}}}]}},
    }  # fmt: skip
    trains = [
        {"train": "shop", "state": "joined", "ok": True, "started": at(0),
         "duration": 10.0, "trials": []},
        {"train": "shop", "state": "running"},
    ]  # fmt: skip
    hosts = [{"host": "web-1", "commit": "f" * 40, "published_at": at(100),
              "started_at": at(110), "ended_at": at(150), "state": "deployed",
              "stages": []}]  # fmt: skip
    found = collect(history=document, trains=trains, hosts=hosts)
    assert sorted((c["kind"], c["target"]) for c in found) == [
        ("environment", "main"),
        ("nix-host", "web-1"),
        ("nix-release", "web-1"),
        ("train", "shop"),
    ]
    host = next(c for c in found if c["kind"] == "nix-host")
    assert host["seconds"] == 150.0  # from the release's request
    status = {"envs": {"main": {"cycle": done("main", 42, {})}}}
    assert [c["seconds"] for c in collect(status=status)] == [42.0]


def test_the_host_log_is_kept_once_and_bounded(tmp_path: Path) -> None:
    attempt = {"commit": "a" * 40, "started_at": at(0), "ended_at": at(5),
               "state": "deployed", "stages": [{"name": "activate", "seconds": 4,
                                                "output": "secret"}]}  # fmt: skip
    assert local.remember_host("web-1", attempt, tmp_path)
    assert not local.remember_host("web-1", attempt, tmp_path)  # once
    assert not local.remember_host("web-1", {"started_at": at(0)}, tmp_path)
    (kept,) = local.read_hosts(tmp_path)
    assert kept["stages"] == [{"name": "activate", "state": None, "seconds": 4}]
    for n in range(local.MAX_HOSTS + 5):
        local.remember_host("web-1", {**attempt, "started_at": at(10 + n)}, tmp_path)
    kept_all = local.read_hosts(tmp_path)
    assert len(kept_all) == local.MAX_HOSTS
    assert kept_all[-1]["started_at"] == at(10 + local.MAX_HOSTS + 4)


# ------------------------------------------------------------------ command


def _write_sources(tmp_path: Path) -> tuple[Path, Path]:
    from piceli.gitops.state import HISTORY_KEY
    from piceli.train.state import TrainState

    controller = tmp_path / "controller"
    controller.mkdir()
    entries = [
        history.event(
            {"state": "deployed", "components": {}}, started_at=at(n * 1000),
            finished_at=at(n * 1000 + 100 + n), trigger="push", approval=None,
            outcome=None, built=[],
            cycle=done("main", 100 + n, {"building": 60.0 + n, "applying": 20.0},
                       start=n * 1000, start_end=n * 1000),
        )
        for n in range(3)
    ]  # fmt: skip
    (controller / HISTORY_KEY).write_text(
        json.dumps(
            {
                "schema": history.SCHEMA,
                "envs": {
                    "main": {"runs": history.environment_runs("main", entries, [])}
                },
            }
        )
    )
    (controller / "status.json").write_text(json.dumps({"envs": {}}))
    trains = tmp_path / "trains"
    state = TrainState(trains / "shop")
    state.write_record(
        "20270115T080000Z-abcdef",
        {"train": "shop", "state": "joined", "ok": True, "started": at(0),
         "duration": 30.0, "head": "e" * 40,
         "trials": [{"steps": [{"name": "unit", "kind": "command",
                                "duration": 25.0}]}]},
    )  # fmt: skip
    return controller, trains


def test_the_command_reports_text_json_and_markdown(tmp_path: Path) -> None:
    controller, trains = _write_sources(tmp_path)
    base = ["cycle", "report", "--state-dir", str(controller),
            "--train-state-dir", str(trains)]  # fmt: skip
    result = CliRunner().invoke(app, [*base, "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["schema"] == "piceli.cycle-report.v1"
    assert [(t["kind"], t["target"], t["cycles"]) for t in body["targets"]] == [
        ("environment", "main", 3),
        ("train", "shop", 1),
    ]
    assert body["targets"][0]["p50_seconds"] == 101.0
    assert body["targets"][0]["slowest_phase"]["name"] == "building"
    assert body["targets"][1]["slowest_phase"]["name"] == "testing unit"
    assert "push time unknown" in result.stderr
    result = CliRunner().invoke(app, [*base, "--markdown", "--target", "shop"])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("<!-- generated by")
    assert "| shop | train | 1 |" in result.stdout and "| main |" not in result.stdout


def test_the_command_without_a_controller_says_so(tmp_path: Path) -> None:
    _, trains = _write_sources(tmp_path)
    result = CliRunner().invoke(
        app, ["cycle", "report", "--train-state-dir", str(trains), "--json"]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert [t["target"] for t in body["targets"]] == ["shop"]
    assert body["sources"]["controller"].startswith("not read")
    assert "controller: not read" in result.stderr


def test_the_command_refuses_bad_options(tmp_path: Path) -> None:
    for args in (["--since", "yesterday"], ["--json", "--markdown"]):
        result = CliRunner().invoke(app, ["cycle", "report", *args])
        assert result.exit_code == 2
        assert json.loads(result.stdout)["reason"] == "cycle-options-invalid"
