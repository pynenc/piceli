"""Acceptance: a resumed run reports its own final outcome, not the earlier one."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.acceptance.test_deploy_pipeline import FakeBackend, deploy
from tests.acceptance.test_deploy_pipeline_checks import probed  # noqa: F401


def _run(tmp_path: Path) -> dict[str, Any]:
    path = sorted((tmp_path / "state" / "runs").glob("*.json"))[-1]
    return json.loads(path.read_text())


def _summary(tmp_path: Path) -> tuple[dict[str, Any], str]:
    directory = sorted((tmp_path / "state" / "runs").glob("*.json"))[-1].with_suffix("")
    return (
        json.loads((directory / "summary.json").read_text()),
        (directory / "summary.md").read_text(),
    )


def test_resume_after_an_interruption_reports_ready_everywhere(probed) -> None:  # noqa: F811
    api, tmp_path = probed
    FakeBackend.image_id = "sha256:" + "4" * 64
    (tmp_path / "mode").write_text("interrupt")
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 1 and events[-1]["state"] == "interrupted", result.stderr
    (tmp_path / "mode").write_text("pass")

    code, events, result = deploy(tmp_path, "--resume", "--json")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["state"] == "ready"
    run = _run(tmp_path)
    assert run["state"] == "ready" and "reason" not in run
    summary, markdown = _summary(tmp_path)
    assert summary["state"] == "ready" and summary["failure"] is None
    assert all(stage["state"] == "done" for stage in summary["stages"].values())
    assert "interrupted" not in json.dumps(summary)
    assert "interrupted" not in markdown and "Failure" not in markdown


def test_resume_after_a_failure_drops_the_earlier_reason(probed) -> None:  # noqa: F811
    api, tmp_path = probed
    FakeBackend.image_id = "sha256:" + "5" * 64
    (tmp_path / "mode").write_text("nope")
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 1, result.stderr
    (tmp_path / "mode").write_text("pass")
    api.ready = True

    code, events, result = deploy(tmp_path, "--resume", "--json")
    assert code == 0, result.stdout + result.stderr
    run = _run(tmp_path)
    assert run["state"] == "ready" and "reason" not in run, run.get("reason")
    assert "reason" not in run["stages"]["checks"], run["stages"]["checks"]
    summary, markdown = _summary(tmp_path)
    assert summary["state"] == "ready" and "reason" not in summary
    assert "reason" not in summary["stages"]["checks"]
    assert "pipeline-checks-failed" not in markdown
