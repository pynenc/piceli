"""Field-level diffs, server dry-run evidence and true no-op plans (pure)."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from typing import Any

import pytest

from piceli.k8s.ops.discovery import (
    ApiResource,
    DiscoveredResource,
    DiscoveryArtifact,
    DiscoveryCoverage,
    DiscoveryLimits,
    DiscoveryProvenance,
    EvidenceSource,
    Ownership,
    PlanTarget,
    ResourceIdentity,
    ResourceScope,
    ResourceType,
    ServerDryRun,
)
from piceli.k8s.ops.dry_run import (
    MAX_DRY_RUNS,
    capture_server_dry_runs,
    update_body,
)
from piceli.k8s.ops.field_diff import (
    describe_change,
    field_changes,
    merge_patch,
    plan_diffs,
    unified_diff,
)
from piceli.k8s.ops.kubernetes_provider import ProviderError
from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    ObservedSnapshot,
    PlanAuthorization,
    PlanOperation,
    ResourceIntent,
    build_plan,
    dry_run_confirms,
)
from piceli.k8s.ops.secret_versions import SecretVersionRef

TARGET = PlanTarget("unit-cluster", "app")
DEPLOYMENT = ResourceType("apps/v1", "Deployment")
CONFIG = ResourceType("v1", "ConfigMap")
SECRET = ResourceType("v1", "Secret")


def deployment(image: str = "example.invalid/api:1", cpu: str = "0.5") -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "app"},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "api"}},
            "template": {
                "metadata": {"labels": {"app": "api"}},
                "spec": {
                    "containers": [
                        {
                            "name": "api",
                            "image": image,
                            "resources": {"requests": {"cpu": cpu}},
                        }
                    ]
                },
            },
        },
    }


def served(desired: dict, *, version: str = "7") -> dict:
    """``desired`` as the API server stores it: defaults and canonical values."""
    value = copy.deepcopy(desired)
    value["metadata"].update(
        {
            "uid": "uid-api",
            "resourceVersion": version,
            "generation": 3,
            "annotations": {
                "piceli.io/owner": "owner",
                "piceli.io/operation": "op-1",
                "deployment.kubernetes.io/revision": "2",
            },
            "managedFields": [{"manager": "piceli", "operation": "Update"}],
        }
    )
    spec = value["spec"]
    spec.update({"revisionHistoryLimit": 10, "progressDeadlineSeconds": 600})
    container = spec["template"]["spec"]["containers"][0]
    container["imagePullPolicy"] = "IfNotPresent"
    container["resources"]["requests"]["cpu"] = "500m"
    value["status"] = {"replicas": 1}
    return value


def artifact(*manifests: dict, dry_runs: tuple[ServerDryRun, ...] = ()) -> Any:
    types = (CONFIG, DEPLOYMENT, SECRET)
    return DiscoveryArtifact(
        TARGET,
        "2026-01-01T00:00:00+00:00",
        DiscoveryLimits(),
        DiscoveryCoverage(
            "unit",
            "capture-1",
            "policy-1",
            types,
            types,
            tuple(
                ApiResource(item, ResourceScope.NAMESPACED, item.kind.lower() + "s")
                for item in types
            ),
        ),
        tuple(
            DiscoveredResource.from_manifest(
                item, scope=ResourceScope.NAMESPACED, ownership=Ownership.MANAGED
            )
            for item in manifests
        ),
        provenance=DiscoveryProvenance(
            EvidenceSource.LOOPBACK, "endpoint", "cluster-uid", "namespace-uid"
        ),
        server_dry_runs=dry_runs,
    )


def composition(*intents: ResourceIntent) -> DeploymentComposition:
    return DeploymentComposition((DeploymentComponent("app", intents),))


def evidence(desired: ResourceIntent, answer: dict, version: str = "7") -> ServerDryRun:
    return ServerDryRun(
        ResourceIdentity(**desired.ref.__dict__),
        desired.digest,
        version,
        json.dumps(answer),
    )


def plan(desired: ResourceIntent, live: dict, *dry_runs: ServerDryRun):
    snapshot = ObservedSnapshot.from_discovery(artifact(live, dry_runs=dry_runs))
    return (
        build_plan(composition(desired), snapshot, PlanAuthorization(TARGET)),
        snapshot,
    )


# ------------------------------------------------------------ equivalence
def test_live_defaults_without_evidence_plan_as_apply() -> None:
    desired = ResourceIntent.from_manifest(deployment())
    result, _ = plan(desired, served(deployment()))
    assert result.actions[0].operation is PlanOperation.APPLY


def test_matching_server_dry_run_makes_an_unchanged_object_a_noop() -> None:
    desired = ResourceIntent.from_manifest(deployment())
    live = served(deployment())
    result, snapshot = plan(desired, live, evidence(desired, live))
    assert result.actions[0].operation is PlanOperation.NOOP
    assert plan_diffs(result, snapshot) == []
    # The executor accepts the no-op although "0.5" is stored as "500m".
    assert dry_run_confirms(snapshot, desired, live)
    changed = copy.deepcopy(live)
    changed["spec"]["replicas"] = 2
    assert not dry_run_confirms(snapshot, desired, changed)


def test_changed_field_is_the_only_diff() -> None:
    desired = ResourceIntent.from_manifest(deployment("example.invalid/api:2"))
    live = served(deployment())
    answer = served(deployment("example.invalid/api:2"))
    result, snapshot = plan(desired, live, evidence(desired, answer))
    assert result.actions[0].operation is PlanOperation.APPLY
    (diff,) = plan_diffs(result, snapshot)
    assert diff["basis"] == "server-dry-run"
    assert diff["changes"] == [
        {
            "path": "/spec/template/spec/containers/0/image",
            "op": "replace",
            "before": "example.invalid/api:1",
            "after": "example.invalid/api:2",
        }
    ]
    assert "-      - image: example.invalid/api:1\n" in diff["unified"]
    assert "+      - image: example.invalid/api:2\n" in diff["unified"]


def test_evidence_for_another_desired_manifest_is_ignored() -> None:
    old = ResourceIntent.from_manifest(deployment())
    desired = ResourceIntent.from_manifest(deployment("example.invalid/api:2"))
    live = served(deployment())
    result, snapshot = plan(desired, live, evidence(old, live))
    assert result.actions[0].operation is PlanOperation.APPLY
    (diff,) = plan_diffs(result, snapshot)
    assert diff["basis"] == "client"
    assert not dry_run_confirms(snapshot, desired, live)


def test_evidence_must_match_the_observed_resource_version() -> None:
    desired = ResourceIntent.from_manifest(deployment())
    live = served(deployment())
    with pytest.raises(ValueError, match="dry-run evidence does not match"):
        artifact(live, dry_runs=(evidence(desired, live, version="6"),))
    snapshot = ObservedSnapshot.from_discovery(artifact(live))
    with pytest.raises(ValueError, match="server dry runs must match"):
        replace(snapshot, server_dry_runs=(evidence(desired, live, version="6"),))


def test_secret_bound_objects_are_never_equivalent() -> None:
    reference = SecretVersionRef("0" * 32, "1" * 32)
    desired = ResourceIntent.from_manifest(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "token", "namespace": "app"},
            "data": {"token": "<private>", "user": "YWRtaW4="},
        }
    ).with_secret("/data/token", reference)
    live = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "token",
            "namespace": "app",
            "uid": "uid-token",
            "resourceVersion": "7",
            "annotations": {"piceli.io/owner": "owner"},
        },
        "data": {"token": "c2VjcmV0LXZhbHVl", "user": "YWRtaW4="},
    }
    result, snapshot = plan(desired, live, evidence(desired, live))
    assert result.actions[0].operation is PlanOperation.APPLY
    (diff,) = plan_diffs(result, snapshot)
    assert diff["not_compared"] == ["/data/token"]
    # The bound value is masked on both sides, so it is never compared.
    assert diff["changes"] == []
    encoded = json.dumps(diff)
    for value in ("c2VjcmV0LXZhbHVl", "YWRtaW4="):
        assert value not in encoded
    changes = field_changes(live, live | {"data": {"user": "cm9vdA=="}})
    assert {change["op"] for change in changes} == {"remove", "replace"}
    assert all(
        change["before"] == "<redacted>"
        for change in changes
        if change["path"].startswith("/data/")
    )
    assert "cm9vdA==" not in json.dumps(changes)


def test_evidence_is_private_bound_to_the_snapshot_and_round_trips() -> None:
    desired = ResourceIntent.from_manifest(deployment())
    live = served(deployment())
    plain = artifact(live)
    with_evidence = artifact(live, dry_runs=(evidence(desired, live),))
    # Snapshots without evidence keep the hash they had before evidence existed.
    assert ObservedSnapshot.from_discovery(plain).snapshot_hash == (
        ObservedSnapshot(
            plain.target,
            plain.coverage,
            ObservedSnapshot.from_discovery(plain).resources,
            captured_at=plain.captured_at,
            provenance=plain.provenance,
        ).snapshot_hash
    )
    assert (
        ObservedSnapshot.from_discovery(with_evidence).snapshot_hash
        != ObservedSnapshot.from_discovery(plain).snapshot_hash
    )
    assert "server_dry_runs" not in with_evidence.to_json()
    assert with_evidence.to_json() == plain.to_json()
    private = with_evidence.to_private_json()
    assert "server_dry_runs" in private
    assert "server_dry_runs" not in plain.to_private_json()
    restored = DiscoveryArtifact.from_private_json(private)
    assert restored.server_dry_runs == with_evidence.server_dry_runs
    assert ObservedSnapshot.from_discovery(restored) == ObservedSnapshot.from_discovery(
        with_evidence
    )


def test_plan_hash_is_stable_and_excludes_diffs() -> None:
    desired = ResourceIntent.from_manifest(deployment("example.invalid/api:2"))
    live = served(deployment())
    answer = served(deployment("example.invalid/api:2"))
    first, snapshot = plan(desired, live, evidence(desired, answer))
    second = build_plan(composition(desired), snapshot, PlanAuthorization(TARGET))
    assert first.plan_hash == second.plan_hash
    assert "diff" not in json.dumps(first.summary())


# ------------------------------------------------------------------ diffs
def test_field_changes_ops_paths_and_order() -> None:
    before = {"a": {"b/c": 1, "x": [1, 2, 3]}, "gone": True, "same": "s"}
    after = {"a": {"b/c": 2, "x": [1, 5]}, "new": {"k": "v"}, "same": "s"}
    assert field_changes(before, after) == [
        {"path": "/a/b~1c", "op": "replace", "before": 1, "after": 2},
        {"path": "/a/x/1", "op": "replace", "before": 2, "after": 5},
        {"path": "/a/x/2", "op": "remove", "before": 3, "after": None},
        {"path": "/gone", "op": "remove", "before": True, "after": None},
        {"path": "/new", "op": "add", "before": None, "after": {"k": "v"}},
    ]
    assert describe_change(field_changes({"n": 1}, {"n": 2})[0]) == "~ /n: 1 -> 2"


def test_sensitive_values_are_redacted_in_changes_and_unified_diff() -> None:
    def pod(value: str) -> dict:
        manifest = deployment()
        manifest["spec"]["template"]["spec"]["containers"][0]["env"] = [
            {"name": "DB_PASSWORD", "value": value}
        ]
        return manifest

    changes = field_changes(pod("old-value"), pod("new-value"))
    assert changes == [
        {
            "path": "/spec/template/spec/containers/0/env/0/value",
            "op": "replace",
            "before": "<redacted>",
            "after": "<redacted>",
        }
    ]
    text = unified_diff(pod("old-value"), pod("new-value"), label="Deployment/api")
    assert "old-value" not in text and "new-value" not in text


def test_merge_patch_follows_rfc_7386() -> None:
    assert merge_patch(
        {"a": 1, "b": {"c": 1, "d": 2}, "l": [1, 2]},
        {
            "b": {"c": None, "e": 3},
            "l": [3],
        },
    ) == {"a": 1, "b": {"d": 2, "e": 3}, "l": [3]}


# ---------------------------------------------------------------- capture
class _Provider:
    owner_id = "owner"

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[dict] = []

    def preview_update(self, current, manifest, *, deadline=None):
        self.calls.append(manifest)
        answer = self.answers[current.identity.name]
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_capture_probes_managed_comparable_objects_only() -> None:
    desired = ResourceIntent.from_manifest(deployment())
    live = served(deployment())
    secret = ResourceIntent.from_manifest(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "token", "namespace": "app"},
            "data": {"token": "dG9rZW4="},
        }
    )
    secret_live = secret.manifest | {
        "metadata": secret.manifest["metadata"]
        | {"uid": "uid-token", "resourceVersion": "3"}
    }
    provider = _Provider({"api": live})
    captured, unavailable = capture_server_dry_runs(
        provider, artifact(live, secret_live), composition(desired, secret)
    )
    assert unavailable == ()
    (run,) = captured.server_dry_runs
    assert run.resource.name == "api" and run.desired_digest == desired.digest
    # The request is the executor's same-owner write.
    (body,) = provider.calls
    assert body == update_body(desired, captured.resources[0], "owner")
    assert body["metadata"]["annotations"] == {"piceli.io/owner": "owner"}
    assert body["metadata"]["resourceVersion"] == "7"
    # Volatile bookkeeping is not stored as evidence.
    assert "managedFields" not in run.manifest["metadata"]
    assert "status" not in run.manifest


def test_capture_failures_leave_objects_without_evidence() -> None:
    desired = ResourceIntent.from_manifest(deployment())
    live = served(deployment())
    source = artifact(live)
    for answer, reason in (
        (ProviderError("rbac-denied", status=403), "rbac-denied"),
        ({"kind": "Deployment"}, "invalid-dry-run-response"),
    ):
        captured, unavailable = capture_server_dry_runs(
            _Provider({"api": answer}), source, composition(desired)
        )
        assert captured is source
        assert [item.reason for item in unavailable] == [reason]
    captured, unavailable = capture_server_dry_runs(
        _Provider({"api": live}), source, composition(desired), max_requests=0
    )
    assert [item.reason for item in unavailable] == ["dry-run-limit-exceeded"]
    assert MAX_DRY_RUNS == 256
    # A provider without dry-run support changes nothing.
    assert capture_server_dry_runs(object(), source, composition(desired)) == (
        source,
        (),
    )
