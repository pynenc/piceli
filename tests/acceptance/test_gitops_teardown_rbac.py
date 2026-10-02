"""Branch teardown with exactly the controller's RBAC grants (fake API).

The controller removes a branch environment with its service account: the
ClusterRole ``piceli-gitops`` everywhere and the deployer ClusterRole through
the RoleBinding in the branch namespace. :class:`RbacFakeAPI` refuses
everything else with ``403``, as the API server does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from piceli import App, EnvConfig, Pipeline, Target
from piceli.envs import env_down
from piceli.envs.model import (
    ENV_BRANCH_ANNOTATION,
    ENV_BRANCH_LABEL,
    ENV_OF_LABEL,
    EnvError,
)
from piceli.envs.ops import branch_label
from piceli.gitops.install import DEPLOYER, NAME, render_foundation
from piceli.testing import manifest, serve, write_kubeconfig
from tests.acceptance.rbac_support import RbacFakeAPI

BRANCH = "wp-gone"
NAMESPACE = "shop-wp-gone"


def _rules(name: str, **options: bool) -> list[dict[str, Any]]:
    role = next(
        item
        for item in render_foundation("piceli-system", **options)
        if item["kind"] == "ClusterRole" and item["metadata"]["name"] == name
    )
    return list(role["rules"])


def _api(cluster_rules: list[dict[str, Any]]) -> RbacFakeAPI:
    api = RbacFakeAPI(cluster_rules, {NAMESPACE: _rules(DEPLOYER)}, namespace=NAMESPACE)
    env_ns = api.objects[("Namespace", NAMESPACE)]
    env_ns["metadata"]["labels"] = {
        "app.kubernetes.io/managed-by": "piceli",
        ENV_OF_LABEL: "shop",
        ENV_BRANCH_LABEL: branch_label(BRANCH),
    }
    env_ns["metadata"]["annotations"] = {ENV_BRANCH_ANNOTATION: BRANCH}
    claim = manifest("PersistentVolumeClaim", "data-db-0")
    api.put(claim)
    volume = manifest("PersistentVolume", "pv-data-db-0")
    volume["spec"] = {"claimRef": {"namespace": NAMESPACE, "name": "data-db-0"}}
    api.put(volume)
    return api


def _pipeline(kubeconfig: Path, tmp_path: Path) -> Pipeline:
    return Pipeline(
        App("shop"),
        Target.kubeconfig(
            kubeconfig, context="fake", namespace="shop", transport="loopback-http"
        ),
        envs=EnvConfig(prefix="shop-", branches=["wp-*"], auto_approve=True),
        state_dir=tmp_path / "state",
    )


def test_teardown_needs_only_what_the_controller_is_granted(tmp_path: Path) -> None:
    with serve(_api(_rules(NAME, delete_volumes=True))) as (api, url):
        kubeconfig = write_kubeconfig(url, tmp_path / "kubeconfig", context="fake")
        result = env_down(
            _pipeline(kubeconfig, tmp_path), BRANCH, approve_if_policy=True
        )
    # Only the probe for the release's ClusterRoles is refused: a controller
    # without --cluster-rbac could not have made any.
    assert [(d["verb"], d["resource"]) for d in api.denied] == [
        ("list", "clusterroles")
    ]
    assert result["state"] == "removed"
    assert result["delete"] == {
        "claims": ["data-db-0"],
        "volumes": ["pv-data-db-0"],
        "cluster_objects": [],
    }
    assert "volumes_left" not in result
    assert ("PersistentVolume", "pv-data-db-0") not in api.objects


def test_a_denied_request_names_its_verb_resource_and_namespace(
    tmp_path: Path,
) -> None:
    # Without the persistent volume rule (as installed by 0.14.1).
    narrow = [r for r in _rules(NAME) if "persistentvolumes" not in r["resources"]]
    with serve(_api(narrow)) as (api, url):
        kubeconfig = write_kubeconfig(url, tmp_path / "kubeconfig", context="fake")
        with pytest.raises(EnvError) as refused:
            env_down(_pipeline(kubeconfig, tmp_path), BRANCH, approve_if_policy=True)
    assert refused.value.code == "env-cluster-unavailable"
    assert refused.value.details["denied"] == {
        "verb": "list",
        "resource": "persistentvolumes",
        "namespace": None,
        "status": 403,
    }
    assert "list persistentvolumes" in str(refused.value)
    assert "Forbidden" not in str(refused.value)  # never the server's body


def test_a_denied_grant_names_its_request(tmp_path: Path) -> None:
    from piceli.gitops import GitOpsError
    from piceli.gitops.install import grant_env_access

    narrow = [r for r in _rules(NAME) if "rolebindings" not in r["resources"]]
    fake = _api(narrow)
    fake.namespace_rules = {}  # no RoleBinding in the namespace yet
    with serve(fake) as (api, url):
        kubeconfig = write_kubeconfig(url, tmp_path / "kubeconfig", context="fake")
        with pytest.raises(GitOpsError) as refused:
            grant_env_access(
                kubeconfig,
                "fake",
                NAMESPACE,
                controller_namespace="piceli-system",
                branch=BRANCH,
                transport="loopback-http",
            )
    assert refused.value.details["denied"] == {
        "verb": "get",
        "resource": "rolebindings",
        "namespace": NAMESPACE,
        "status": 403,
    }
    assert f"get rolebindings in namespace {NAMESPACE}" in str(refused.value)


def test_by_default_teardown_keeps_volumes_and_reports_them(tmp_path: Path) -> None:
    """Deleting PersistentVolumes cluster-wide is opt-in (delete_volumes)."""
    assert not any(
        "persistentvolumes" in rule["resources"] and "delete" in rule["verbs"]
        for rule in _rules(NAME)
    )
    with serve(_api(_rules(NAME))) as (api, url):
        kubeconfig = write_kubeconfig(url, tmp_path / "kubeconfig", context="fake")
        result = env_down(
            _pipeline(kubeconfig, tmp_path), BRANCH, approve_if_policy=True
        )
    assert result["state"] == "removed"
    assert result["volumes_left"] == ["pv-data-db-0"]
    assert ("PersistentVolume", "pv-data-db-0") in api.objects
    assert ("Namespace", NAMESPACE) not in api.objects


def test_teardown_removes_the_cluster_wide_objects_of_its_release(
    tmp_path: Path,
) -> None:
    """ClusterRoles/Bindings named ``<namespace>:<app>:…`` with Piceli's owner go too."""
    rules = _rules(NAME, cluster_rbac=True, delete_volumes=True)
    fake = _api(rules)
    owned = {"piceli.io/owner": "shop"}
    for kind in ("ClusterRole", "ClusterRoleBinding"):
        mine = manifest(kind, f"{NAMESPACE}:shop:reader")
        mine["metadata"]["annotations"] = dict(owned)
        fake.put(mine)
        other = manifest(kind, "shop-main:shop:reader")  # another environment's
        other["metadata"]["annotations"] = dict(owned)
        fake.put(other)
        foreign = manifest(kind, f"{NAMESPACE}:shop:by-hand")  # not Piceli's
        fake.put(foreign)
        # A renamed object of this environment (``<name>-<namespace>``).
        labelled = manifest(kind, f"reader-{NAMESPACE}")
        labelled["metadata"]["annotations"] = dict(owned)
        labelled["metadata"]["labels"] = {"piceli.io/env-namespace": NAMESPACE}
        fake.put(labelled)
        # Another environment whose name merely ends like this one.
        lookalike = manifest(kind, f"x-{NAMESPACE}")
        lookalike["metadata"]["annotations"] = dict(owned)
        lookalike["metadata"]["labels"] = {"piceli.io/env-namespace": "x-" + NAMESPACE}
        fake.put(lookalike)
    with serve(fake) as (api, url):
        kubeconfig = write_kubeconfig(url, tmp_path / "kubeconfig", context="fake")
        result = env_down(
            _pipeline(kubeconfig, tmp_path), BRANCH, approve_if_policy=True
        )
    assert result["state"] == "removed"
    assert result["delete"]["cluster_objects"] == [
        f"ClusterRole/reader-{NAMESPACE}",
        f"ClusterRole/{NAMESPACE}:shop:reader",
        f"ClusterRoleBinding/reader-{NAMESPACE}",
        f"ClusterRoleBinding/{NAMESPACE}:shop:reader",
    ]
    for kind in ("ClusterRole", "ClusterRoleBinding"):
        assert (kind, f"{NAMESPACE}:shop:reader") not in api.objects
        assert (kind, "shop-main:shop:reader") in api.objects
        assert (kind, f"{NAMESPACE}:shop:by-hand") in api.objects
        assert (kind, f"reader-{NAMESPACE}") not in api.objects
        assert (kind, f"x-{NAMESPACE}") in api.objects


def test_without_cluster_rbac_teardown_has_no_cluster_objects_to_remove(
    tmp_path: Path,
) -> None:
    """A controller that cannot list ClusterRoles could not have created any."""
    with serve(_api(_rules(NAME, delete_volumes=True))) as (api, url):
        kubeconfig = write_kubeconfig(url, tmp_path / "kubeconfig", context="fake")
        result = env_down(
            _pipeline(kubeconfig, tmp_path), BRANCH, approve_if_policy=True
        )
    assert result["state"] == "removed"
    assert result["delete"]["cluster_objects"] == []
