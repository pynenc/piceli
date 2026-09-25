"""The owner's approval policy: validation, evaluation and the plan hash (M5.9)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from piceli.approval_policy import (
    ALWAYS_REVIEWED,
    ApprovalPolicy,
    ApprovalPolicyError,
)
from piceli.errors import ERRORS
from piceli.k8s.ops.discovery import PlanTarget
from piceli.k8s.ops.plan import DeploymentPlan
from piceli.k8s.release_spec import ReleaseSettings
from piceli.pipeline import PipelineError
from piceli.pipeline.runner import PipelineRunner


def _action(operation: str, name: str = "web", **extra: Any) -> dict[str, Any]:
    return {"operation": operation, "kind": "Deployment", "name": name, **extra}


def test_codes_are_registered() -> None:
    for code in (
        "approval-policy-invalid",
        "approval-policy-missing",
        "approval-policy-exceeded",
        "approve-if-policy-flags-conflict",
    ):
        assert ERRORS[code].area == "approval"


@pytest.mark.parametrize("widened", sorted(ALWAYS_REVIEWED))
def test_a_policy_can_never_allow_delete_replace_or_adopt(widened: str) -> None:
    with pytest.raises(ApprovalPolicyError) as caught:
        ApprovalPolicy(allow={"create", widened})
    assert caught.value.code == "approval-policy-invalid"
    with pytest.raises(ApprovalPolicyError):
        ApprovalPolicy.from_value({"allow": [widened]})


@pytest.mark.parametrize(
    "value",
    [
        {"allow": ["everything"]},
        {"deny": ["nothing"]},
        {"allow": "create"},
        {"max_objects": -1},
        {"max_objects": True},
        {"max_objects": 5000},
        {"widen": ["delete"]},
        "create,apply",
    ],
)
def test_invalid_declarations_are_refused(value: Any) -> None:
    with pytest.raises(ApprovalPolicyError):
        ApprovalPolicy.from_value(value)


def test_defaults_and_identity_round_trip() -> None:
    policy = ApprovalPolicy()
    assert policy.effective == {"create", "apply", "no-op"}
    assert ApprovalPolicy.from_identity(policy.identity()) == policy
    assert ApprovalPolicy.from_value(None) is None
    assert ApprovalPolicy.from_value(policy) is policy
    denied = ApprovalPolicy(allow={"create", "apply"}, deny={"apply", "delete"})
    assert denied.effective == {"create"}


def test_evaluate_refuses_every_action_outside_the_policy() -> None:
    policy = ApprovalPolicy(
        allow={"create", "apply", "no-op"},
        deny={"delete", "replace", "adopt", "cluster_scoped"},
        max_objects=3,
    )
    inside = policy.evaluate([_action("create"), _action("apply"), _action("no-op")])
    assert inside.allowed and inside.changes == 2
    outside = policy.evaluate(
        [
            _action("create", "a"),
            _action("delete", "b"),
            _action("replace", "c"),
            _action("adopt", "d"),
            {
                "operation": "create",
                "kind": "ClusterRole",
                "name": "e",
                "cluster_scoped": True,
            },
            _action("future-operation", "f"),
        ],
        drift=[{"resource": {"kind": "Deployment", "name": "g"}, "managers": ["x"]}],
    )
    assert not outside.allowed
    assert outside.violations == (
        "delete Deployment/b",
        "replace Deployment/c",
        "adopt Deployment/d",
        "cluster_scoped ClusterRole/e",
        "future-operation Deployment/f",
        "drift Deployment/g",
        "max_objects: 6 changed objects > 3",
    )
    assert outside.to_dict()["policy"] == policy.identity()


def test_cluster_scoped_and_drift_need_an_explicit_allow() -> None:
    role = {
        "operation": "create",
        "kind": "ClusterRole",
        "name": "r",
        "cluster_scoped": True,
    }
    drift = [{"kind": "Deployment", "name": "web"}]
    assert not ApprovalPolicy().evaluate([role]).allowed
    assert not ApprovalPolicy().evaluate([], drift=drift).allowed
    wider = ApprovalPolicy(allow={"create", "apply", "cluster_scoped", "drift"})
    assert wider.evaluate([role], drift=drift).allowed
    assert (
        not ApprovalPolicy(allow=wider.allow, deny={"drift"})
        .evaluate([], drift=drift)
        .allowed
    )


def test_release_settings_parse_and_validate_auto_approve() -> None:
    base = {
        "name": "app",
        "owner": "owner",
        "field_manager": "manager",
        "composition": "compose.py:build",
        "state_dir": "state",
    }
    assert ReleaseSettings(**base).approval_policy is None
    settings = ReleaseSettings(
        **base, auto_approve={"allow": ["create", "apply"], "max_objects": 4}
    )
    assert settings.approval_policy == ApprovalPolicy(
        allow={"create", "apply"}, max_objects=4
    )
    with pytest.raises(ValidationError):
        ReleaseSettings(**base, auto_approve={"allow": ["create", "delete"]})
    with pytest.raises(ValidationError):
        ReleaseSettings(**base, auto_approve={"allow": ["create"], "widen": True})


def test_the_policy_is_part_of_the_release_plan_hash() -> None:
    target = PlanTarget("cluster", "shop")

    def plan(policy: ApprovalPolicy | None) -> DeploymentPlan:
        return DeploymentPlan(
            target, "sha256:" + "0" * 64, (), (), approval_policy=policy
        )

    without = plan(None)
    assert "approval_policy" not in without.summary()  # older plans keep their hash
    strict = plan(ApprovalPolicy(max_objects=1))
    wider = plan(ApprovalPolicy(max_objects=2))
    assert len({without.plan_hash, strict.plan_hash, wider.plan_hash}) == 3
    assert strict.plan_hash == plan(ApprovalPolicy(max_objects=1)).plan_hash
    assert (
        strict.summary()["approval_policy"] == ApprovalPolicy(max_objects=1).identity()
    )


def test_a_policy_approved_run_is_checked_again_after_delivery() -> None:
    """The real release plan (with digests) must stay inside the policy too."""
    policy = ApprovalPolicy()
    result = SimpleNamespace(
        to_dict=lambda: {"actions": [_action("delete", "old")]}, drift=[]
    )

    def runner(approved_by: str | None) -> Any:
        data = {"approved_by": approved_by} if approved_by else {}
        return SimpleNamespace(
            run=SimpleNamespace(data=data),
            pipeline=SimpleNamespace(auto_approve=policy),
        )

    PipelineRunner._within_policy(runner(None), result)  # a human approved it
    with pytest.raises(PipelineError) as caught:
        PipelineRunner._within_policy(runner("policy"), result)
    assert caught.value.code == "approval-policy-exceeded"
    assert caught.value.details["policy"]["violations"] == ["delete Deployment/old"]
