"""Opt-in: the reference app's ``dev`` environment on a real cluster (kind).

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-ref --kubeconfig /tmp/ref.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/ref.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-ref \\
      uv run pytest tests/integration/test_reference_app_kind.py

It needs ``kubectl`` and ``sops`` on ``PATH`` and pulls two small public images
(busybox and redis, pinned by digest); nothing is built. The test never reads
the ambient kubeconfig. It installs the Gateway API CRDs from a pinned
release and the vendored cert-manager ``Certificate`` CRD (no controller runs:
the HTTPRoute and the Certificate are only stored), then, in a uniquely named
namespace, with ``examples/reference`` copied to a temporary directory:

1. deploys ``--env dev`` (plan, then approve the combined hash): the store's
   password is decrypted by ``sops`` with the example-only age key, the
   ``migrate`` Job writes to the store through the release-wide
   NetworkPolicy, and the example's checks pass (an HTTP check of the web
   Service and an exec check of the store);
2. checks readiness with ``piceli status`` and the API: every workload is
   ready, the store's claim is bound, the Job completed;
3. changes one value (the dev autoscaler's ``max_replicas``): the plan
   updates exactly ``HorizontalPodAutoscaler/web`` and the deploy leaves the
   content of every other object unchanged;
4. rolls back to the first release: the autoscaler is restored.

The namespace and the cert-manager CRD are deleted afterwards.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
KUBECTL = shutil.which("kubectl")
SOPS = shutil.which("sops")
ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "reference"
CERTIFICATE_CRD = ROOT / "tests" / "fixtures" / "crds" / "cert-manager-certificate.yaml"
#: Gateway API standard channel CRDs, pinned to one release.
GATEWAY_API_CRDS = (
    "https://github.com/kubernetes-sigs/gateway-api/releases/download/"
    "v1.2.1/standard-install.yaml"
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(1800),
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT and KUBECTL and SOPS),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT, and install "
        "kubectl and sops",
    ),
]


def _kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # fixed argv, explicit kubeconfig and context
        [str(KUBECTL), "--kubeconfig", KUBECONFIG, "--context", CONTEXT, *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=check,
    )


@pytest.fixture
def cluster():
    from kubernetes.client import CoreV1Api

    # Idempotent; retried because a busy API server may time out one request.
    for attempt in range(3):
        result = _kubectl("apply", "--server-side", "-f", GATEWAY_API_CRDS, check=False)
        if result.returncode == 0:
            break
        time.sleep(10 * (attempt + 1))
    else:
        raise AssertionError(f"Gateway API CRDs not installed: {result.stderr[-500:]}")
    _kubectl("apply", "--server-side", "-f", str(CERTIFICATE_CRD))
    for crd in (
        "httproutes.gateway.networking.k8s.io",
        "certificates.cert-manager.io",
    ):
        _kubectl("wait", "--for=condition=Established", "--timeout=120s", f"crd/{crd}")
    name = "ref-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    core = CoreV1Api(client)
    core.create_namespace({"metadata": {"name": name}})
    try:
        yield name, client
    finally:
        core.delete_namespace(name)
        _kubectl("delete", "--wait=false", "-f", str(CERTIFICATE_CRD), check=False)
        client.close()


def _deploy(module: Path, *args: str) -> tuple[int, dict[str, Any], Any]:
    result = CliRunner().invoke(
        cli, ["deploy", f"{module}:pipeline", "--env", "dev", "--json", *args]
    )
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return result.exit_code, lines[-1] if lines else {}, result


def _release(module: Path, *args: str) -> tuple[int, dict[str, Any], Any]:
    result = CliRunner().invoke(
        cli, ["release", *args, "--spec", f"{module}:pipeline", "--env", "dev"]
    )
    return result.exit_code, json.loads(result.stdout or "{}"), result


_KINDS = (
    "deployments,statefulsets,jobs,cronjobs,horizontalpodautoscalers,"
    "poddisruptionbudgets,networkpolicies,configmaps,secrets,services,"
    "serviceaccounts,roles,rolebindings,ingresses,httproutes,certificates"
)


def _versions(namespace: str) -> dict[str, str]:
    """``Kind/name`` → each managed object without its metadata and status.

    Controllers write status (and so the resourceVersion) on their own, and an
    HPA's generation does not follow its spec: compare the content instead.
    """
    listed = json.loads(_kubectl("get", _KINDS, "-n", namespace, "-o", "json").stdout)
    versions = {}
    for item in listed["items"]:
        name = item["metadata"]["name"]
        if name in {"kube-root-ca.crt", "default"}:
            continue  # created by Kubernetes in every namespace
        content = {k: v for k, v in item.items() if k not in {"metadata", "status"}}
        versions[f"{item['kind']}/{name}"] = json.dumps(content, sort_keys=True)
    return versions


def _hpa(client: Any, namespace: str) -> tuple[int, int]:
    from kubernetes.client import AutoscalingV2Api

    spec = (
        AutoscalingV2Api(client)
        .read_namespaced_horizontal_pod_autoscaler("web", namespace)
        .spec
    )
    return spec.min_replicas, spec.max_replicas


def test_reference_app_deploys_changes_one_object_and_rolls_back(
    cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kubernetes.client import AppsV1Api, BatchV1Api, CoreV1Api, CustomObjectsApi

    namespace, client = cluster
    directory = tmp_path / "reference"
    shutil.copytree(EXAMPLE, directory)
    monkeypatch.setenv("REFERENCE_KUBECONFIG", KUBECONFIG)
    monkeypatch.setenv("REFERENCE_CONTEXT", CONTEXT)
    monkeypatch.setenv("REFERENCE_NAMESPACE", namespace)
    monkeypatch.setenv("REFERENCE_STATE_DIR", "state")
    monkeypatch.setenv(
        "SOPS_AGE_KEY_FILE", str(directory / "secrets" / "example-only.agekey")
    )
    source = (directory / "app.py").read_text()
    before = 'autoscalers={"web": Scaling(min_replicas=1, max_replicas=2)}'
    assert before in source
    changed_module = directory / "app_v2.py"  # modules are cached per path
    changed_module.write_text(
        source.replace(before, before.replace("max_replicas=2", "max_replicas=3"))
    )
    module = directory / "app.py"

    # 1. Plan, then deploy the approved hash; the checks run against the cluster.
    code, planned, result = _deploy(module, "--plan")
    assert code == 0, result.output
    assert planned["state"] == "planned" and planned["environment"]["name"] == "dev"
    code, done, result = _deploy(module, "--approve", planned["combined_hash"])
    assert code == 0, result.output
    assert done["state"] == "ready", done
    events = [json.loads(line) for line in result.stdout.splitlines() if line]
    (checked,) = [
        event["detail"]
        for event in events
        if event.get("stage") == "checks" and event.get("state") == "done"
    ]
    assert checked["passed"] is True, checked
    assert sorted(item["name"] for item in checked["results"]) == [
        "exec-statefulset-store-redis-cli",
        "http-service-web",
    ], checked

    # 2. Status and readiness.
    status = CliRunner().invoke(cli, ["status", f"{module}:dev", "--json"])
    assert status.exit_code == 0, status.output
    report = json.loads(status.stdout)
    assert report["schema"] == "piceli.status.v1"
    apps, batch, core = AppsV1Api(client), BatchV1Api(client), CoreV1Api(client)
    assert (
        apps.read_namespaced_stateful_set("store", namespace).status.ready_replicas == 1
    )
    assert batch.read_namespaced_job("migrate", namespace).status.succeeded == 1
    assert apps.read_namespaced_deployment("api", namespace).status.ready_replicas == 1
    assert apps.read_namespaced_deployment("web", namespace).status.ready_replicas >= 1
    claim = core.read_namespaced_persistent_volume_claim("data-store-0", namespace)
    assert claim.status.phase == "Bound"
    certificate = CustomObjectsApi(client).get_namespaced_custom_object(
        "cert-manager.io", "v1", namespace, "certificates", "site-tls"
    )
    assert certificate["spec"]["dnsNames"] == ["dev.reference.example.com"]
    route = CustomObjectsApi(client).get_namespaced_custom_object(
        "gateway.networking.k8s.io", "v1", namespace, "httproutes", "site"
    )
    assert route["spec"]["hostnames"] == ["dev.reference.example.com"]
    secret = core.read_namespaced_secret("store-credentials-1", namespace)
    assert set(secret.data) == {"password", "url", "redis.conf"}
    assert _hpa(client, namespace) == (1, 2)
    code, again, result = _release(module, "plan")
    assert code == 0, result.output
    assert set(again["summary"]) == {"no-op"}, again["summary"]
    versions = _versions(namespace)

    # 3. One value changed: exactly one object is updated.
    code, planned, result = _release(changed_module, "plan")
    assert code == 0, result.output
    updates = [
        f"{a['kind']}/{a['name']}"
        for a in planned["actions"]
        if a["operation"] != "no-op"
    ]
    assert updates == ["HorizontalPodAutoscaler/web"], planned["actions"]
    code, done, result = _deploy(changed_module, "--auto-approve")
    assert code == 0, result.output
    assert done["state"] == "ready", done
    assert _hpa(client, namespace) == (1, 3)
    after = _versions(namespace)
    assert set(after) == set(versions)
    changed = sorted(key for key in versions if versions[key] != after[key])
    assert changed == ["HorizontalPodAutoscaler/web"], changed

    # 4. Roll back to the first release.
    code, planned, result = _release(changed_module, "rollback", "previous")
    assert code == 3, result.output
    assert planned["intent"] == "rollback"
    code, outcome, result = _release(
        changed_module, "rollback", "previous", "--approve", planned["plan_hash"]
    )
    assert code == 0, result.output
    assert outcome["state"] == "succeeded", outcome
    assert _hpa(client, namespace) == (1, 2)
    code, final, result = _release(module, "plan")
    assert code == 0, result.output
    assert set(final["summary"]) == {"no-op"}, final["summary"]
