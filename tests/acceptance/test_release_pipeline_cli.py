"""Acceptance: ``piceli release … --spec MODULE:ATTR`` operates a pipeline's release.

The pipeline is deployed with ``piceli deploy`` (fake build/delivery backend,
real release engine against the in-process fake API); every ``release``
subcommand then resolves the same state directory, release name, target and
composition from the pipeline declaration. Nothing is built by the release
commands: a rollback re-applies the recorded image digests.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from tests.acceptance.test_deploy_pipeline import FakeBackend, deploy, image


def release(tmp_path: Path, *args: str) -> tuple[int, dict[str, Any], Any]:
    result = CliRunner().invoke(cli, ["release", *args])
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    document = json.loads(result.stdout) if lines else {}
    return result.exit_code, document, result


def target(tmp_path: Path) -> str:
    return f"{tmp_path / 'app.py'}:pipeline"


def deployed_twice(api: Any, tmp_path: Path) -> tuple[str, str]:
    """Deploy v1, then a changed source (v2); return the two images."""
    code, _, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    first = image(api)
    (tmp_path / "src" / "main.txt").write_text("v2\n")
    FakeBackend.image_id = "sha256:" + "2" * 64
    code, _, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    assert image(api) != first
    return first, image(api)


def test_status_rollback_and_secret_of_a_deployed_pipeline(shop) -> None:
    api, tmp_path = shop
    first, second = deployed_twice(api, tmp_path)
    calls = list(FakeBackend.calls)

    code, status, result = release(tmp_path, "status", "--spec", target(tmp_path))
    assert code == 0, result.stdout + result.stderr
    names = [item["name"] for item in status["releases"]]
    assert len(names) == 2 and all(name.startswith("shop-") for name in names)
    assert status["deployed"] != status["previous"]
    assert {status["deployed"], status["previous"]} == set(names)
    previous = status["previous"]

    # Rollback plans the previous release; approval is required first.
    code, planned, result = release(
        tmp_path, "rollback", "previous", "--spec", target(tmp_path)
    )
    assert code == 3, result.stdout + result.stderr
    assert planned["state"] == "approval-required"
    assert planned["release"] == previous and planned["intent"] == "rollback"
    assert planned["source"]["kind"] == "oci-set"  # the recorded image digests
    assert f"--spec {target(tmp_path)} --approve" in result.stderr
    assert image(api) == second
    code, outcome, result = release(
        tmp_path,
        "rollback",
        "previous",
        "--spec",
        target(tmp_path),
        "--approve",
        planned["plan_hash"],
    )
    assert code == 0, result.stdout + result.stderr
    assert outcome["state"] == "succeeded" and outcome["release"] == previous
    assert image(api) == first
    assert FakeBackend.calls == calls  # nothing was built or delivered

    code, status, _ = release(tmp_path, "status", "--spec", target(tmp_path))
    assert code == 0 and status["deployed"] == previous

    # The pipeline's generated secret, from the same secret store.
    code, metadata, result = release(
        tmp_path, "secret", "show", "password", "--spec", target(tmp_path), "--json"
    )
    assert code == 0, result.stdout + result.stderr
    assert "values" not in metadata and metadata["secret"] == "password"
    code, revealed, _ = release(
        tmp_path,
        "secret",
        "show",
        "password",
        "--spec",
        target(tmp_path),
        "--json",
        "--reveal",
    )
    assert code == 0
    assert set(revealed["values"]) == {"password"}
    assert all(len(value) >= 24 for value in revealed["values"].values())

    # Nothing is running, so there is nothing to stop.
    code, refused, _ = release(tmp_path, "stop", "--spec", target(tmp_path))
    assert code == 2 and refused["reason"] == "nothing-to-stop"

    # A later deploy converges on the pipeline's current release again.
    code, events, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["stages"]["apply"] == "done"
    assert image(api) == second


def test_plan_and_diff_use_the_delivered_images_and_the_same_release(shop) -> None:
    api, tmp_path = shop
    code, events, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    deployed = events[-1]["release"]

    code, planned, result = release(tmp_path, "plan", "--spec", target(tmp_path))
    assert code == 0, result.stdout + result.stderr
    assert planned["state"] == "planned"
    assert planned["release"] == deployed  # the same fingerprint as deploy
    code, diffed, result = release(tmp_path, "diff", "--spec", target(tmp_path))
    assert code == 0, result.stdout + result.stderr
    assert diffed["state"] == "diffed" and not diffed["changes"]

    # Changed sources are not built by release commands.
    (tmp_path / "src" / "main.txt").write_text("v2\n")
    for command in ("plan", "diff", "apply"):
        code, refused, _ = release(tmp_path, command, "--spec", target(tmp_path))
        assert code == 2, command
        assert refused["state"] == "rejected"
        assert refused["reason"] == "pipeline-not-delivered"
    # Existing releases can still be operated.
    code, status, _ = release(tmp_path, "status", "--spec", target(tmp_path))
    assert code == 0 and status["deployed"] == deployed


def test_stop_and_resume_an_interrupted_pipeline_apply(shop) -> None:
    api, tmp_path = shop
    api.ready = False
    code, events, result = deploy(tmp_path, "--auto-approve")
    assert code == 1, result.stdout + result.stderr
    assert events[-1]["reason"] == "pipeline-apply-not-ready"
    api.ready = True
    code, outcome, result = release(tmp_path, "resume", "--spec", target(tmp_path))
    assert code == 0, result.stdout + result.stderr
    assert outcome["state"] == "succeeded"
    assert outcome["release"] == events[-1]["release"]


def test_mutating_commands_wait_for_a_running_deploy(shop) -> None:
    from piceli.pipeline.journal import Journal

    api, tmp_path = shop
    code, _, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    with Journal(tmp_path / "state").locked():
        code, refused, _ = release(
            tmp_path, "rollback", "previous", "--spec", target(tmp_path)
        )
        assert code == 2 and refused["reason"] == "pipeline-locked"
        # Read-only commands do not take the lock.
        code, _, _ = release(tmp_path, "status", "--spec", target(tmp_path))
        assert code == 0


def test_unknown_targets_are_rejected_with_codes(shop, tmp_path_factory) -> None:
    api, tmp_path = shop
    code, refused, _ = release(
        tmp_path, "status", "--spec", f"{tmp_path / 'missing.py'}:pipeline"
    )
    assert code == 2 and refused["reason"] == "pipeline-not-found"
    code, refused, _ = release(
        tmp_path, "status", "--spec", f"{tmp_path / 'app.py'}:app"
    )
    assert code == 2 and refused["reason"] == "pipeline-not-found"
    code, refused, _ = release(
        tmp_path, "status", "--spec", str(tmp_path / "missing.toml")
    )
    assert code == 2 and refused["reason"] == "invalid-release-spec"
    # Before any deploy: no release yet, and nothing delivered to plan with.
    code, status, _ = release(tmp_path, "status", "--spec", target(tmp_path))
    assert code == 0 and status["releases"] == []
    code, refused, _ = release(tmp_path, "plan", "--spec", target(tmp_path))
    assert code == 2 and refused["reason"] == "pipeline-not-delivered"


def test_status_and_dashboard_of_a_pipeline_see_its_release(shop) -> None:
    from piceli.k8s.access import resolve_target
    from piceli.k8s.cli import access as cli_access

    api, tmp_path = shop
    code, events, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    deployed = events[-1]["release"]

    resolved = resolve_target(target(tmp_path), tmp_path)
    assert resolved.spec is not None  # the dashboard's catalog and history
    assert resolved.spec.catalog_path.exists()
    assert resolved.spec.state_dir == tmp_path / "state" / "release"

    result = CliRunner().invoke(cli, ["status", target(tmp_path), "--json"])
    document = json.loads(result.stdout)
    assert document["release"]["current"] == deployed
    assert document["release"]["state"] == "ready"
    text = cli_access._human_status(document, target(tmp_path))
    assert f"release    {deployed}  ready" in text
    rows = [line for line in text.splitlines() if "Deployment/" in line]
    assert len(rows) == 2
    # Columns line up whatever the workload names' lengths.
    assert len({row.index("Deployment/") for row in rows}) == 1
    counts = {re.search(r" (\d+/\d+|-) ", row).start() for row in rows}  # type: ignore[union-attr]
    assert len(counts) == 1, rows
