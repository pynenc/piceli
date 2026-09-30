"""Acceptance: ``piceli release orphans`` reports and prunes leftover objects.

Runs the real CLI against the fake API. v1 of the app has components A and B;
v2 drops B; B's objects stay in the namespace and are the leftovers.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from tests.acceptance.fake_api import TARGET, manifest, serve
from tests.acceptance.test_release_cli import _receipt

LABELS = {"app.kubernetes.io/part-of": "shop"}
DIGEST = "sha256:" + "1" * 64

COMPOSITION = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent

LABELS = {"app.kubernetes.io/part-of": "shop"}


def build(ctx):
    def meta(name):
        return {"name": name, "namespace": ctx.namespace, "labels": LABELS}

    def config(name):
        return ResourceIntent.from_manifest(
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta(name),
             "data": {"k": "v"}}
        )

    def claim(name):
        return ResourceIntent.from_manifest(
            {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": meta(name),
             "spec": {"accessModes": ["ReadWriteOnce"],
                      "resources": {"requests": {"storage": "1Gi"}}}}
        )

    components = [DeploymentComponent("a", (config("a-config"),))]
    if "b" in ctx.values["parts"]:
        components.append(
            DeploymentComponent("b", (config("b-config"), claim("b-data")))
        )
    return DeploymentComposition(tuple(components))
"""

TOML = """
images_from = "build.receipt.json"

[target]
kubeconfig = "kubeconfig"
context = "fake"
namespace = "{namespace}"
cluster_uid = "cluster-uid"
transport = "loopback-http"

[release]
name = "shop"
owner = "acceptance-owner"
field_manager = "piceli-acceptance"
composition = "compose.py:build"
state_dir = "state"
{extra}
[execution]
max_seconds = 30
readiness_seconds = 1
poll_seconds = 0.05

[values]
parts = "{parts}"
"""


def _write_spec(root: Path, parts: str, extra: str = "") -> None:
    (root / "release.toml").write_text(
        TOML.format(namespace=TARGET.namespace, parts=parts, extra=extra)
    )


@pytest.fixture
def env(tmp_path):
    with serve() as (api, url):
        (tmp_path / "kubeconfig").write_text(
            textwrap.dedent(
                f"""
                apiVersion: v1
                kind: Config
                current-context: must-not-be-used
                clusters:
                - name: fake
                  cluster: {{server: "{url}"}}
                users:
                - name: nobody
                  user: {{}}
                contexts:
                - name: fake
                  context: {{cluster: fake, user: nobody}}
                - name: must-not-be-used
                  context: {{cluster: fake, user: nobody}}
                """
            )
        )
        (tmp_path / "compose.py").write_text(COMPOSITION)
        _receipt(tmp_path / "build.receipt.json", DIGEST)
        _write_spec(tmp_path, "ab")
        yield api, tmp_path


def _run(root: Path, *args: str):
    result = CliRunner().invoke(app, [*args, "--spec", str(root / "release.toml")])
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload, result


def _deploy_v1_then_v2(api, root: Path) -> None:
    code, applied, result = _run(root, "apply", "--auto-approve")
    assert code == 0, result.output
    assert ("PersistentVolumeClaim", "b-data") in api.objects
    _write_spec(root, "a")
    code, applied, result = _run(root, "apply", "--auto-approve")
    assert code == 0, result.output
    # No [release] prune: B stays in the namespace.
    assert ("ConfigMap", "b-config") in api.objects


def _leftover(kind: str, name: str, owner: str | None, labels=LABELS, **metadata):
    value = manifest(kind, name)
    value["metadata"]["labels"] = dict(labels)
    value["metadata"]["creationTimestamp"] = "2026-01-01T00:00:00Z"
    if owner is not None:
        value["metadata"]["annotations"] = {"piceli.io/owner": owner}
    value["metadata"].update(metadata)
    return value


def _names(payload, *, prunable: bool | None = None) -> set[str]:
    return {
        f"{item['kind']}/{item['name']}"
        for item in payload["orphans"]
        if prunable is None or item["prunable"] is prunable
    }


def test_owned_objects_are_not_listed(env):
    api, root = env
    code, _, result = _run(root, "apply", "--auto-approve")
    assert code == 0, result.output
    code, report, _ = _run(root, "orphans")
    assert code == 0
    assert report["state"] == "listed"
    assert report["orphans"] == []
    assert report["app_labels"] == LABELS


def test_removed_component_and_other_environment_are_listed(env):
    api, root = env
    _deploy_v1_then_v2(api, root)
    api.put(_leftover("Deployment", "test-r05-web", "shop-test"))
    api.put(_leftover("ConfigMap", "hand-made", None))  # no Piceli owner
    api.put(_leftover("ConfigMap", "elsewhere", "acceptance-owner", labels={}))
    api.put(_leftover("ConfigMap", "stranger", "other-team", labels={"x": "y"}))
    code, report, _ = _run(root, "orphans")
    assert code == 0
    assert _names(report) == {
        "ConfigMap/b-config",
        "PersistentVolumeClaim/b-data",
        "ConfigMap/elsewhere",
        "Deployment/test-r05-web",
    }
    by_name = {item["name"]: item for item in report["orphans"]}
    removed = by_name["b-config"]
    assert removed["reason"] == "not-in-current-release"
    assert "piceli.io/owner" in removed["matched"]
    assert removed["prunable"] is True
    other = by_name["test-r05-web"]
    assert other["reason"] == "other-owner"
    assert other["owner"] == "shop-test"
    assert other["matched"] == ["app.kubernetes.io/part-of=shop"]
    assert other["created_at"] == "2026-01-01T00:00:00Z"
    assert other["age_seconds"] > 0
    assert other["prunable"] is False
    assert by_name["b-data"]["blocked_by"] == ["claims: needs --include-claims"]
    # Read-only: nothing was deleted.
    assert ("ConfigMap", "b-config") in api.objects


def test_prune_needs_approval_then_deletes_exactly_the_approved_set(env):
    api, root = env
    _deploy_v1_then_v2(api, root)
    api.put(_leftover("ConfigMap", "hand-made", None))
    code, pending, _ = _run(root, "orphans", "--prune")
    assert code == 3
    assert pending["state"] == "approval-required"
    assert _names(pending, prunable=True) == {"ConfigMap/b-config"}
    assert ("ConfigMap", "b-config") in api.objects  # nothing without approval
    code, done, result = _run(
        root, "orphans", "--prune", "--approve", pending["plan_hash"]
    )
    assert code == 0, result.output
    assert done["state"] == "succeeded"
    assert done["deleted"] == [
        {"kind": "ConfigMap", "name": "b-config", "outcome": "deleted"}
    ]
    assert ("ConfigMap", "b-config") not in api.objects
    # Kept by default: the claim of B, and everything not owned.
    assert ("PersistentVolumeClaim", "b-data") in api.objects
    assert ("ConfigMap", "hand-made") in api.objects
    assert ("ConfigMap", "settings") not in api.objects
    assert ("ConfigMap", "a-config") in api.objects
    deletes = [r for r in api.requests if r["method"] == "DELETE"]
    assert len(deletes) == 1


def test_changed_set_is_refused_and_nothing_is_deleted(env):
    api, root = env
    _deploy_v1_then_v2(api, root)
    code, pending, _ = _run(root, "orphans", "--prune")
    assert code == 3
    api.put(_leftover("ConfigMap", "late", "acceptance-owner"))  # set grew
    code, refused, _ = _run(
        root, "orphans", "--prune", "--approve", pending["plan_hash"]
    )
    assert code == 2
    assert refused["reason"] == "orphans-plan-changed"
    assert refused["plan_hash"] != pending["plan_hash"]
    assert ("ConfigMap", "b-config") in api.objects
    assert ("ConfigMap", "late") in api.objects
    # An object changing under the same name (new resourceVersion) is refused too.
    code, pending, _ = _run(root, "orphans", "--prune")
    api.put(_leftover("ConfigMap", "late", "acceptance-owner"))
    code, refused, _ = _run(
        root, "orphans", "--prune", "--approve", pending["plan_hash"]
    )
    assert code == 2 and refused["reason"] == "orphans-plan-changed"


def test_claims_secrets_other_owners_need_their_flags(env):
    api, root = env
    _deploy_v1_then_v2(api, root)
    api.put(_leftover("Secret", "b-token", "acceptance-owner"))
    api.put(_leftover("Deployment", "test-r05-web", "shop-test"))
    flags = [
        "--include-claims",
        "--include-secrets",
        "--include-other-owners",
    ]
    code, report, _ = _run(root, "orphans", "--prune", *flags)
    assert code == 3
    assert _names(report, prunable=True) == {
        "ConfigMap/b-config",
        "PersistentVolumeClaim/b-data",
        "Secret/b-token",
        "Deployment/test-r05-web",
    }
    code, done, result = _run(
        root, "orphans", "--prune", "--approve", report["plan_hash"], *flags
    )
    assert code == 0, result.output
    assert {(i["kind"], i["outcome"]) for i in done["deleted"]} == {
        ("ConfigMap", "deleted"),
        ("PersistentVolumeClaim", "deleted"),
        ("Secret", "deleted"),
        ("Deployment", "deleted"),
    }
    assert ("PersistentVolumeClaim", "b-data") not in api.objects
    assert ("ConfigMap", "a-config") in api.objects
    # The flags are part of the approved hash: the default set differs.
    code, other, _ = _run(root, "orphans", "--prune")
    assert other["plan_hash"] != report["plan_hash"]


def test_a_claim_prune_waits_until_the_claim_is_gone(env):
    """A claim stays terminating while a pod mounts it (``pvc-protection``):
    the prune waits for it and reports ``deleted`` only once it is gone."""
    api, root = env
    api.server_defaults = True  # claims carry pvc-protection
    _deploy_v1_then_v2(api, root)
    code, pending, _ = _run(root, "orphans", "--prune", "--include-claims")
    assert code == 3
    api.terminating_reads = 3
    code, done, result = _run(
        root,
        "orphans",
        "--prune",
        "--approve",
        pending["plan_hash"],
        "--include-claims",
    )
    assert code == 0, result.output
    assert {(i["kind"], i["outcome"]) for i in done["deleted"]} == {
        ("ConfigMap", "deleted"),
        ("PersistentVolumeClaim", "deleted"),
    }
    assert ("PersistentVolumeClaim", "b-data") not in api.objects


def test_a_claim_still_terminating_is_reported_deleting(env):
    api, root = env
    api.server_defaults = True  # claims carry pvc-protection
    _deploy_v1_then_v2(api, root)
    code, pending, _ = _run(root, "orphans", "--prune", "--include-claims")
    assert code == 3
    api.terminating_reads = 10_000  # beyond readiness_seconds (1 s)
    code, done, result = _run(
        root,
        "orphans",
        "--prune",
        "--approve",
        pending["plan_hash"],
        "--include-claims",
    )
    assert code == 0, result.output
    assert done["state"] == "succeeded"
    claim = next(i for i in done["deleted"] if i["kind"] == "PersistentVolumeClaim")
    assert claim == {
        "kind": "PersistentVolumeClaim",
        "name": "b-data",
        "outcome": "deleting",
        "finalizers": ["kubernetes.io/pvc-protection"],
    }
    # A terminating object is no longer a leftover.
    code, report, _ = _run(root, "orphans", "--include-claims")
    assert code == 0
    assert report["orphans"] == []


def test_statefulset_deleting_its_claims_is_kept_without_include_claims(env):
    api, root = env
    _deploy_v1_then_v2(api, root)
    keep = _leftover("StatefulSet", "b-db", "acceptance-owner")
    keep["spec"] = {"persistentVolumeClaimRetentionPolicy": {"whenDeleted": "Delete"}}
    api.put(keep)
    api.put(_leftover("StatefulSet", "b-plain", "acceptance-owner"))
    code, report, _ = _run(root, "orphans")
    blocked = {i["name"]: i["blocked_by"] for i in report["orphans"]}
    assert blocked["b-db"] and not blocked["b-plain"]


def test_objects_with_owner_references_or_terminating_are_ignored(env):
    api, root = env
    _deploy_v1_then_v2(api, root)
    api.put(
        _leftover(
            "ConfigMap",
            "child",
            "acceptance-owner",
            ownerReferences=[
                {"apiVersion": "v1", "kind": "ConfigMap", "name": "x", "uid": "u1"}
            ],
        )
    )
    api.put(
        _leftover(
            "ConfigMap",
            "dying",
            "acceptance-owner",
            deletionTimestamp="2026-01-01T00:00:00Z",
        )
    )
    code, report, _ = _run(root, "orphans")
    assert "ConfigMap/child" not in _names(report)
    assert "ConfigMap/dying" not in _names(report)


def test_refusals(env):
    api, root = env
    code, refused, _ = _run(root, "orphans")
    assert code == 2 and refused["reason"] == "orphans-no-release"
    code, refused, _ = _run(root, "orphans", "--approve", "0" * 64)
    assert code == 2 and refused["reason"] == "orphans-approve-without-prune"
    code, refused, _ = _run(root, "orphans", "--prune", "--approve", "abc")
    assert code == 2 and refused["reason"] == "invalid-plan-hash"


def test_nothing_to_prune_succeeds_without_approval(env):
    api, root = env
    code, _, result = _run(root, "apply", "--auto-approve")
    assert code == 0, result.output
    code, report, _ = _run(root, "orphans", "--prune")
    assert code == 0
    assert report["state"] == "succeeded"
    assert report["summary"]["total"] == 0
