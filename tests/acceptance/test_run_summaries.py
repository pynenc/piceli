"""Acceptance: every deploy run writes ``runs/<id>/summary.{json,md}``;
``piceli runs`` lists them; ``cache_budget=`` prunes after a run.

Against the fake API with the fake build/delivery backend of
``test_deploy_pipeline``.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import jsonschema
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from tests.acceptance.test_deploy_pipeline import FakeBackend, deploy, with_checks

SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "schemas"
        / "piceli-run-summary-v1.schema.json"
    ).read_text()
)


def summary_of(result: dict[str, Any]) -> tuple[dict[str, Any], str]:
    paths = result["summary"]
    document = json.loads(Path(paths["json"]).read_text())
    jsonschema.validate(document, SCHEMA)
    return document, Path(paths["markdown"]).read_text()


def runs(tmp_path: Path, *args: str) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(cli, ["runs", str(tmp_path / "app.py:pipeline"), *args])
    body = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, body, result.stderr


def test_a_ready_run_writes_its_summary_and_runs_lists_it(shop) -> None:
    api, tmp_path = shop
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    final = events[-1]
    document, markdown = summary_of(final)
    state = tmp_path / "state"
    assert Path(final["summary"]["json"]) == (
        state / "runs" / final["run_id"] / "summary.json"
    )
    assert document["state"] == "ready" and document["failure"] is None
    assert document["run_id"] == final["run_id"]
    assert document["combined_hash"] == final["combined_hash"]
    assert document["release"] == final["release"]
    assert document["pipeline"]["app"] == "shop"
    web = document["images"]["web"]
    assert web["config_digest"] == FakeBackend.image_id
    assert web["reference"].startswith("registry.example:5000/shop/web@sha256:")
    assert web["delivery"]["action"] == "pushed"
    plan = document["plan"]
    assert "create" in plan["classes"] and plan["counts"]["create"] >= 2
    assert {"operation": "create", "kind": "Deployment", "name": "web"} in plan[
        "changes"
    ]
    assert set(document["stages"]) == {
        "inputs",
        "build",
        "deliver",
        "plan",
        "apply",
        "checks",
    }
    assert all("seconds" in stage for stage in document["stages"].values())
    assert markdown.startswith("### piceli deploy `shop`: ready")
    assert "#### Images" in markdown and "#### Plan" in markdown
    assert "create `Deployment/web`" in markdown
    assert "| apply | done |" in markdown
    # Never a secret value (the generated password), never the kubeconfig path.
    secret = next(
        value
        for (kind, _), value in api.objects.items()
        if kind == "Secret" and "password" in value.get("data", {})
    )["data"]["password"]
    plain = base64.b64decode(secret).decode()
    for text in (json.dumps(document), markdown):
        assert secret not in text and plain not in text
        assert "kubeconfig" not in text

    code, body, stderr = runs(tmp_path, "--json")
    assert code == 0, stderr
    assert body["schema"] == "piceli.runs.v1"
    [listed] = body["runs"]
    assert listed["run_id"] == final["run_id"] and listed["state"] == "ready"
    assert listed["summary"] == final["summary"]
    assert final["run_id"] in stderr


def test_a_second_run_summarises_changed_fields(shop) -> None:
    _api, tmp_path = shop
    assert deploy(tmp_path, "--auto-approve")[0] == 0
    (tmp_path / "src" / "main.txt").write_text("v2\n")
    FakeBackend.image_id = "sha256:" + "2" * 64
    code, events, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    document, markdown = summary_of(events[-1])
    [change] = [c for c in document["plan"]["changes"] if c["name"] == "web"]
    assert change["operation"] == "apply"
    assert change["changed_fields"] >= 1
    assert any("image" in field for field in change["fields"])
    assert "apply `Deployment/web`: `/spec/template" in markdown
    code, body, _ = runs(tmp_path, "--json", "--limit", "1")
    assert code == 0 and len(body["runs"]) == 1
    assert body["runs"][0]["run_id"] == events[-1]["run_id"]


def test_a_failed_run_summarises_its_cause(shop) -> None:
    _api, tmp_path = shop
    FakeBackend.fail_delivery.append("registry-unreachable")
    code, events, result = deploy(tmp_path, "--auto-approve")
    assert code == 1, result.stdout + result.stderr
    document, markdown = summary_of(events[-1])
    assert document["state"] == "failed"
    assert document["failure"]["stage"] == "deliver"
    assert document["failure"]["reason"] == "registry-unreachable"
    assert document["failure"]["explain"] == "piceli explain registry-unreachable"
    assert document["plan"] is None or document["stages"]["plan"]["state"] == "pending"
    assert "#### Failure" in markdown and "`registry-unreachable`" in markdown

    # --resume rewrites the same run's summary once it is ready.
    code, events, result = deploy(tmp_path, "--resume")
    assert code == 0, result.stdout + result.stderr
    resumed, _ = summary_of(events[-1])
    assert resumed["run_id"] == document["run_id"]
    assert resumed["state"] == "ready" and resumed["failure"] is None


def test_failed_checks_are_in_the_summary(shop) -> None:
    _api, tmp_path = shop
    with_checks(tmp_path)
    assert deploy(tmp_path, "--auto-approve")[0] == 0
    (tmp_path / "src" / "main.txt").write_text("v2\n")
    FakeBackend.image_id = "sha256:" + "2" * 64
    FakeBackend.passing[0] = False
    code, events, _ = deploy(tmp_path, "--auto-approve")
    assert code == 1
    document, markdown = summary_of(events[-1])
    assert document["state"] == "rolled-back"
    assert document["failure"]["stage"] == "checks"
    assert document["failure"]["reason"] == "pipeline-checks-failed"
    assert document["checks"]["passed"] is False
    assert document["checks"]["rollback"]["state"] == "ready"
    assert "#### Checks" in markdown and "FAILED" in markdown


def test_cache_budget_prunes_old_runs_after_a_run(shop) -> None:
    _api, tmp_path = shop
    text = (tmp_path / "app.py").read_text()
    (tmp_path / "app.py").write_text(
        text.replace('state_dir="state",', 'state_dir="state", cache_budget=1,')
    )
    for _ in range(3):
        code, events, result = deploy(tmp_path, "--auto-approve", "--reapply")
        assert code == 0, result.stdout + result.stderr
    final = events[-1]
    assert final["cache"]["budget_bytes"] == 1
    assert final["cache"]["over_budget"] is True  # the release state is never pruned
    remaining = sorted((tmp_path / "state" / "runs").glob("*.json"))
    # The latest run stays; so does the latest run of each applied release
    # (all three applied the same release: only the newest is kept).
    assert [path.stem for path in remaining] == [final["run_id"]]
    assert (tmp_path / "state" / "release" / "catalog.json").is_file()
    assert (tmp_path / "state" / "release" / "secrets.sqlite").is_file()
    assert (tmp_path / "state" / "builds" / "shop" / "receipt.json").is_file()
    assert "[cache]" in result.stderr
