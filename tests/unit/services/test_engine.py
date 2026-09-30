"""Real release execution cannot cross the durable pre-dispatch hook."""

from pathlib import Path

import pytest

from piceli.k8s.release_runner import ReleaseRunner
from piceli.k8s.release_spec import ReleaseSpec
from tests.acceptance.test_release_cli import release_env  # noqa: F401


def test_before_execution_commits_identity_before_any_write(release_env) -> None:  # noqa: F811
    api, root = release_env
    runner = ReleaseRunner(ReleaseSpec.from_toml(root / "release.toml"))
    plan = runner.plan()
    before = dict(api.objects)
    seen = []

    def refuse(entry: dict) -> None:
        seen.append(entry)
        assert entry["plan_hash"] == plan.plan_hash
        assert entry["execution_id"]
        assert api.objects == before
        raise RuntimeError("control store write failed")

    runner.before_execution = refuse
    with pytest.raises(RuntimeError, match="control store write failed"):
        runner.apply(plan.plan_hash)
    assert seen
    assert api.objects == before
    assert runner.history.entries() == []
    runner.before_execution = None
    result = runner.apply(plan.plan_hash)
    assert result["execution"]["execution_id"] == seen[0]["execution_id"]
    assert result["release_state"] == "ready"


def test_reapply_hook_receives_new_execution_before_history(release_env) -> None:  # noqa: F811
    _api, root = release_env
    runner = ReleaseRunner(ReleaseSpec.from_toml(Path(root) / "release.toml"))
    runner.apply(runner.plan().plan_hash)
    before = runner.history.entries()
    planned = runner.plan()
    assert planned.mode == "reapply"
    seen = []

    def capture(entry: dict) -> None:
        assert runner.history.entries() == before
        seen.append(entry)

    runner.before_execution = capture
    result = runner.apply(planned.plan_hash)
    assert result["execution"]["execution_id"] == seen[0]["execution_id"]
    assert result["execution"]["execution_id"] != before[-1]["execution_id"]
