"""Public evidence preserves engine order without exposing private values."""

import copy
import json

from piceli.services.plan_evidence import desired_resources, plan_steps


def ref(kind: str, name: str) -> dict[str, str]:
    return {"api_version": "v1", "kind": kind, "namespace": "shop", "name": name}


def fixture_plan() -> dict:
    config, worker, old = (
        ref("ConfigMap", "settings"),
        ref("Pod", "api"),
        ref("Pod", "old"),
    )
    return {
        "levels": [[config], [worker]],
        "actions": [
            {
                "operation": "create",
                "resource": config,
                "dependencies": [],
                "manifest": {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "settings"},
                    "data": {"mode": "ready", "password": "never-public"},
                },
            },
            {
                "operation": "apply",
                "resource": worker,
                "dependencies": [config],
                "manifest": {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {"name": "api"},
                    "spec": {"token": "never-public", "bound": "<private>"},
                },
            },
            {
                "operation": "delete",
                "resource": old,
                "dependencies": [],
                "manifest": {"data": {"old": "not-a-desired-resource"}},
            },
        ],
    }


def test_steps_keep_exact_action_ordinals_dependencies_and_engine_levels() -> None:
    source = fixture_plan()
    before = copy.deepcopy(source)
    steps = plan_steps(source, "target")
    assert [
        (step.ordinal, step.level, step.operation, step.resource.name) for step in steps
    ] == [
        (0, 0, "create", "settings"),
        (1, 1, "apply", "api"),
        (2, None, "delete", "old"),
    ]
    assert steps[1].dependencies == [steps[0].resource]
    assert all(step.resource.target_id == "target" for step in steps)
    assert source == before
    assert "manifest" not in steps[0].model_dump()


def test_desired_manifest_snapshot_masks_private_fields_and_excludes_deletions() -> (
    None
):
    resources, complete = desired_resources(fixture_plan(), "target")
    assert complete
    assert [item.resource.name for item in resources] == ["settings", "api"]
    assert resources[0].manifest["data"]["mode"] == "ready"
    assert resources[0].not_compared == ["/data/password"]
    assert resources[1].not_compared == ["/spec/bound", "/spec/token"]
    assert "never-public" not in json.dumps([item.model_dump() for item in resources])


def test_incomplete_or_bounded_snapshots_never_claim_complete_desired_state() -> None:
    resources, complete = desired_resources(fixture_plan(), "target", max_resources=1)
    assert len(resources) == 1 and not complete
    resources, complete = desired_resources(fixture_plan(), "target", max_bytes=1)
    assert resources == [] and not complete
    plan = fixture_plan()
    del plan["actions"][0]["manifest"]
    resources, complete = desired_resources(plan, "target")
    assert [item.resource.name for item in resources] == ["api"] and not complete


def test_noop_desired_resources_remain_in_complete_snapshot_and_masks_escape_pointers() -> (
    None
):
    plan = fixture_plan()
    plan["actions"][0]["operation"] = "no-op"
    plan["actions"][0]["manifest"]["data"] = {"nested/key~name": "<private>"}
    resources, complete = desired_resources(plan, "target")
    assert complete
    assert resources[0].resource.name == "settings"
    assert resources[0].not_compared == ["/data/nested~1key~0name"]
    assert plan_steps(plan, "target")[0].operation == "no-op"
