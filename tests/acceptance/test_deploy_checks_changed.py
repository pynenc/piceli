"""Acceptance: a changed check set verifies a release whose manifests did not change.

The fake backend of ``test_deploy_pipeline`` (builds, deliveries and the
check runner in memory) against the in-process fake API. Changing, adding
or removing a check changes the combined hash but not the release: the next
deploy applies nothing, runs the checks against the running release (a
verification), and a failing check never rolls that release back.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.acceptance.test_deploy_pipeline import FakeBackend, deploy, image


def set_checks(tmp_path: Path, count: int) -> None:
    """Declare ``count`` fake checks (each one changes the check set)."""
    text = (tmp_path / "app.py").read_text()
    head, _, rest = text.partition("CHECKS = ")
    _, _, tail = rest.partition("\n")
    checks = ", ".join(["Check()"] * count)
    (tmp_path / "app.py").write_text(
        head
        + "from tests.acceptance.test_deploy_pipeline import Check\n"
        + f"CHECKS = [{checks}]\n"
        + tail
    )
    # The CLI runs in this process: import the edited module again.
    path = str((tmp_path / "app.py").resolve())
    sys.modules.pop(
        "_piceli_render_" + hashlib.sha256(path.encode()).hexdigest()[:16], None
    )


def last_run(tmp_path: Path) -> dict[str, Any]:
    path = sorted((tmp_path / "state" / "runs").glob("*.json"))[-1]
    return dict(json.loads(path.read_text()))


def releases(tmp_path: Path) -> int:
    catalog = json.loads((tmp_path / "state" / "release" / "catalog.json").read_text())
    return len(catalog["releases"])


def test_a_changed_check_set_verifies_the_running_release(shop) -> None:
    api, tmp_path = shop
    set_checks(tmp_path, 1)
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["stages"]["checks"] == "done"
    assert "verification" not in events[-1]
    good, count, calls = image(api), releases(tmp_path), list(FakeBackend.calls)
    assert len(FakeBackend.contexts) == 1

    # Unchanged: the verified release is not checked again.
    code, events, _ = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0 and events[-1]["stages"]["checks"] == "skipped"
    assert len(FakeBackend.contexts) == 1

    # A check added: nothing applied, the checks run against the release.
    set_checks(tmp_path, 2)
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    final = events[-1]
    assert final["state"] == "ready"
    assert final["stages"]["apply"] == "skipped"
    assert final["stages"]["checks"] == "done"
    verification = final["verification"]
    assert verification["trigger"] == "checks-changed"
    assert verification["applied"] is False and verification["passed"] is True
    assert verification["checks_hash"].startswith("sha256:")
    assert len(FakeBackend.contexts) == 2
    assert "verifying the running release (checks-changed)" in result.stderr
    assert image(api) == good and releases(tmp_path) == count

    # The same check set again: verified, not re-run.
    code, events, _ = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0 and events[-1]["stages"]["checks"] == "skipped"
    assert "verification" not in events[-1]
    assert len(FakeBackend.contexts) == 2

    # A removed check is a change of the set too.
    set_checks(tmp_path, 1)
    code, events, _ = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0 and events[-1]["stages"]["checks"] == "done"
    assert events[-1]["verification"]["trigger"] == "checks-changed"
    assert len(FakeBackend.contexts) == 3
    # Nothing was built, delivered, applied or backed up along the way.
    assert [c for c in FakeBackend.calls if c != "head"] == [
        c for c in calls if c != "head"
    ]
    assert image(api) == good and releases(tmp_path) == count


def test_a_failing_changed_check_never_rolls_back_an_unchanged_release(shop) -> None:
    api, tmp_path = shop
    set_checks(tmp_path, 1)
    code, _, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    previous = image(api)
    # A second release, so a rollback would have a target.
    (tmp_path / "src" / "main.txt").write_text("v2\n")
    FakeBackend.image_id = "sha256:" + "2" * 64
    code, _, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    good, count = image(api), releases(tmp_path)
    assert good != previous and count == 2

    # rollback_on_failed_checks=True, but this run rolled nothing out.
    set_checks(tmp_path, 2)
    FakeBackend.passing[0] = False
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 1, result.stdout + result.stderr
    final = events[-1]
    assert final["reason"] == "pipeline-checks-failed"
    assert final["state"] == "failed"  # not rolled-back
    output = last_run(tmp_path)["stages"]["checks"]["output"]
    assert "rollback" not in output
    assert output["verification"] == {"trigger": "checks-changed", "applied": False}
    assert output["passed"] is False and output["checks_hash"].startswith("sha256:")
    assert "not rolled back" in result.stderr
    assert image(api) == good and releases(tmp_path) == count

    # The failed verification is not "verified": the next run checks again
    # and a passing check clears it.
    FakeBackend.passing[0] = True
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["stages"]["checks"] == "done"
    assert events[-1]["verification"]["trigger"] == "unverified"
    assert events[-1]["verification"]["passed"] is True
    assert image(api) == good and releases(tmp_path) == count


def test_new_commits_with_the_same_release_run_the_checks_again(
    shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0.88.0: a commit whose images and manifests did not change gets checks
    of its own; the same commits again stay verified."""
    from piceli.pipeline import runner as runner_module

    api, tmp_path = shop
    set_checks(tmp_path, 1)
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    good, count = image(api), releases(tmp_path)
    assert len(FakeBackend.contexts) == 1

    revision = {"app": "a" * 40}
    original = runner_module.PipelineRunner.__init__

    def told(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("revision", dict(revision))
        original(self, *args, **kwargs)

    monkeypatch.setattr(runner_module.PipelineRunner, "__init__", told)

    # Checks recorded without commits (before 0.88.0): checked once more.
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["stages"]["apply"] == "skipped"
    assert events[-1]["stages"]["checks"] == "done"
    assert events[-1]["verification"]["trigger"] == "new-revision"
    assert len(FakeBackend.contexts) == 2
    output = last_run(tmp_path)["stages"]["checks"]["output"]
    assert output["revision"] == revision and output["results"]

    # The same commits: verified already.
    code, events, _ = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0 and events[-1]["stages"]["checks"] == "skipped"
    assert len(FakeBackend.contexts) == 2

    # A new commit, nothing else changed: its own checks, nothing applied.
    revision["app"] = "b" * 40
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["stages"]["apply"] == "skipped"
    assert events[-1]["verification"]["trigger"] == "new-revision"
    assert events[-1]["verification"]["passed"] is True
    assert len(FakeBackend.contexts) == 3
    assert "verifying the running release (new-revision)" in result.stderr
    assert image(api) == good and releases(tmp_path) == count
