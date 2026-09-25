"""Acceptance: ``[[checks]]`` gate a release and roll it back automatically.

Runs ``piceli release`` against the loopback fake API. The checks are Python
checks: one inspects the release's images through the ``CheckContext``, the
other reads the live Deployment through the context's explicit API client.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from tests.acceptance.test_release_cli import (  # noqa: F401 (fixture)
    DIGEST_1,
    DIGEST_2,
    _image,
    _receipt,
    _run,
    release_env,
)

CHECKS = """
from kubernetes.client import AppsV1Api

BROKEN = "sha256:" + "2" * 64


def image_is_good(ctx):
    if BROKEN in ctx.images["api"]:
        return "the api image is the broken build"
    return None


def live_image_is_good(ctx):
    deployment = AppsV1Api(ctx.api_client()).read_namespaced_deployment(
        "worker", ctx.namespace
    )
    return BROKEN not in deployment.spec.template.spec.containers[0].image
"""

CHECK_TABLES = """
[[checks]]
type = "python"
name = "image"
call = "checks.py:image_is_good"
retries = 0

[[checks]]
type = "python"
name = "live-image"
call = "checks.py:live_image_is_good"
retries = 0
"""


def _with_checks(tmp_path: Path, *, rollback: bool) -> None:
    (tmp_path / "checks.py").write_text(CHECKS)
    spec = tmp_path / "release.toml"
    text = spec.read_text()
    if rollback:
        text = text.replace(
            'state_dir = "state"\n',
            'state_dir = "state"\nrollback_on_failed_checks = true\n',
        )
    spec.write_text(text + textwrap.dedent(CHECK_TABLES))


def _history(tmp_path: Path) -> list[dict]:
    return json.loads((tmp_path / "state" / "history.json").read_text())["entries"]


def test_failed_checks_roll_back_automatically(release_env):  # noqa: F811
    api, tmp_path = release_env
    _with_checks(tmp_path, rollback=True)

    code, planned, result = _run(tmp_path, "plan")
    assert code == 0, planned
    assert "checks after readiness: image, live-image; on failure" in result.stderr
    assert planned["checks"] == {
        "names": ["image", "live-image"],
        "rollback_on_failed_checks": True,
    }
    code, first, result = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, result.output
    assert first["release_state"] == "ready"
    assert first["checks"]["passed"] is True
    assert [item["name"] for item in first["checks"]["results"]] == [
        "image",
        "live-image",
    ]
    assert "check image: passed" in result.stderr

    # A broken image becomes ready, fails its checks and is rolled back.
    _receipt(tmp_path / "build.receipt.json", DIGEST_2)
    code, broken, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 1, result.output
    assert broken["execution"]["state"] == "ready"
    assert broken["release_state"] == "checks-failed"
    assert broken["checks"]["failed"] == ["image", "live-image"]
    failure = broken["checks"]["results"][0]
    assert failure["code"] == "check-failed"
    assert failure["detail"] == "the api image is the broken build"
    rollback = broken["rollback"]
    assert rollback["state"] == "rolled-back"
    assert rollback["target"] == first["release"]
    assert rollback["release_state"] == "ready"
    assert rollback["checks"]["passed"] is True
    assert rollback["selected"] == first["release"]
    assert _image(api) == f"registry.example/app/api@{DIGEST_1}"
    assert "rolled back automatically to" in result.stderr

    history = _history(tmp_path)
    failed_entry, rollback_entry = history[-2:]
    assert failed_entry["release"] == broken["release"]
    assert failed_entry["state"] == "checks-failed"
    assert failed_entry["checks"] == {
        "passed": False,
        "failed": ["image", "live-image"],
        "count": 2,
    }
    assert failed_entry["rollback"] == {
        "state": "rolled-back",
        "target": first["release"],
        "execution_id": rollback["execution"]["execution_id"],
    }
    assert rollback_entry["intent"] == "rollback"
    assert rollback_entry["trigger"] == "checks-failed"
    assert rollback_entry["rolled_back_from"] == broken["release"]
    assert rollback_entry["failed_execution_id"] == failed_entry["execution_id"]
    assert rollback_entry["state"] == "ready"
    report = json.loads(
        (
            tmp_path / "state" / "checks" / f"{failed_entry['execution_id']}.json"
        ).read_text()
    )
    assert report["release"] == broken["release"] and report["passed"] is False

    code, status, result = _run(tmp_path, "status")
    assert code == 0
    assert status["deployed"] == first["release"]
    assert status["selected"] == first["release"]
    by_name = {item["name"]: item for item in status["releases"]}
    assert by_name[broken["release"]]["checks"]["passed"] is False
    assert by_name[broken["release"]]["checks"]["state"] == "checks-failed"
    assert by_name[first["release"]]["checks"]["passed"] is True
    assert "checks FAILED: image, live-image" in result.stderr

    # `release check` runs the checks now and changes nothing.
    code, checked, _ = _run(tmp_path, "check")
    assert code == 0, checked
    assert checked["release"] == first["release"]
    assert checked["state"] == "succeeded" and "reason" not in checked
    code, checked, result = _run(tmp_path, "check", "--release", broken["release"])
    assert code == 1
    assert checked["state"] == "failed" and checked["reason"] == "check-failed"
    assert checked["checks"]["failed"] == ["image"]  # live image is the good one
    assert "[check-failed]" in result.stderr
    assert _history(tmp_path) == history


def test_without_auto_rollback_the_failed_release_stays_unselected(release_env):  # noqa: F811
    api, tmp_path = release_env
    _with_checks(tmp_path, rollback=False)
    code, first, _ = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, first

    _receipt(tmp_path / "build.receipt.json", DIGEST_2)
    code, broken, _ = _run(tmp_path, "apply", "--auto-approve")
    assert code == 1
    assert broken["release_state"] == "checks-failed"
    assert "rollback" not in broken
    assert broken["selected"] == first["release"]
    assert _image(api) == f"registry.example/app/api@{DIGEST_2}"

    # `previous` is the last ready release; the manual rollback checks too.
    code, rolled, _ = _run(tmp_path, "rollback", "previous", "--auto-approve")
    assert code == 0, rolled
    assert rolled["release"] == first["release"]
    assert rolled["release_state"] == "ready" and rolled["checks"]["passed"]
    assert _image(api) == f"registry.example/app/api@{DIGEST_1}"

    # A re-apply of a release that fails its checks does not stay selected.
    code, pending, _ = _run(tmp_path, "rollback", broken["release"])
    assert code == 3
    code, again, _ = _run(
        tmp_path, "rollback", broken["release"], "--approve", pending["plan_hash"]
    )
    assert code == 1 and again["release_state"] == "checks-failed"
    assert again["selected"] == first["release"]


def test_skip_checks_is_recorded(release_env):  # noqa: F811
    api, tmp_path = release_env
    _with_checks(tmp_path, rollback=True)
    code, first, _ = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, first
    _receipt(tmp_path / "build.receipt.json", DIGEST_2)
    code, planned, _ = _run(tmp_path, "plan")
    code, skipped, result = _run(
        tmp_path, "apply", "--approve", planned["plan_hash"], "--skip-checks"
    )
    assert code == 0, result.output
    assert skipped["release_state"] == "ready"
    assert skipped["checks"] == {
        "skipped": True,
        "flag": "--skip-checks",
        "declared": ["image", "live-image"],
    }
    assert "rollback" not in skipped
    assert _image(api) == f"registry.example/app/api@{DIGEST_2}"
    entry = _history(tmp_path)[-1]
    assert entry["skip_checks"] is True
    assert entry["checks"] == {"skipped": True, "flag": "--skip-checks"}
    assert "checks skipped (--skip-checks)" in result.stderr


def test_first_release_failing_checks_has_nothing_to_roll_back_to(release_env):  # noqa: F811
    _api, tmp_path = release_env
    _with_checks(tmp_path, rollback=True)
    _receipt(tmp_path / "build.receipt.json", DIGEST_2)
    code, broken, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 1, result.output
    assert broken["release_state"] == "checks-failed"
    assert broken["rollback"]["state"] == "unavailable"
    assert broken["rollback"]["reason"] == "checks-rollback-unavailable"
    assert _history(tmp_path)[-1]["rollback"]["reason"] == (
        "checks-rollback-unavailable"
    )
    code, status, _ = _run(tmp_path, "status")
    assert status["deployed"] is None


@pytest.mark.parametrize(
    ("tables", "message"),
    [
        ('[[checks]]\ntype = "http"\ntarget = "job/x"\n', "target must be"),
        ('[[checks]]\ntype = "nope"\n', "checks.0"),
        (
            '[[checks]]\ntype = "python"\ncall = "a.py:f"\n'
            '[[checks]]\ntype = "python"\ncall = "b.py:f"\n',
            "two checks are named",
        ),
    ],
)
def test_invalid_checks_are_refused_before_planning(release_env, tables, message):  # noqa: F811
    _api, tmp_path = release_env
    spec = tmp_path / "release.toml"
    spec.write_text(spec.read_text() + tables)
    code, refused, result = _run(tmp_path, "plan")
    assert code == 2, result.output
    assert refused["reason"] == "invalid-release-spec"
    assert message in refused["message"]


def test_unimportable_python_check_is_refused_at_plan(release_env):  # noqa: F811
    api, tmp_path = release_env
    spec = tmp_path / "release.toml"
    spec.write_text(
        spec.read_text() + '[[checks]]\ntype = "python"\ncall = "missing.py:run"\n'
    )
    code, refused, _ = _run(tmp_path, "plan")
    assert code == 2
    assert refused["reason"].startswith("check-callable-invalid")
    assert ("Deployment", "worker") not in api.objects


def test_resume_runs_the_checks_and_rolls_back(release_env):  # noqa: F811
    api, tmp_path = release_env
    _with_checks(tmp_path, rollback=True)
    code, first, _ = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, first

    _receipt(tmp_path / "build.receipt.json", DIGEST_2)
    api.ready = False
    code, applied, _ = _run(tmp_path, "apply", "--auto-approve")
    assert code == 1
    assert applied["release_state"] == "failed"
    assert applied["checks"] is None and "rollback" not in applied  # not ready

    api.ready = True
    code, resumed, _ = _run(tmp_path, "resume")
    assert code == 1
    assert resumed["execution"]["state"] == "ready"
    assert resumed["release_state"] == "checks-failed"
    assert resumed["rollback"]["state"] == "rolled-back"
    assert resumed["rollback"]["target"] == first["release"]
    assert _image(api) == f"registry.example/app/api@{DIGEST_1}"
    code, status, _ = _run(tmp_path, "status")
    assert status["selected"] == first["release"]
    assert [entry["state"] for entry in status["history"]] == [
        "ready",
        "failed",
        "checks-failed",
        "ready",
    ]
