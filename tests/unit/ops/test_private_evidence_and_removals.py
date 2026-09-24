"""Private secret evidence (no-op for unchanged secrets) and three-way removal."""

from __future__ import annotations

import base64
import copy
import json
from dataclasses import replace

import pytest

from piceli.k8s.ops.discovery import (
    DiscoveredResource,
    Ownership,
    ResourceScope,
)
from piceli.k8s.ops.field_diff import plan_diffs
from piceli.k8s.ops.plan import (
    ObservedSnapshot,
    PlanAction,
    PlanAuthorization,
    PlanOperation,
    PrivateEvidence,
    ResourceIntent,
    ResourcePrecondition,
    build_plan,
    declared_union,
    private_comparable,
    private_evidence,
    removal_patch,
    without_removed_fields,
)
from piceli.k8s.ops.secret_versions import SecretVersionRef
from tests.unit.ops.test_field_diff import (
    TARGET,
    artifact,
    composition,
    deployment,
    evidence,
    served,
)

VALUE = base64.b64encode(b"unit-secret-do-not-print").decode()
REF = SecretVersionRef("a" * 32, "b" * 32)
MANAGER = "piceli-unit"


def live_secret(value: str = VALUE, **extra: object) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "token",
            "namespace": "app",
            "uid": "uid-token",
            "resourceVersion": "3",
            "annotations": {"piceli.io/owner": "owner"},
        },
        "type": "Opaque",
        "data": {"token": value},
        **extra,
    }


def desired_secret(**body: object) -> ResourceIntent:
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "token", "namespace": "app"},
        "data": {"token": "<private>"},
    }
    manifest.update(body)
    return ResourceIntent.from_manifest(manifest).with_secret("/data/token", REF)


def resolve(stored: dict[SecretVersionRef, object]):
    return lambda reference: stored[reference]


def snapshot_of(*manifests: dict) -> ObservedSnapshot:
    return ObservedSnapshot.from_discovery(artifact(*manifests))


def plan_for(desired, snapshot, **authorization):
    return build_plan(
        composition(*desired),
        snapshot,
        PlanAuthorization(TARGET, field_manager=MANAGER, **authorization),
        private=private_evidence(
            composition(*desired), snapshot, resolve({REF: VALUE})
        ),
    )


# --------------------------------------------------------- private evidence


def test_unchanged_secret_plans_noop_only_with_private_evidence():
    desired = desired_secret()
    snapshot = snapshot_of(live_secret())
    private = private_evidence(composition(desired), snapshot, resolve({REF: VALUE}))
    assert private.matching == {desired.ref}
    assert VALUE not in repr(private)

    with_evidence = build_plan(
        composition(desired), snapshot, PlanAuthorization(TARGET), private=private
    )
    without = build_plan(composition(desired), snapshot, PlanAuthorization(TARGET))
    assert with_evidence.actions[0].operation is PlanOperation.NOOP
    assert without.actions[0].operation is PlanOperation.APPLY
    # Nothing secret-derived reaches the public plan or its diffs.
    public = json.dumps(with_evidence.summary()) + json.dumps(
        plan_diffs(with_evidence, snapshot)
    )
    assert VALUE not in public and "unit-secret" not in public


def test_private_evidence_is_deterministic_although_keys_are_random():
    desired = desired_secret()
    snapshot = snapshot_of(live_secret())
    hashes = {
        build_plan(
            composition(desired),
            snapshot,
            PlanAuthorization(TARGET),
            private=private_evidence(
                composition(desired), snapshot, resolve({REF: VALUE})
            ),
        ).plan_hash
        for _ in range(3)
    }
    assert len(hashes) == 1


@pytest.mark.parametrize(
    "stored",
    [
        {REF: base64.b64encode(b"rotated").decode()},  # different value
        {},  # version missing: never guessed equal
    ],
)
def test_changed_or_unresolvable_secret_plans_apply(stored):
    desired = desired_secret()
    snapshot = snapshot_of(live_secret())
    private = private_evidence(composition(desired), snapshot, resolve(stored))
    assert private.matching == frozenset()
    plan = build_plan(
        composition(desired), snapshot, PlanAuthorization(TARGET), private=private
    )
    assert plan.actions[0].operation is PlanOperation.APPLY


def test_secret_server_forms_are_compared_as_stored():
    # stringData is stored base64-encoded in data; type defaults to Opaque.
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "token", "namespace": "app"},
        "stringData": {"token": "<private>"},
    }
    desired = ResourceIntent.from_manifest(manifest).with_secret(
        "/stringData/token", REF
    )
    snapshot = snapshot_of(live_secret())
    private = private_evidence(
        composition(desired), snapshot, resolve({REF: "unit-secret-do-not-print"})
    )
    assert private.matching == {desired.ref}
    assert private_comparable(
        {"apiVersion": "v1", "kind": "Secret", "stringData": {"k": "v"}}
    ) == {"apiVersion": "v1", "kind": "Secret", "data": {"k": "dg=="}}
    # A declared type other than the live one is a change.
    typed = desired_secret(type="kubernetes.io/basic-auth")
    assert not private_evidence(
        composition(typed), snapshot, resolve({REF: VALUE})
    ).matching


def test_extra_live_keys_or_labels_are_a_difference():
    desired = desired_secret()
    live = live_secret()
    live["data"]["other"] = VALUE
    snapshot = snapshot_of(live)
    assert not private_evidence(
        composition(desired), snapshot, resolve({REF: VALUE})
    ).matching


def test_unmanaged_objects_are_never_compared_privately():
    desired = desired_secret()
    found = artifact(live_secret())
    unmanaged = replace(
        found,
        resources=tuple(
            replace(item, ownership=Ownership.UNMANAGED) for item in found.resources
        ),
    )
    snapshot = ObservedSnapshot.from_discovery(unmanaged)
    assert (
        private_evidence(composition(desired), snapshot, resolve({REF: VALUE}))
        == PrivateEvidence()
    )


def test_secret_bound_non_secret_object_is_compared_literally():
    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "settings", "namespace": "app"},
        "data": {"url": "<private>", "mode": "blue"},
    }
    desired = ResourceIntent.from_manifest(manifest).with_secret("/data/url", REF)
    live = copy.deepcopy(manifest)
    live["data"]["url"] = "postgres://example.invalid/db"
    live["metadata"].update({"uid": "uid-settings", "resourceVersion": "4"})
    snapshot = snapshot_of(live)
    matched = private_evidence(
        composition(desired),
        snapshot,
        resolve({REF: "postgres://example.invalid/db"}),
    )
    assert matched.matching == {desired.ref}


# ------------------------------------------------------ three-way removals


def config(data: dict, labels: dict | None = None, **metadata: object) -> dict:
    meta: dict = {"name": "settings", "namespace": "app", **metadata}
    if labels is not None:
        meta["labels"] = labels
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta, "data": data}


def live_config(data: dict, labels: dict, managed_fields: list | None = None) -> dict:
    return config(
        data,
        labels,
        uid="uid-settings",
        resourceVersion="5",
        annotations={"piceli.io/owner": "owner"},
        managedFields=managed_fields
        or [
            {
                "manager": MANAGER,
                "operation": "Update",
                "fieldsV1": {
                    "f:data": {f"f:{key}": {} for key in data},
                    "f:metadata": {"f:labels": {f"f:{key}": {} for key in labels}},
                },
            }
        ],
    )


PREVIOUS = ResourceIntent.from_manifest(
    config({"a": "1", "b": "2"}, {"app": "web", "tier": "front"})
)
DESIRED = ResourceIntent.from_manifest(config({"a": "1"}, {"app": "web"}))


def test_keys_dropped_since_an_earlier_release_are_removed():
    live = live_config({"a": "1", "b": "2", "c": "3"}, {"app": "web", "tier": "front"})
    snapshot = snapshot_of(live)
    plan = plan_for([DESIRED], snapshot, previous=(PREVIOUS,))
    (action,) = plan.actions
    assert action.operation is PlanOperation.APPLY
    # ``c`` was never declared by a release: it is left alone.
    assert action.removals == ("/data/b", "/metadata/labels/tier")
    assert action.summary()["removes"] == ["/data/b", "/metadata/labels/tier"]
    without = plan_for([DESIRED], snapshot)
    assert without.actions[0].removals == ()
    assert plan.plan_hash != without.plan_hash

    (diff,) = plan_diffs(plan, snapshot)
    assert diff["basis"] == "client"
    assert [(change["path"], change["op"]) for change in diff["changes"]] == [
        ("/data/b", "remove"),
        ("/metadata/labels/tier", "remove"),
    ]


def test_a_removal_turns_a_server_confirmed_noop_into_apply():
    live = live_config({"a": "1", "b": "2"}, {"app": "web"})
    previous = ResourceIntent.from_manifest(
        config({"a": "1", "b": "2"}, {"app": "web"})
    )
    answer = copy.deepcopy(live)
    answer["metadata"].pop("managedFields")
    snapshot = ObservedSnapshot.from_discovery(
        artifact(live, dry_runs=(evidence(DESIRED, answer, "5"),))
    )
    assert plan_for([DESIRED], snapshot).actions[0].operation is PlanOperation.NOOP
    plan = plan_for([DESIRED], snapshot, previous=(previous,))
    assert plan.actions[0].operation is PlanOperation.APPLY
    (diff,) = plan_diffs(plan, snapshot)
    assert diff["basis"] == "server-dry-run"
    assert diff["changes"] == [
        {"path": "/data/b", "op": "remove", "before": "2", "after": None}
    ]


def test_fields_owned_by_another_manager_are_never_removed():
    managed = [
        {
            "manager": MANAGER,
            "operation": "Update",
            "fieldsV1": {"f:data": {"f:a": {}}},
        },
        # ``kubectl edit`` changed ``b`` after the release wrote it.
        {
            "manager": "kubectl-edit",
            "operation": "Update",
            "fieldsV1": {"f:data": {"f:b": {}}},
        },
        {
            "manager": "kube-controller-manager",
            "operation": "Update",
            "subresource": "status",
            "fieldsV1": {"f:metadata": {"f:labels": {"f:tier": {}}}},
        },
    ]
    live = live_config({"a": "1", "b": "9"}, {"app": "web", "tier": "front"}, managed)
    plan = plan_for([DESIRED], snapshot_of(live), previous=(PREVIOUS,))
    # The status-subresource entry does not protect the label.
    assert plan.actions[0].removals == ("/metadata/labels/tier",)


def test_a_dropped_map_is_removed_key_by_key():
    previous = ResourceIntent.from_manifest(config({"a": "1"}, {"app": "web"}))
    desired = ResourceIntent.from_manifest(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "settings", "namespace": "app"},
        }
    )
    live = live_config({"a": "1", "other": "x"}, {"app": "web"})
    plan = plan_for([desired], snapshot_of(live), previous=(previous,))
    assert plan.actions[0].removals == ("/data/a", "/metadata/labels/app")
    assert removal_patch(desired.manifest, plan.actions[0].removals) == {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "settings",
            "namespace": "app",
            "labels": {"app": None},
        },
        "data": {"a": None},
    }


def test_lists_are_replaced_by_the_merge_patch_not_removed_by_key():
    before = deployment()
    container = before["spec"]["template"]["spec"]["containers"][0]
    container["env"] = [{"name": "A", "value": "1"}, {"name": "B", "value": "2"}]
    after = copy.deepcopy(before)
    after["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "A", "value": "1"}
    ]
    live = served(before)
    live["metadata"]["managedFields"] = [
        {"manager": MANAGER, "operation": "Update", "fieldsV1": {"f:spec": {}}}
    ]
    desired = ResourceIntent.from_manifest(after)
    snapshot = snapshot_of(live)
    plan = plan_for(
        [desired], snapshot, previous=(ResourceIntent.from_manifest(before),)
    )
    (action,) = plan.actions
    assert action.operation is PlanOperation.APPLY and action.removals == ()
    (diff,) = plan_diffs(plan, snapshot)
    assert {
        "path": "/spec/template/spec/containers/0/env/1",
        "op": "remove",
        "before": {"name": "B", "value": "2"},
        "after": None,
    } in diff["changes"]


def test_retained_objects_and_first_releases_get_no_removals():
    previous = ResourceIntent.from_manifest(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "token", "namespace": "app", "labels": {"x": "1"}},
            "data": {"token": "<private>", "old": "<private>"},
        }
    )
    live = live_secret(VALUE)
    live["data"]["old"] = VALUE
    live["metadata"]["labels"] = {"x": "1"}
    plan = plan_for([desired_secret()], snapshot_of(live), previous=(previous,))
    assert plan.actions[0].removals == ()


def test_removal_pointers_are_validated():
    for pointer in ("/metadata/name", "/apiVersion", "/status/x", "data/a"):
        with pytest.raises(ValueError):
            PlanAction(
                PlanOperation.APPLY,
                DESIRED,
                (),
                ResourcePrecondition("uid", "1"),
                removals=(pointer,),
            )
    with pytest.raises(ValueError, match="only full apply"):
        PlanAction(
            PlanOperation.NOOP,
            DESIRED,
            (),
            ResourcePrecondition("uid", "1"),
            removals=("/data/b",),
        )
    with pytest.raises(ValueError, match="declared field"):
        removal_patch(DESIRED.manifest, ("/data/a",))
    assert without_removed_fields({"data": {"a": 1, "b": 2}}, ("/data/b",)) == {
        "data": {"a": 1}
    }


def test_declared_union_merges_every_earlier_declaration():
    first = ResourceIntent.from_manifest(config({"a": "1"}, {"app": "web"}))
    second = ResourceIntent.from_manifest(config({"b": "2"}, {"tier": "front"}))
    (merged,) = declared_union([first, second])
    assert merged.manifest["data"] == {"a": "1", "b": "2"}
    assert merged.manifest["metadata"]["labels"] == {"app": "web", "tier": "front"}
    with pytest.raises(ValueError, match="unique"):
        PlanAuthorization(TARGET, previous=(first, second))


def test_discovered_secret_content_stays_private():
    # Guard for the fixture: the live Secret is complete private evidence.
    resource = DiscoveredResource.from_manifest(
        live_secret(), scope=ResourceScope.NAMESPACED, ownership=Ownership.MANAGED
    )
    assert VALUE not in json.dumps(resource.public_dict())
