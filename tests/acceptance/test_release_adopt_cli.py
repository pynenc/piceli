"""Acceptance: ``piceli release`` adoption (``--adopt`` and ``[release] adopt``)."""

# ruff: noqa: F811  (tests take the imported ``release_env`` fixture)

from __future__ import annotations

import copy
import json
from pathlib import Path

from tests.acceptance.fake_api import TARGET, manifest
from tests.acceptance.test_release_cli import (  # noqa: F401 (pytest fixture)
    DIGEST_1,
    DIGEST_2,
    _image,
    _receipt,
    _run,
    release_env,
)

MANAGER = "piceli-acceptance"


def mutations(api):
    return [
        request
        for request in api.requests
        if request["method"] in {"POST", "PATCH", "DELETE"}
        and request["query"].get("dryRun") != ["All"]
    ]


def kubectl_objects(api):
    """A worker Deployment and settings ConfigMap made by kubectl."""
    api.field_ownership = True
    worker = manifest("Deployment", "worker")
    worker["spec"]["template"]["spec"]["containers"][0]["image"] = "kubectl/api:1"
    applied = copy.deepcopy(worker)
    applied["spec"]["template"]["spec"]["containers"][0].pop("image")
    api.put(
        worker,
        managers=[
            ("kubectl-client-side-apply", "Update", applied),
            (
                "kubectl-set",
                "Update",
                {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {"name": "worker", "image": "kubectl/api:1"}
                                ]
                            }
                        }
                    }
                },
            ),
        ],
    )
    settings = manifest("ConfigMap", "settings", value="blue")
    api.put(settings, managers=[("kubectl-client-side-apply", "Update", settings)])


def forced(api):
    return [
        request
        for request in mutations(api)
        if request["query"].get("force") == ["true"]
    ]


def test_plan_names_unadopted_objects_and_refuses(release_env):
    api, tmp_path = release_env
    kubectl_objects(api)
    code, refused, _ = _run(tmp_path, "plan")
    assert code == 2
    assert "Deployment/worker" in refused["reason"]
    assert "ConfigMap/settings" in refused["reason"]
    assert "--adopt" in refused["reason"]
    assert mutations(api) == []


def test_cli_adoption_is_planned_bound_and_applied(release_env):
    api, tmp_path = release_env
    kubectl_objects(api)
    code, planned, result = _run(
        tmp_path,
        "plan",
        "--adopt",
        "Deployment/worker",
        "--adopt",
        "ConfigMap/settings",
    )
    assert code == 0, result.output
    adoptions = {
        item["name"]: item["adoption"]
        for item in planned["actions"]
        if item["operation"] == "adopt"
    }
    assert adoptions == {
        "settings": {
            "mode": "takeover",
            "previous_owner": None,
            "transferred_managers": ["kubectl-client-side-apply"],
            "removes_undeclared_fields": True,
        },
        "worker": {
            "mode": "takeover",
            "previous_owner": None,
            "transferred_managers": ["kubectl-client-side-apply", "kubectl-set"],
            "removes_undeclared_fields": True,
        },
    }
    assert (
        "adopt Deployment/worker  [takeover: transfers field managers: "
        "kubectl-client-side-apply, kubectl-set; fields they own that the release "
        "does not declare will be REMOVED; previous owner: none]"
    ) in result.stderr
    assert mutations(api) == []

    # Adoption flags are planning flags: an approval runs exactly the plan.
    code, refused, _ = _run(
        tmp_path,
        "apply",
        "--approve",
        planned["plan_hash"],
        "--adopt",
        "Deployment/worker",
    )
    assert code == 2 and "planning flags" in refused["reason"]

    code, applied, result = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, result.output
    assert applied["execution"]["state"] == "ready"
    adopted = {item["name"]: item for item in applied["adopted"]}
    assert adopted["worker"]["completed_transfer"] == [
        "kubectl-client-side-apply",
        "kubectl-set",
    ]
    assert "adopted Deployment/worker (takeover; transferred field managers" in (
        result.stderr
    )
    assert _image(api) == f"registry.example/app/api@{DIGEST_1}"
    assert set(api.managers("Deployment", "worker")) == {f"{MANAGER}/Apply"}
    # Nothing forced is ever persisted.
    assert forced(api) == []


def test_standing_adopt_list_is_reported_once_objects_are_managed(release_env):
    api, tmp_path = release_env
    kubectl_objects(api)
    spec = tmp_path / "release.toml"
    spec.write_text(
        spec.read_text().replace(
            'state_dir = "state"',
            'state_dir = "state"\nadopt = ["apps/v1/Deployment/worker", '
            '"ConfigMap/settings"]',
        )
    )
    code, applied, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, result.output
    assert {item["name"] for item in applied["adopted"]} == {"worker", "settings"}

    code, planned, result = _run(tmp_path, "plan")
    assert code == 0, result.output
    assert planned["adopt_not_needed"] == ["ConfigMap/settings", "Deployment/worker"]
    assert all(item["operation"] != "adopt" for item in planned["actions"])
    assert "adopt Deployment/worker: not needed" in result.stderr


def test_a_later_kubectl_edit_is_reported_as_drift(release_env):
    api, tmp_path = release_env
    kubectl_objects(api)
    code, _, result = _run(
        tmp_path,
        "apply",
        "--auto-approve",
        "--adopt",
        "Deployment/worker",
        "--adopt",
        "ConfigMap/settings",
    )
    assert code == 0, result.output
    code, planned, _ = _run(tmp_path, "plan")
    assert planned["drift"] == []

    live = api.objects[("Deployment", "worker")]
    live["spec"]["template"]["spec"]["containers"][0]["image"] = "hand/edit:2"
    live["metadata"]["managedFields"].append(
        {
            "manager": "kubectl-set",
            "operation": "Update",
            "apiVersion": "apps/v1",
            "fieldsType": "FieldsV1",
            "fieldsV1": {
                "f:spec": {
                    "f:template": {
                        "f:spec": {
                            "f:containers": {'k:{"name":"worker"}': {"f:image": {}}}
                        }
                    }
                }
            },
        }
    )
    code, planned, result = _run(tmp_path, "plan")
    assert code == 0
    assert planned["drift"] == [
        {
            "resource": {
                "api_version": "apps/v1",
                "kind": "Deployment",
                "namespace": TARGET.namespace,
                "name": "worker",
            },
            "managers": ["kubectl-set"],
        }
    ]
    assert "drift   Deployment/worker: desired fields also managed by kubectl-set" in (
        result.stderr
    )
    operations = {item["name"]: item["operation"] for item in planned["actions"]}
    assert operations["worker"] == "apply"


def test_rollback_after_adoption_restores_the_adopting_release(release_env):
    api, tmp_path = release_env
    kubectl_objects(api)
    code, first, result = _run(
        tmp_path,
        "apply",
        "--auto-approve",
        "--adopt",
        "Deployment/worker",
        "--adopt",
        "ConfigMap/settings",
    )
    assert code == 0, result.output
    _receipt(tmp_path / "build.receipt.json", DIGEST_2)
    code, second, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, result.output
    assert _image(api) == f"registry.example/app/api@{DIGEST_2}"
    code, rolled, result = _run(tmp_path, "rollback", "previous", "--auto-approve")
    assert code == 0, result.output
    assert rolled["selected"] == first["release"]
    assert _image(api) == f"registry.example/app/api@{DIGEST_1}"


def test_secret_with_another_value_is_refused_before_any_write(release_env):
    api, tmp_path = release_env
    kubectl_objects(api)
    api.put(manifest("Secret", "credential", value="a3ViZWN0bA=="))
    code, planned, result = _run(
        tmp_path,
        "plan",
        "--adopt",
        "Deployment/worker",
        "--adopt",
        "ConfigMap/settings",
        "--adopt",
        "Secret/credential",
    )
    assert code == 0, result.output
    secret = next(item for item in planned["actions"] if item["kind"] == "Secret")
    assert secret["adoption"]["mode"] == "metadata-only"
    code, refused, _ = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 2
    assert "contain the desired manifest" in refused["reason"]
    assert "a3ViZWN0bA" not in refused["reason"]
    assert mutations(api) == []


def test_adopt_entries_must_name_declared_resources(release_env):
    api, tmp_path = release_env
    kubectl_objects(api)
    code, refused, _ = _run(tmp_path, "plan", "--adopt", "Deployment/wroker")
    assert code == 2 and "does not name exactly one resource" in refused["reason"]
    code, refused, _ = _run(tmp_path, "plan", "--adopt", "deployment/worker")
    assert code == 2 and "Kind/name" in refused["reason"]
    assert mutations(api) == []


def test_plan_lists_every_blocking_object_with_suggested_flags(release_env):
    api, tmp_path = release_env
    kubectl_objects(api)
    api.put(manifest("Secret", "credential", value="a3ViZWN0bA=="))
    code, refused, result = _run(tmp_path, "plan")
    assert code == 2
    assert refused["code"] == "resource-requires-adoption"
    assert refused["blocking"] == [
        {
            "kind": "ConfigMap",
            "name": "settings",
            "code": "resource-requires-adoption",
            "reason": "exists and is not managed by this release's owner",
            "suggest": ["--adopt ConfigMap/settings", "--replace ConfigMap/settings"],
        },
        {
            "kind": "Deployment",
            "name": "worker",
            "code": "resource-requires-adoption",
            "reason": "exists and is not managed by this release's owner",
            "suggest": ["--adopt Deployment/worker", "--replace Deployment/worker"],
        },
        {
            "kind": "Secret",
            "name": "credential",
            "code": "resource-requires-adoption",
            "reason": "exists and is not managed by this release's owner; "
            "retained: replace is never allowed",
            "suggest": ["--adopt Secret/credential"],
        },
    ]
    assert "Secret/credential (--adopt Secret/credential)" in refused["reason"]
    assert (
        "blocking Deployment/worker: exists and is not managed by this release's "
        "owner -> --adopt Deployment/worker or --replace Deployment/worker"
    ) in result.stderr
    assert mutations(api) == []


def test_adopt_all_desired_adopts_every_unmanaged_declared_object(release_env):
    api, tmp_path = release_env
    kubectl_objects(api)
    unrelated = manifest("ConfigMap", "unrelated")
    api.put(unrelated)
    before = copy.deepcopy(api.objects[("ConfigMap", "unrelated")])
    code, planned, result = _run(tmp_path, "plan", "--adopt-all-desired")
    assert code == 0, result.output
    assert planned["authorized"] == {
        "adopt": ["ConfigMap/settings", "Deployment/worker"],
        "replace": [],
        "adopt_all_desired": True,
        "replace_not_needed": [],
    }
    operations = {item["name"]: item["operation"] for item in planned["actions"]}
    assert operations == {
        "settings": "adopt",
        "worker": "adopt",
        "credential": "create",
    }
    assert "adopt Deployment/worker  [takeover" in result.stderr

    code, refused, _ = _run(
        tmp_path, "apply", "--approve", planned["plan_hash"], "--adopt-all-desired"
    )
    assert code == 2 and "planning flags" in refused["reason"]
    assert mutations(api) == []
    code, applied, result = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, result.output
    assert {item["name"] for item in applied["adopted"]} == {"settings", "worker"}
    assert api.objects[("ConfigMap", "unrelated")] == before


def test_adopt_all_desired_keeps_retained_objects_metadata_only(release_env):
    api, tmp_path = release_env
    kubectl_objects(api)
    api.put(manifest("Secret", "credential", value="a3ViZWN0bA=="))
    code, planned, result = _run(tmp_path, "plan", "--adopt-all-desired")
    assert code == 0, result.output
    secret = next(item for item in planned["actions"] if item["kind"] == "Secret")
    assert secret["operation"] == "adopt"
    assert secret["adoption"]["mode"] == "metadata-only"
    # The generated value differs from the live one: refused before writes.
    code, refused, _ = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 2 and "contain the desired manifest" in refused["reason"]
    assert mutations(api) == []


def test_cli_replace_writes_a_backup_then_recreates(release_env):
    api, tmp_path = release_env
    kubectl_objects(api)
    old = copy.deepcopy(api.objects[("Deployment", "worker")])
    code, planned, result = _run(
        tmp_path,
        "plan",
        "--replace",
        "Deployment/worker",
        "--adopt",
        "ConfigMap/settings",
    )
    assert code == 0, result.output
    worker = next(item for item in planned["actions"] if item["name"] == "worker")
    assert worker["operation"] == "replace"
    assert worker["replace"]["deletes_uid"] == old["metadata"]["uid"]
    assert planned["authorized"]["replace"] == ["Deployment/worker"]
    assert (
        f"replace Deployment/worker  [DELETES uid {old['metadata']['uid']} and "
        "recreates it from the release; backup written first; dependents: deleted]"
    ) in result.stderr
    assert mutations(api) == []

    code, applied, result = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, result.output
    [replaced] = [item for item in applied["adopted"] if item["mode"] == "replace"]
    backup = tmp_path / "state" / "backups"
    assert replaced["backup"].startswith(str(backup))
    saved = json.loads(Path(replaced["backup"]).read_text())
    assert saved["spec"] == old["spec"]
    assert "uid" not in saved["metadata"]
    assert "kubectl create -f " + replaced["backup"] in result.stderr
    live = api.objects[("Deployment", "worker")]
    assert live["metadata"]["uid"] != old["metadata"]["uid"]
    assert _image(api) == f"registry.example/app/api@{DIGEST_1}"
    assert set(api.managers("Deployment", "worker")) == {f"{MANAGER}/Update"}

    # Once managed, a replace is refused (nothing is deleted).
    code, refused, _ = _run(tmp_path, "plan", "--replace", "Deployment/worker")
    assert code == 2 and refused["code"] == "replace-refused"
    assert "already managed" in refused["reason"]


def test_cli_replace_refusals_happen_before_any_write(release_env):
    api, tmp_path = release_env
    kubectl_objects(api)
    api.put(manifest("Secret", "credential", value="a3ViZWN0bA=="))
    code, refused, _ = _run(
        tmp_path,
        "plan",
        "--replace",
        "Secret/credential",
        "--adopt-all-desired",
    )
    assert code == 2
    assert refused["blocking"] == [
        {
            "kind": "Secret",
            "name": "credential",
            "code": "replace-refused",
            "reason": "retained objects are never deleted; adopt it instead",
            "suggest": ["--adopt Secret/credential"],
        }
    ]
    code, refused, _ = _run(
        tmp_path,
        "plan",
        "--replace",
        "Deployment/worker",
        "--adopt",
        "Deployment/worker",
    )
    assert code == 2 and refused["code"] == "adopt-and-replace"
    code, refused, _ = _run(tmp_path, "plan", "--replace", "Deployment/wroker")
    assert code == 2 and refused["code"] == "replace-entry-not-declared"
    code, refused, _ = _run(tmp_path, "plan", "--replace", "deployment/worker")
    assert code == 2 and "replace entry must be" in refused["reason"]
    assert mutations(api) == []


def test_spec_replace_list_and_absent_objects(release_env):
    api, tmp_path = release_env
    spec = tmp_path / "release.toml"
    spec.write_text(
        spec.read_text().replace(
            'state_dir = "state"',
            'state_dir = "state"\nreplace = ["Deployment/worker"]',
        )
    )
    code, planned, result = _run(tmp_path, "plan")
    assert code == 0, result.output
    assert planned["authorized"]["replace_not_needed"] == ["Deployment/worker"]
    assert "replace Deployment/worker: not needed (absent)" in result.stderr
    assert {item["operation"] for item in planned["actions"]} == {"create"}
