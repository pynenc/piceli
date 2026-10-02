"""An HTTP check reaches a workload in an isolated environment (kind enforces policies).

A branch environment's ``piceli-env-isolation`` NetworkPolicy admits traffic
only from its own namespace, so the API server's proxy cannot reach its pods.
The check goes through a port forward of the API instead (the connection
starts inside the pod), with no ``kubectl`` on ``PATH``; a pod in another
namespace still cannot reach the workload.

Needs PICELI_KIND_KUBECONFIG, PICELI_KIND_CONTEXT and kubectl (to set up and
to probe from a pod). It never uses the current context and deletes both
namespaces it creates.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from piceli.checks import CheckContext, CheckError, Checks, run_checks
from piceli.envs import BranchEnv, EnvConfig
from piceli.envs.isolation import isolation_objects

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
KUBECTL = shutil.which("kubectl")
# nginx:1.27-alpine multi-arch index digest (BusyBox wget included).
IMAGE = (
    "docker.io/library/nginx@"
    "sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(600),
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT and KUBECTL),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT, and install kubectl",
    ),
]


def _kubectl(*args: str, stdin: str = "", check: bool = True) -> str:
    result = subprocess.run(  # fixed argv, explicit kubeconfig
        [str(KUBECTL), "--kubeconfig", KUBECONFIG, "--context", CONTEXT, *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if check:
        assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout


def _worker() -> str | None:
    """A node without the control-plane role (the API server runs elsewhere)."""
    nodes = json.loads(_kubectl("get", "nodes", "-o", "json"))["items"]
    for node in nodes:
        labels = node["metadata"].get("labels") or {}
        if not any(
            key.startswith("node-role.kubernetes.io/control-plane")
            or key.startswith("node-role.kubernetes.io/master")
            for key in labels
        ):
            return str(node["metadata"]["name"])
    return None


def _web(namespace: str, node: str | None = None) -> list[dict[str, object]]:
    labels = {"app": "web"}
    return [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "web", "namespace": namespace},
            "spec": {
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        **(
                            {"nodeSelector": {"kubernetes.io/hostname": node}}
                            if node
                            else {}
                        ),
                        "containers": [
                            {
                                "name": "web",
                                "image": IMAGE,
                                "ports": [{"name": "http", "containerPort": 80}],
                            }
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "web", "namespace": namespace},
            "spec": {
                "selector": labels,
                "ports": [{"port": 8080, "targetPort": "http"}],
            },
        },
    ]


@pytest.fixture
def environments() -> Iterator[tuple[str, str]]:
    suffix = uuid.uuid4().hex[:8]
    isolated, other = f"iso-{suffix}", f"other-{suffix}"
    try:
        for name in (isolated, other):
            _kubectl("create", "namespace", name)
        yield isolated, other
    finally:
        for name in (isolated, other):
            _kubectl("delete", "namespace", name, "--wait=false", check=False)


def test_an_http_check_passes_in_an_isolated_environment(
    environments: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated, other = environments
    policy, _ = isolation_objects(
        BranchEnv("wp-x", isolated, EnvConfig(prefix="iso-"), "main")
    )
    # On a multi-node cluster the isolated workload runs away from the API
    # server: its proxied traffic then crosses the policy (as on k3s nodes).
    items = [*_web(isolated, _worker()), *_web(other), policy]
    _kubectl(
        "apply",
        "-f",
        "-",
        stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": items}),
    )
    for name in (isolated, other):
        _kubectl("-n", name, "rollout", "status", "deployment/web", "--timeout=240s")
    client = _kubectl(
        "-n", other, "get", "pods", "-l", "app=web", "-o", "jsonpath={.items[0].metadata.name}"
    )  # fmt: skip

    def reaches() -> bool:
        probe = subprocess.run(  # fixed argv, explicit kubeconfig
            [
                str(KUBECTL), "--kubeconfig", KUBECONFIG, "--context", CONTEXT,
                "-n", other, "exec", client, "--",
                "wget", "-q", "-T", "3", "-O", "/dev/null", f"http://web.{isolated}:8080",
            ],
            capture_output=True, timeout=60, check=False,
        )  # fmt: skip
        return probe.returncode == 0

    # Isolation holds: another environment cannot reach the workload.
    deadline = time.monotonic() + 60
    while reaches() and time.monotonic() < deadline:
        time.sleep(2)
    assert not reaches()

    # No kubectl for the check (as in the controller image).
    from piceli.checks import context as module

    forwarded: list[str] = []
    real = module.api_forward_get

    def spy(*args: object, **kwargs: object) -> module.HttpResponse:
        answer = real(*args, **kwargs)  # type: ignore[arg-type]
        forwarded.append(str(args[2]))  # only a port forward that answered
        return answer

    monkeypatch.setattr(module, "api_forward_get", spy)
    monkeypatch.setenv("PATH", str(tmp_path))
    with CheckContext(
        Path(KUBECONFIG), CONTEXT, isolated, "release-1", kubectl="kubectl"
    ) as context:
        # Through the API server's proxy the isolation decides: k3s (the API
        # server on the node, kube-router) refuses it (HTTP 503), kind's
        # kindnet admits node traffic. Either way the check does not use it.
        try:
            context.proxy_get("service/web", "/", timeout=5)
            print("API proxy to the isolated pod: admitted")
        except CheckError as error:
            print(f"API proxy to the isolated pod: refused ({error.code})")
        report = run_checks(
            [
                Checks.http("service/web", "/", expect=200, retries=2),
                Checks.http("deployment/web", "/", port=80, expect=200, retries=2),
            ],
            context,
        )
    assert report.passed, report.to_dict()
    assert len(forwarded) >= 2 and all(name.startswith("web-") for name in forwarded)
