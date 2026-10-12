"""Opt-in: the operator's namespace reaches an exposed pod under an IH-style policy.

Reproduces k-lab's ``ih-main`` (0.19 P0) on a disposable kind cluster whose
CNI enforces NetworkPolicies: the policy in front of the pods selects them
through a label the Service selector does not use. Before
:func:`piceli.envs.access.allow_tailnet` a pod in the operator's namespace
cannot reach the Service; after it, it can, and a pod in any other
namespace still cannot::

    kind create cluster --name piceli-access --kubeconfig /tmp/access.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/access.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-access \\
      uv run pytest tests/integration/test_tailnet_policy_kind.py

Every namespace it creates is deleted when it ends.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from piceli.envs.access import Access, allow_tailnet, tailnet_plan

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
NGINX = "nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
BUSYBOX = "docker.io/library/busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"

pytestmark = [
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT",
    ),
    pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl"),
    pytest.mark.timeout(600),
]

APP = {"app.kubernetes.io/name": "shop", "app.kubernetes.io/part-of": "shop-p2"}


def kubectl(*args: str, data: str | None = None, check: bool = True) -> str:
    result = subprocess.run(
        ["kubectl", "--kubeconfig", KUBECONFIG, "--context", CONTEXT, *args],
        input=data, capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    if check and result.returncode != 0:
        raise AssertionError(result.stderr[-2000:])
    return result.stdout


@pytest.fixture
def namespaces() -> Iterator[dict[str, str]]:
    suffix = uuid.uuid4().hex[:6]
    names = {
        "app": f"shop-{suffix}",
        "operator": f"ts-{suffix}",
        "other": f"other-{suffix}",
    }
    for name in names.values():
        kubectl("create", "namespace", name)
    try:
        yield names
    finally:
        for name in names.values():
            kubectl("delete", "namespace", name, "--wait=false", check=False)


def _manifests(namespace: str) -> list[dict[str, Any]]:
    pod_labels = {**APP, "shop.network": "primary", "shop.role": "web"}
    return [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "web", "namespace": namespace},
            "spec": {
                "selector": {"matchLabels": {**APP, "shop.role": "web"}},
                "template": {
                    "metadata": {"labels": pod_labels},
                    "spec": {"containers": [{"name": "web", "image": NGINX,
                                             "ports": [{"containerPort": 80}]}]},
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "web", "namespace": namespace},
            "spec": {"selector": {**APP, "shop.role": "web"},
                     "ports": [{"port": 80, "targetPort": 80}]},
        },
        # Like ih-ingress: selects the pods by a label the Service does not use.
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "shop-ingress", "namespace": namespace},
            "spec": {
                "podSelector": {"matchLabels": {**APP, "shop.network": "primary"}},
                "policyTypes": ["Ingress"],
                "ingress": [{"from": [{"podSelector": {"matchLabels": APP}}]}],
            },
        },
    ]  # fmt: skip


def _reaches(namespace: str, url: str) -> bool:
    name = f"probe-{uuid.uuid4().hex[:6]}"
    output = kubectl(
        "-n", namespace, "run", name, "--image", BUSYBOX, "--restart=Never",
        "--rm", "-i", "--quiet", "--command", "--",
        "sh", "-c", f"wget -T 4 -qO- {url} >/dev/null && echo REACHED || echo BLOCKED",
        check=False,
    )  # fmt: skip
    return "REACHED" in output


def _wait_reachable(namespace: str, url: str, expected: bool) -> bool:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _reaches(namespace, url) == expected:
            return True
        time.sleep(3)
    return False


def test_the_operator_namespace_reaches_pods_behind_an_ih_style_policy(
    namespaces: dict[str, str],
) -> None:
    app, operator, other = (
        namespaces["app"],
        namespaces["operator"],
        namespaces["other"],
    )
    manifests = _manifests(app)
    kubectl("apply", "-f", "-", data=json.dumps({"apiVersion": "v1", "kind": "List",
                                                "items": manifests}))  # fmt: skip
    kubectl("-n", app, "rollout", "status", "deployment/web", "--timeout=180s")
    url = f"http://web.{app}.svc.cluster.local/"
    # Enforced: neither the operator's namespace nor another one gets in.
    if _reaches(operator, url):
        pytest.skip("this cluster's CNI does not enforce NetworkPolicies")
    assert not _reaches(other, url)

    access = Access(tailnet=True, operator_namespace=operator)
    amended = copy.deepcopy(manifests)
    plan = tailnet_plan(amended, access, {"web": (80,)}, "main", app)
    policies = [m for m in amended if m["kind"] == "NetworkPolicy"]
    changed, notes = allow_tailnet(policies, plan.backends, amended, access)
    assert [p["metadata"]["name"] for p in changed] == ["shop-ingress"] and notes == []
    kubectl("apply", "-f", "-", data=json.dumps(changed[0]))

    assert _wait_reachable(operator, url, True), (
        "the operator's namespace is still blocked"
    )
    assert not _reaches(other, url), "another namespace got in"
