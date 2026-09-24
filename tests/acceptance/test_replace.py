"""Acceptance: ``replace`` (backup, guarded delete, create from the release)."""

# ruff: noqa: F811  (tests take the imported ``api_url``/``provider`` fixtures)

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import replace

import pytest

from piceli.k8s.ops.plan import (
    PlanAction,
    PlanAuthorization,
    PlanOperation,
    ResourceIntent,
)
from piceli.k8s.ops.replace_backup import restorable_manifest
from tests.acceptance.fake_api import TARGET, manifest
from tests.acceptance.test_adoption import (  # noqa: F401 (pytest fixtures)
    MANAGER,
    api_url,
    desired_deployment,
    executor,
    forced,
    grant,
    kubectl_deployment,
    live_pvc,
    mutations,
    payloads,
    prepare,
    provider,
    pvc,
)


def prepare_replace(provider, desired, replace_refs=None):
    refs = replace_refs if replace_refs is not None else [d.ref for d in desired]
    return prepare(provider, desired, replace_refs=tuple(refs))


def test_replace_backs_up_deletes_with_preconditions_and_recreates(
    api_url, provider, tmp_path
):
    api, _ = api_url
    before = kubectl_deployment(api)
    intent = desired_deployment()
    with pytest.raises(ValueError, match="explicit adoption"):
        prepare(provider, [intent])
    plan, snapshot, authorization = prepare_replace(provider, [intent])
    [action] = plan.actions
    assert action.operation is PlanOperation.REPLACE
    assert action.summary()["replace"] == {
        "deletes_uid": before["metadata"]["uid"],
        "propagation": "Background",
        "backup": "written to the state directory before the delete",
    }
    backups = tmp_path / "state" / "backups"
    run = executor(provider, tmp_path, backups=backups)
    assert run.run("swap", plan, snapshot, authorization)["state"] == "ready"

    delete, create = mutations(api)
    assert delete["method"] == "DELETE"
    assert delete["body"]["preconditions"] == {
        "uid": before["metadata"]["uid"],
        "resourceVersion": before["metadata"]["resourceVersion"],
    }
    assert delete["body"]["propagationPolicy"] == "Background"
    assert create["method"] == "POST"
    assert forced(api) == []
    dry = [
        r
        for r in api.requests
        if r["method"] == "DELETE" and r["query"].get("dryRun") == ["All"]
    ]
    assert len(dry) == 1
    # The API server ignores the query string when a DeleteOptions body is
    # sent: the dry run must be in the body.
    assert dry[0]["body"]["dryRun"] == ["All"]

    after = api.objects[("Deployment", "worker")]
    assert after["metadata"]["uid"] != before["metadata"]["uid"]
    assert after["metadata"]["annotations"]["piceli.io/owner"] == provider.owner_id
    assert after["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "example.invalid/worker:new"
    )
    assert [e["manager"] for e in after["metadata"]["managedFields"]] == [MANAGER]

    [detail] = [item["replace"] for item in payloads(run, "swap")]
    path = backups / "swap" / "0000-Deployment-worker.json"
    assert detail["backup"] == str(path)
    assert detail["deleted_uid"] == before["metadata"]["uid"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(backups.stat().st_mode) == 0o700
    assert hashlib.sha256(path.read_bytes()).hexdigest() == detail["backup_sha256"]
    saved = json.loads(path.read_text())
    assert saved == restorable_manifest(before)
    assert "status" not in saved
    assert not {
        "uid",
        "resourceVersion",
        "managedFields",
        "generation",
        "creationTimestamp",
    } & set(saved["metadata"])
    assert saved["spec"] == before["spec"]
    assert saved["metadata"]["annotations"] == before["metadata"]["annotations"]

    # The backup restores the previous object (``kubectl delete`` then
    # ``kubectl create -f``): a plain create of the file content.
    current = provider.get(snapshot.discovery.resources[0].identity)
    provider.delete(
        current.identity,
        uid=current.manifest["metadata"]["uid"],
        resource_version=current.manifest["metadata"]["resourceVersion"],
    )
    restored = provider.write(current.identity, saved, create=True)
    assert restored.manifest["spec"] == before["spec"]


def test_replace_is_refused_for_retained_managed_owned_or_absent_objects(
    api_url, provider
):
    api, _ = api_url
    live_pvc(api)
    claim = ResourceIntent.from_manifest(pvc())
    with pytest.raises(ValueError, match="retained objects are never deleted"):
        prepare_replace(provider, [claim])

    api.put(manifest("ConfigMap", "settings"), owned=True)
    owned = ResourceIntent.from_manifest(manifest("ConfigMap", "settings", value="x"))
    with pytest.raises(ValueError, match="already managed"):
        prepare_replace(provider, [owned])

    child = manifest("ConfigMap", "child")
    child["metadata"]["ownerReferences"] = [
        {"apiVersion": "v1", "kind": "ConfigMap", "name": "parent", "uid": "p-1"}
    ]
    api.put(child)
    with pytest.raises(ValueError, match="owned by another object"):
        prepare_replace(
            provider, [ResourceIntent.from_manifest(manifest("ConfigMap", "child"))]
        )

    absent = ResourceIntent.from_manifest(manifest("ConfigMap", "absent"))
    with pytest.raises(ValueError, match="existing desired resource"):
        prepare_replace(provider, [absent])

    with pytest.raises(ValueError, match="both adopted and replaced"):
        PlanAuthorization(TARGET, (claim.ref,), replace_resources=(claim.ref,))
    assert mutations(api) == []


def test_replace_without_a_backup_directory_is_refused_before_any_write(
    api_url, provider, tmp_path
):
    api, _ = api_url
    kubectl_deployment(api)
    plan, snapshot, authorization = prepare_replace(provider, [desired_deployment()])
    with pytest.raises(ValueError, match="backup directory"):
        executor(provider, tmp_path).run("nobackup", plan, snapshot, authorization)
    assert mutations(api) == []


def test_unauthorized_or_forged_replace_is_refused_before_any_write(
    api_url, provider, tmp_path
):
    api, _ = api_url
    kubectl_deployment(api)
    live_pvc(api)
    intent = desired_deployment()
    plan, snapshot, authorization = prepare_replace(provider, [intent])
    run = executor(provider, tmp_path, backups=tmp_path / "backups")
    # A grant for another operation does not authorize the replace.
    other = replace(
        authorization,
        actions=(replace(authorization.actions[0], operation=PlanOperation.APPLY),),
    )
    with pytest.raises(ValueError, match="action/private-version scope"):
        run.run("no-grant", plan, snapshot, other)
    # A forged REPLACE of a retained claim is refused by the executor too.
    adopt_plan, adopt_snapshot, _ = prepare(
        provider,
        [ResourceIntent.from_manifest(pvc())],
        adopt=(ResourceIntent.from_manifest(pvc()).ref,),
    )
    forged_action = PlanAction(
        PlanOperation.REPLACE,
        adopt_plan.actions[0].resource,
        (),
        adopt_plan.actions[0].precondition,
    )
    forged = replace(adopt_plan, actions=(forged_action,))
    with pytest.raises(ValueError, match="replace refused"):
        run.run(
            "forged", forged, adopt_snapshot, grant(provider, forged, adopt_snapshot)
        )
    assert mutations(api) == []
    with pytest.raises(Exception, match="retained-resource"):
        provider.delete(
            adopt_snapshot.discovery.resources[0].identity,
            uid="x",
            resource_version="1",
        )


def test_object_changed_after_planning_is_not_deleted(api_url, provider, tmp_path):
    api, _ = api_url
    kubectl_deployment(api)
    plan, snapshot, authorization = prepare_replace(provider, [desired_deployment()])
    live = api.objects[("Deployment", "worker")]
    live["spec"]["replicas"] = 3
    api.version += 1
    live["metadata"]["resourceVersion"] = str(api.version)
    backups = tmp_path / "backups"
    report = executor(provider, tmp_path, backups=backups).run(
        "late", plan, snapshot, authorization
    )
    assert report["state"] == "failed"
    assert report["failure_category"] == "resource-content-precondition-failed"
    assert mutations(api) == []
    assert not backups.exists()


def test_create_failure_after_the_delete_blocks_and_resume_recreates(
    api_url, provider, tmp_path
):
    api, _ = api_url
    kubectl_deployment(api)
    plan, snapshot, authorization = prepare_replace(provider, [desired_deployment()])
    api.inject("POST", "/deployments", status=500, dry_run=False)
    backups = tmp_path / "backups"
    run = executor(provider, tmp_path, backups=backups)
    first = run.run("cut", plan, snapshot, authorization)
    assert first["state"] == "blocked"
    [row] = run.journal.actions("cut")
    assert row["state"] == "intent"
    assert row["payload"]["replace"]["phase"] == "deleted"
    assert ("Deployment", "worker") not in api.objects
    assert (backups / "cut" / "0000-Deployment-worker.json").exists()
    second = run.run("cut", plan, snapshot, authorization, resume=True)
    assert second["state"] == "ready"
    assert api.objects[("Deployment", "worker")]["metadata"]["annotations"][
        "piceli.io/owner"
    ] == (provider.owner_id)
    assert [r["method"] for r in mutations(api)] == ["DELETE", "POST", "POST"]


def test_lost_create_response_is_reconciled_on_resume(api_url, provider, tmp_path):
    api, _ = api_url
    kubectl_deployment(api)
    plan, snapshot, authorization = prepare_replace(provider, [desired_deployment()])
    api.inject("POST", "/deployments", disconnect_after=True, dry_run=False)
    run = executor(provider, tmp_path, backups=tmp_path / "backups")
    assert run.run("lost", plan, snapshot, authorization)["state"] == "blocked"
    created = api.objects[("Deployment", "worker")]["metadata"]["uid"]
    assert run.run("lost", plan, snapshot, authorization, resume=True)["state"] == (
        "ready"
    )
    assert api.objects[("Deployment", "worker")]["metadata"]["uid"] == created
    assert [r["method"] for r in mutations(api)] == ["DELETE", "POST"]


def test_object_recreated_by_someone_else_blocks_the_resume(
    api_url, provider, tmp_path
):
    api, _ = api_url
    kubectl_deployment(api)
    plan, snapshot, authorization = prepare_replace(provider, [desired_deployment()])
    api.inject("POST", "/deployments", status=500, dry_run=False)
    run = executor(provider, tmp_path, backups=tmp_path / "backups")
    assert run.run("race", plan, snapshot, authorization)["state"] == "blocked"
    api.put(manifest("Deployment", "worker"))
    report = run.run("race", plan, snapshot, authorization, resume=True)
    assert report["state"] == "blocked"
    assert report["failure_category"] == "replace-recreated-by-another-writer"


def test_compensation_never_touches_a_replaced_object(api_url, provider, tmp_path):
    api, _ = api_url
    kubectl_deployment(api)
    plan, snapshot, authorization = prepare_replace(provider, [desired_deployment()])
    run = executor(provider, tmp_path, backups=tmp_path / "backups")
    assert run.run("keep", plan, snapshot, authorization)["state"] == "ready"
    writes = len(mutations(api))
    run.compensate("keep", plan, snapshot, authorization)
    assert len(mutations(api)) == writes


def test_replace_of_a_non_workload_orphans_dependents(api_url, provider, tmp_path):
    api, _ = api_url
    api.put(manifest("ConfigMap", "settings"))
    intent = ResourceIntent.from_manifest(manifest("ConfigMap", "settings", value="2"))
    plan, snapshot, authorization = prepare_replace(provider, [intent])
    assert plan.actions[0].summary()["replace"]["propagation"] == "Orphan"
    run = executor(provider, tmp_path, backups=tmp_path / "backups")
    assert run.run("cm", plan, snapshot, authorization)["state"] == "ready"
    assert mutations(api)[0]["body"]["propagationPolicy"] == "Orphan"
    assert api.objects[("ConfigMap", "settings")]["data"] == {"mode": "2"}
