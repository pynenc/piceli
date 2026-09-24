"""Acceptance regressions: server-populated PVC binding fields, boolean *Token fields."""

from __future__ import annotations

from piceli.k8s.ops.discovery import ResourceType, public_manifest
from piceli.k8s.ops.plan import PlanOperation
from tests.acceptance.fake_api import manifest
from tests.acceptance.test_local_executor import executor, mutations, prepare


def test_first_apply_of_a_wait_for_first_consumer_claim_is_not_drift(
    local_api, tmp_path
):
    api, provider = local_api
    api.wait_for_first_consumer.add("state")
    claim = manifest("PersistentVolumeClaim", "state")
    claim["spec"] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "1Gi"}},
    }
    worker = manifest("Deployment", "worker")
    worker["spec"]["template"]["spec"]["volumes"] = [
        {"name": "state", "persistentVolumeClaim": {"claimName": "state"}}
    ]
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [claim, worker])

    result = run.run("wffc-bind", plan, snapshot, grant)

    assert result["state"] == "ready", result
    bound = api.objects[("PersistentVolumeClaim", "state")]
    # The server populated fields the plan never declared while binding.
    assert bound["spec"]["volumeName"].startswith("pvc-")
    assert bound["metadata"]["annotations"]["pv.kubernetes.io/bind-completed"] == "yes"
    assert {entry["manager"] for entry in bound["metadata"]["managedFields"]} >= {
        "kube-controller-manager",
        "kube-scheduler",
    }
    assert [row["state"] for row in run.journal.actions("wffc-bind")] == [
        "ready",
        "ready",
    ]


def test_a_foreign_edit_of_a_declared_field_is_still_drift(local_api, tmp_path):
    api, provider = local_api
    run = executor(provider, tmp_path)
    api.ready = False
    plan, snapshot, grant = prepare(
        provider,
        [manifest("Deployment", "worker")],
        kinds=(ResourceType("apps/v1", "Deployment"),),
    )
    original = api.route

    def route(request):
        status, body = original(request)
        live = api.objects.get(("Deployment", "worker"))
        if request["method"] == "GET" and live is not None and status == 200:
            live["spec"]["replicas"] = 3
        return status, body

    api.route = route  # type: ignore[method-assign]
    report = run.run("drift", plan, snapshot, grant)
    assert report["failure_category"] == "applied-resource-drift"


def test_automount_service_account_token_is_not_treated_as_a_secret(
    local_api, tmp_path
):
    api, provider = local_api
    worker = manifest("Deployment", "worker")
    pod = worker["spec"]["template"]["spec"]
    pod["automountServiceAccountToken"] = False
    pod["volumes"] = [
        {
            "name": "token",
            "projected": {
                "sources": [
                    {
                        "serviceAccountToken": {
                            "audience": "vault",
                            "expirationSeconds": 600,
                            "path": "token",
                        }
                    }
                ]
            },
        }
    ]
    public, redacted = public_manifest(worker)
    assert not redacted
    assert public["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    run = executor(provider, tmp_path)
    plan, snapshot, grant = prepare(provider, [worker])
    assert plan.actions[0].operation is PlanOperation.CREATE
    assert run.run("automount", plan, snapshot, grant)["state"] == "ready"
    [created] = mutations(api)
    assert (
        created["body"]["spec"]["template"]["spec"]["automountServiceAccountToken"]
        is False
    )


def test_real_secret_values_are_still_redacted():
    worker = manifest("Deployment", "worker")
    container = worker["spec"]["template"]["spec"]["containers"][0]
    container["env"] = [
        {"name": "API_TOKEN", "value": "plain-token"},
        {"name": "MODE", "value": "fast"},
    ]
    worker["metadata"]["annotations"] = {"example.test/token": "annotation-token"}
    worker["spec"]["template"]["spec"]["serviceAccountToken"] = "inline-token"
    public, redacted = public_manifest(worker)
    assert redacted
    env = public["spec"]["template"]["spec"]["containers"][0]["env"]
    assert env == [
        {"name": "API_TOKEN", "value": "<redacted>"},
        {"name": "MODE", "value": "fast"},
    ]
    assert public["metadata"]["annotations"]["example.test/token"] == "<redacted>"
    assert public["spec"]["template"]["spec"]["serviceAccountToken"] == "<redacted>"
    secret, redacted = public_manifest(manifest("Secret", "credentials", value="x"))
    assert redacted and secret["data"] == {"password": "<redacted>"}
