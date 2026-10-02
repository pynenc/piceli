"""Opt-in: the GitOps controller's RBAC on a real cluster (kind).

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-it --kubeconfig /tmp/it.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/it.kubeconfig PICELI_KIND_CONTEXT=kind-piceli-it \\
      uv run pytest tests/integration/test_gitops_rbac_kind.py

It installs the controller's foundation (``render_foundation``: service
account, roles, bindings) in a uniquely named namespace, binds the deployer
role in a branch namespace as ``gitops`` does, then acts as the controller's
service account (``--as``):

* a branch teardown may list and delete persistent volumes;
* a release may create a Role granting ``metrics.k8s.io`` reads (the API
  server refuses an escalation when the deployer lacks them);
* restore points may scale Deployments and StatefulSets.

The cluster-scoped roles have fixed names: the test is skipped when another
install already holds them, and removes what it created.
"""

from __future__ import annotations

import json
import uuid

import pytest
from kind_support import kubectl, requires_kind

from piceli.gitops.install import DEPLOYER, MANAGED, NAME, render_foundation

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300), requires_kind]


def _as(namespace: str) -> str:
    return f"--as=system:serviceaccount:{namespace}:{NAME}"


def _can(namespace: str, *args: str) -> bool:
    out = kubectl("auth", "can-i", *args, _as(namespace), check=False)
    return out.strip() == "yes"


def test_the_controller_may_tear_down_and_releases_may_grant_metrics_and_scale() -> (
    None
):
    for kind, name in (("clusterrole", NAME), ("clusterrole", DEPLOYER)):
        if kubectl("get", kind, name, "--ignore-not-found", "-o", "name").strip():
            pytest.skip(f"{kind}/{name} already exists on this cluster")
    suffix = uuid.uuid4().hex[:8]
    system, env = f"pg-{suffix}", f"pg-{suffix}-env"
    objects = [
        item
        for item in render_foundation(system)
        if item["kind"] != "PersistentVolumeClaim"
    ]
    binding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": DEPLOYER, "namespace": env, "labels": dict(MANAGED)},
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": DEPLOYER,
        },
        "subjects": [{"kind": "ServiceAccount", "name": NAME, "namespace": system}],
    }
    try:
        kubectl(
            "apply",
            "-f",
            "-",
            stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}),
        )
        kubectl("create", "namespace", env)
        kubectl("apply", "-f", "-", stdin=json.dumps(binding))
        # Bug: a branch teardown listed persistent volumes and got HTTP 403.
        assert _can(system, "list", "persistentvolumes")
        assert _can(system, "delete", "persistentvolumes")
        assert _can(system, "delete", "namespaces")
        assert not _can(system, "create", "persistentvolumes")
        # Restore points scale workloads in the branch namespace.
        for kind in ("deployments", "statefulsets"):
            assert _can(system, "patch", kind, "--subresource=scale", "-n", env)
        # A release's own Role with metrics reads is not an escalation.
        role = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "metrics-reader", "namespace": env},
            "rules": [
                {
                    "apiGroups": ["metrics.k8s.io"],
                    "resources": ["pods", "nodes"],
                    "verbs": ["get", "list", "watch"],
                }
            ],
        }
        kubectl("create", "-f", "-", _as(system), stdin=json.dumps(role))
    finally:
        kubectl("delete", "namespace", env, "--wait=false", check=False)
        kubectl("delete", "namespace", system, "--wait=false", check=False)
        for kind in ("clusterrolebinding", "clusterrole"):
            kubectl("delete", kind, NAME, "--ignore-not-found", check=False)
        kubectl("delete", "clusterrole", DEPLOYER, "--ignore-not-found", check=False)
