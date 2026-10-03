"""Acceptance: ``piceli watch`` follows a deploy run from its journal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from piceli.k8s.cli.watch import follow
from tests.acceptance.test_deploy_pipeline import deploy
from tests.acceptance.test_deploy_pipeline_checks import probed  # noqa: F401

SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "schemas"
        / "piceli-watch-event-v1.schema.json"
    ).read_text()
)
VALIDATOR = jsonschema.validators.validator_for(SCHEMA)(SCHEMA)
VALIDATOR.check_schema(SCHEMA)


def watch(tmp_path: Path, *args: str) -> tuple[int, list[dict[str, Any]]]:
    result = CliRunner().invoke(
        cli, ["watch", "--state-dir", str(tmp_path / "state"), *args]
    )
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    for line in lines:
        VALIDATOR.validate(line)
    return result.exit_code, lines


def test_watch_reports_a_finished_run_as_json_lines(shop) -> None:
    api, tmp_path = shop
    code, _, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stderr
    code, lines = watch(tmp_path, "--json")
    assert code == 0
    assert [line["event"] for line in lines] == ["snapshot", "result"]
    snapshot, final = lines
    assert snapshot["state"] == "ready" and final["state"] == "ready"
    assert set(snapshot["stages"].values()) <= {"done", "skipped"}
    assert final["release"] and final["resumable"] is False
    assert Path(final["summary"]["json"]).is_file()


def test_watch_ends_1_on_a_failed_run_and_says_it_is_resumable(probed) -> None:  # noqa: F811
    api, tmp_path = probed
    (tmp_path / "mode").write_text("nope")
    code, _, _ = deploy(tmp_path, "--auto-approve")
    assert code == 1
    code, lines = watch(tmp_path, "--json")
    assert code == 1
    final = lines[-1]
    assert final["event"] == "result" and final["state"] != "ready"
    assert final["reason"] == "pipeline-checks-failed"
    assert final["resumable"] is (final["state"] != "rolled-back")


def test_watch_without_a_run_is_rejected(tmp_path: Path) -> None:
    code, lines = watch(tmp_path, "--json")
    assert code == 2
    assert lines[-1]["reason"] == "watch-no-run"


def test_watch_once_prints_the_current_state_of_an_unfinished_run(shop) -> None:
    api, tmp_path = shop
    deploy(tmp_path, "--auto-approve")
    path = sorted((tmp_path / "state" / "runs").glob("*.json"))[-1]
    data = json.loads(path.read_text())
    data["state"] = "running"
    data["stages"]["checks"] = {"state": "running"}
    path.write_text(json.dumps(data))
    code, lines = watch(tmp_path, "--json", "--once")
    assert code == 0 and lines[-1]["state"] == "running"


def test_watch_times_out_on_a_run_that_never_settles(shop) -> None:
    api, tmp_path = shop
    deploy(tmp_path, "--auto-approve")
    path = sorted((tmp_path / "state" / "runs").glob("*.json"))[-1]
    data = json.loads(path.read_text())
    data["state"] = "running"
    path.write_text(json.dumps(data))
    code, lines = watch(tmp_path, "--json", "--timeout", "0.2", "--interval", "0.05")
    assert code == 1
    assert (
        lines[-1]["reason"] == "watch-timeout" and lines[-1]["run_state"] == "running"
    )


def test_a_deploy_journals_its_progress_lines_for_watch(shop) -> None:
    api, tmp_path = shop
    api.ready = False
    code, _, _ = deploy(tmp_path, "--auto-approve")  # readiness times out
    assert code == 1
    data = json.loads(
        sorted((tmp_path / "state" / "runs").glob("*.json"))[-1].read_text()
    )
    assert data["progress"] and all(
        {"n", "at", "stage", "line"} <= set(item) for item in data["progress"]
    )
    code, lines = watch(tmp_path, "--json", "--once")
    assert lines[0]["event"] == "snapshot"


def journal(state: str, stages: dict[str, str], progress: list[str]) -> dict[str, Any]:
    return {
        "run_id": "r1",
        "state": state,
        "stages": {name: {"state": value} for name, value in stages.items()},
        "progress": [
            {"n": i + 1, "stage": "apply", "line": line, "at": "t"}
            for i, line in enumerate(progress)
        ],
    }


def test_follow_streams_each_change_once_and_stops_when_settled() -> None:
    reads = iter(
        [
            journal("running", {"apply": "running", "checks": "pending"}, []),
            journal("running", {"apply": "running", "checks": "pending"}, ["a"]),
            journal("running", {"apply": "running", "checks": "pending"}, ["a", "b"]),
            journal("running", {"apply": "done", "checks": "running"}, ["a", "b"]),
            journal("ready", {"apply": "done", "checks": "done"}, ["a", "b"]),
        ]
    )
    events: list[dict[str, Any]] = []
    last = follow(
        lambda: next(reads),
        events.append,
        interval=0,
        timeout=None,
        sleep=lambda _s: None,
    )
    assert last["state"] == "ready"
    seen = [
        (e["event"], e.get("line") or e.get("stage"), e.get("state")) for e in events
    ]
    assert seen == [
        ("snapshot", None, "running"),
        ("progress", "a", None),
        ("progress", "b", None),
        ("stage", "apply", "done"),
        ("stage", "checks", "running"),
        ("stage", "checks", "done"),
        ("run", None, "ready"),
    ]
