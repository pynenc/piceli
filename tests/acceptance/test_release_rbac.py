"""Acceptance: a typed app with RBAC (cluster-scoped objects included) through
``piceli release`` against the fake API.

Cluster-scoped objects (ClusterRole, ClusterRoleBinding) are shared by every
namespace, so the release must plan, apply, prune and roll back only its own:
the ones carrying its owner **and** its ``piceli.io/namespace``.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from piceli.k8s.ops.discovery import RELEASE_NAMESPACE_ANNOTATION
from piceli.testing import TARGET, FakeAPI, serve, write_kubeconfig

DIGEST = "sha256:" + "4" * 64
OWNER = "rbac-shop"
NS = TARGET.namespace
CLUSTER_NAME = f"{NS}:shop:watcher"
RBAC = "rbac.authorization.k8s.io/v1"

COMPOSITION = """
from piceli import App, PodDefaults, Rule, Security

CLUSTER = {cluster}


def build(ctx):
    shop = App("shop", pod_defaults=PodDefaults(
        security=Security.restricted(user=10001, fs_group=10001),
        node_selector={{"kubernetes.io/arch": "amd64"}},
        termination_grace_seconds=30,
        automount_token=False,
    ))
    watcher = shop.service_account(
        "watcher",
        rules=[Rule(resources=["pods"], verbs=["get", "list", "watch"])],
        cluster_rules=(
            [Rule(resources=["nodes"], verbs=["get", "list"])] if CLUSTER else []
        ),
    )
    shop.deployment("watcher", image=ctx.image("watcher"), service_account=watcher)
    shop.network_policy(
        selector=shop.release_selector,
        allow_from_selector=shop.release_selector,
        name="shop-internal",
    )
    return shop
"""


def _cluster_role(name: str, namespace: str | None, owner: str = OWNER) -> dict:
    annotations = {"piceli.io/owner": owner}
    if namespace is not None:
        annotations[RELEASE_NAMESPACE_ANNOTATION] = namespace
    return {
        "apiVersion": RBAC,
        "kind": "ClusterRole",
        "metadata": {"name": name, "annotations": annotations},
        "rules": [],
    }


@pytest.fixture
def rbac_env(tmp_path):
    for name, cluster in (("full.py", True), ("trimmed.py", False)):
        (tmp_path / name).write_text(COMPOSITION.format(cluster=cluster))
    api = FakeAPI()
    # The same owner released the app in another namespace; and an object of
    # this owner from before namespace scoping. Neither is this release's.
    api.put(_cluster_role("other-ns:shop:watcher", "other-ns"))
    api.put(_cluster_role("legacy-shop-reader", None))
    with serve(api) as (api, url):
        write_kubeconfig(url, tmp_path / "kubeconfig")
        yield api, tmp_path


def _spec(directory: Path, module: str) -> Path:
    spec = directory / "release.toml"
    spec.write_text(
        textwrap.dedent(
            f"""
            [target]
            kubeconfig = "kubeconfig"
            context = "fake"
            namespace = "{NS}"
            transport = "loopback-http"

            [release]
            name = "shop"
            owner = "{OWNER}"
            field_manager = "{OWNER}"
            composition = "{module}:build"
            state_dir = "state"
            prune = true

            [execution]
            max_seconds = 30
            readiness_seconds = 1
            poll_seconds = 0.05

            [images]
            watcher = "registry.example/watcher@{DIGEST}"
            """
        )
    )
    return spec


def _run(spec: Path, *args: str) -> tuple[int, dict, str]:
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}"), result.output


def _operations(payload: dict) -> dict[str, str]:
    return {f"{a['kind']}/{a['name']}": a["operation"] for a in payload["actions"]}


def _apply(spec: Path, *args: str) -> dict:
    code, planned, output = _run(spec, *args)
    assert code == 0, output
    code, applied, output = _run(spec, *args, "--approve", planned["plan_hash"])
    assert code == 0, output
    assert applied["execution"]["state"] == "ready", applied
    return planned


def test_release_manages_only_its_own_cluster_scoped_objects(rbac_env):
    api, directory = rbac_env
    spec = _spec(directory, "full.py")

    code, planned, output = _run(spec, "plan")
    assert code == 0, output
    operations = _operations(planned)
    assert operations == {
        "ServiceAccount/watcher": "create",
        "Role/watcher": "create",
        "RoleBinding/watcher": "create",
        f"ClusterRole/{CLUSTER_NAME}": "create",
        f"ClusterRoleBinding/{CLUSTER_NAME}": "create",
        "Deployment/watcher": "create",
        "NetworkPolicy/shop-internal": "create",
    }
    scoped = {
        f"{a['kind']}/{a['name']}"
        for a in planned["actions"]
        if a.get("cluster_scoped")
    }
    assert scoped == {
        f"ClusterRole/{CLUSTER_NAME}",
        f"ClusterRoleBinding/{CLUSTER_NAME}",
    }
    # Other namespaces' objects are never pruned, even with the same owner.
    assert "ClusterRole/other-ns:shop:watcher" not in operations
    assert "ClusterRole/legacy-shop-reader" not in operations

    code, applied, output = _run(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, output
    assert applied["execution"]["state"] == "ready", applied
    role = api.objects[("ClusterRole", CLUSTER_NAME)]
    assert role["metadata"]["annotations"]["piceli.io/owner"] == OWNER
    assert role["metadata"]["annotations"][RELEASE_NAMESPACE_ANNOTATION] == NS
    binding = api.objects[("ClusterRoleBinding", CLUSTER_NAME)]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "watcher", "namespace": NS}
    ]
    pod = api.objects[("Deployment", "watcher")]["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsUser"] == 10001
    assert pod["nodeSelector"] == {"kubernetes.io/arch": "amd64"}
    assert pod["automountServiceAccountToken"] is True

    # Unchanged: every object, the cluster-scoped ones included, is a no-op.
    code, again, output = _run(spec, "plan")
    assert code == 0, output
    assert set(again["summary"]) == {"no-op"}, again["summary"]

    # Dropping the cluster rules prunes exactly this release's cluster objects.
    trimmed = _spec(directory, "trimmed.py")
    code, planned, output = _run(trimmed, "plan")
    assert code == 0, output
    deletes = {k for k, v in _operations(planned).items() if v == "delete"}
    assert deletes == {
        f"ClusterRole/{CLUSTER_NAME}",
        f"ClusterRoleBinding/{CLUSTER_NAME}",
    }
    # Like a real API server, an Orphan delete lingers until the garbage
    # collector removes its finalizer: the release waits, it is not drift.
    api.terminating_reads = 2
    code, applied, output = _run(trimmed, "apply", "--approve", planned["plan_hash"])
    assert code == 0, output
    assert applied["execution"]["state"] == "ready", applied
    assert ("ClusterRole", CLUSTER_NAME) not in api.objects
    assert ("ClusterRoleBinding", CLUSTER_NAME) not in api.objects
    assert ("ClusterRole", "other-ns:shop:watcher") in api.objects
    assert ("ClusterRole", "legacy-shop-reader") in api.objects

    # Rolling back to the first release recreates them.
    code, pending, output = _run(trimmed, "rollback", "previous")
    assert code == 3, output
    assert pending["state"] == "approval-required"
    creates = {k for k, v in _operations(pending).items() if v == "create"}
    assert creates == {
        f"ClusterRole/{CLUSTER_NAME}",
        f"ClusterRoleBinding/{CLUSTER_NAME}",
    }
    code, rolled, output = _run(
        trimmed, "rollback", "previous", "--approve", pending["plan_hash"]
    )
    assert code == 0, output
    assert rolled["execution"]["state"] == "ready", rolled
    assert ("ClusterRole", CLUSTER_NAME) in api.objects


def test_same_name_in_another_namespace_needs_adoption(rbac_env):
    """A cluster object of the same owner but another namespace is not ours."""
    api, directory = rbac_env
    api.field_ownership = True  # a takeover moves field ownership
    api.put(_cluster_role(CLUSTER_NAME, "other-ns"))
    code, refused, output = _run(_spec(directory, "full.py"), "plan")
    assert code == 2, output
    assert refused["reason"] == "resource-requires-adoption"
    assert "ClusterRole" in output
    # An explicit adoption (RBAC names may hold ':') takes it over, and the
    # takeover stamps this release's namespace.
    spec = _spec(directory, "full.py")
    adopt = ("--adopt", f"ClusterRole/{CLUSTER_NAME}")
    code, planned, output = _run(spec, "plan", *adopt)
    assert code == 0, output
    assert _operations(planned)[f"ClusterRole/{CLUSTER_NAME}"] == "adopt"
    code, applied, output = _run(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, output
    annotations = api.objects[("ClusterRole", CLUSTER_NAME)]["metadata"]["annotations"]
    assert annotations[RELEASE_NAMESPACE_ANNOTATION] == NS


def test_raw_composition_cluster_object_rules(tmp_path):
    """Raw compositions: only ClusterRole(Binding), stamped with the namespace."""
    source = textwrap.dedent(
        """
        from piceli.k8s.ops.plan import (
            DeploymentComponent, DeploymentComposition, ResourceIntent,
        )

        KIND = {kind!r}
        ANNOTATIONS = {annotations!r}


        def build(ctx):
            meta = {{"name": "reader"}}
            if ANNOTATIONS is not None:
                meta["annotations"] = ANNOTATIONS
            api = "v1" if KIND == "PersistentVolume" else "rbac.authorization.k8s.io/v1"
            body = {{"apiVersion": api, "kind": KIND, "metadata": meta}}
            return DeploymentComposition(
                (DeploymentComponent("rbac", (ResourceIntent.from_manifest(body),)),)
            )
        """
    )
    with serve() as (api, url):
        write_kubeconfig(url, tmp_path / "kubeconfig")
        cases = [
            ("stamped.py", "ClusterRole", None, 0),
            ("wrong.py", "ClusterRole", {RELEASE_NAMESPACE_ANNOTATION: "other"}, 2),
            ("volume.py", "PersistentVolume", None, 2),
        ]
        for module, kind, annotations, expected in cases:
            (tmp_path / module).write_text(
                source.format(kind=kind, annotations=annotations)
            )
            code, payload, output = _run(_spec(tmp_path, module), "plan")
            assert code == expected, output
            if expected:
                assert payload["reason"] == "invalid-composition"
            else:
                action = payload["actions"][0]
                assert action["cluster_scoped"] is True
