"""A failed recovery never silently approves a newly captured plan."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from piceli.k8s.release_runner import ReleaseError
from piceli.pipeline.runner import PipelineRunner


@pytest.mark.parametrize("reason", ["plan-expired", "plan-stale", "plan-blocked"])
def test_resume_refusal_cannot_replan_or_apply_another_digest(reason: str) -> None:
    approved = "a" * 64
    engine = Mock()
    engine.history.entries.return_value = []
    engine.apply.side_effect = ReleaseError("review required", code=reason)
    engine.plan.return_value = SimpleNamespace(plan_hash="b" * 64)
    runner = object.__new__(PipelineRunner)
    runner._work = SimpleNamespace(
        runner=engine,
        release_plan=SimpleNamespace(release="shop-original", plan_hash=approved),
    )
    runner.run = Mock()
    runner.run.output.return_value = {"release": "shop-original", "plan_hash": approved}
    runner._bind = Mock()
    runner._unchanged = Mock(return_value=False)
    runner.say = Mock()

    with pytest.raises(ReleaseError) as error:
        runner._run_apply(reapply=False, resuming=True)

    assert error.value.code == reason
    engine.apply.assert_called_once_with(approved)
    engine.plan.assert_not_called()
