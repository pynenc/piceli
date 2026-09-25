"""Opt-in: every typed App kind on a real cluster (kind).

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-kinds --kubeconfig /tmp/kinds.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/kinds.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-kinds \\
    PICELI_KIND_NODE=piceli-kinds-control-plane \\
      uv run pytest tests/integration/test_typed_kinds_kind.py

The test never reads the ambient kubeconfig. It installs the Gateway API CRDs
from a pinned release (``GATEWAY_API_CRDS``; HTTPRoute needs them, and
nothing else of the Gateway API), then in a uniquely named namespace releases
an app with a StatefulSet (claim templates, headless Service), a DaemonSet, a
Job the Deployment waits for, a CronJob, a HorizontalPodAutoscaler, a
PodDisruptionBudget, an Ingress and an HTTPRoute, with ``PodDefaults`` on
every pod, and checks:

* every object is created and the plan after it is a no-op;
* the HPA raises the Deployment to ``min_replicas`` and a later release that
  changes the Deployment keeps the HPA's count;
* a changed Job is refused (``immutable-field-changed``) and ``--replace
  Job/migrate`` runs the new one;
* a release without the StatefulSet and the networking objects prunes them,
  while the claims the StatefulSet created stay; a rollback recreates them
  and the StatefulSet's pods reattach to the same claims.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
import time
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
NODE = os.environ.get("PICELI_KIND_NODE", "")
KUBECTL = shutil.which("kubectl")
# nginx:1.27-alpine multi-arch index digest (runs `sleep` as a non-root user).
DIGEST = "sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
#: Gateway API standard channel CRDs, pinned to one release.
GATEWAY_API_CRDS = (
    "https://github.com/kubernetes-sigs/gateway-api/releases/download/"
    "v1.2.1/standard-install.yaml"
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(2400),
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT and NODE and KUBECTL),
        reason="set PICELI_KIND_KUBECONFIG, PICELI_KIND_CONTEXT and "
        "PICELI_KIND_NODE, and install kubectl",
    ),
]

COMPOSITION = """
from piceli import App, ClaimTemplate, PodDefaults, Resources, Route, Security

FULL = {full}
JOB_TAG = {job_tag!r}
API_MODE = {api_mode!r}


def build(ctx):
    shop = App("shop", pod_defaults=PodDefaults(
        security=Security.restricted(user=10001, fs_group=10001),
        node_selector={{"kubernetes.io/os": "linux"}},
        termination_grace_seconds=2,
        automount_token=False,
    ))
    image = ctx.image("app")
    sleep = ["sleep", "3600"]
    api = shop.deployment(
        "api", image=image, command=sleep, ports=[8080],
        env={{"MODE": API_MODE}}, resources=Resources(cpu="10m", memory="16Mi"),
    )
    web = shop.service(api, port=8080)
    migrate = shop.job(
        "migrate", image=image, command=["sh", "-c", "echo " + JOB_TAG],
        backoff_limit=1,
    )
    shop.depends(api, on=migrate)
    shop.autoscaler(api, min_replicas=2, max_replicas=3, cpu=80)
    if FULL:
        db = shop.stateful_set(
            "db", image=image, command=sleep, ports=[5432], replicas=2,
            pod_management="Parallel", node="primary",
            volumes={{"/var/lib/db": ClaimTemplate("data", size="16Mi")}},
        )
        shop.disruption_budget(db, max_unavailable=1)
        shop.daemon_set("agent", image=image, command=sleep)
        shop.cron_job("report", schedule="0 3 * * *", image=image,
                      command=["true"], suspend=True)
        shop.ingress("shop", hosts=["shop.example.com"], routes=[Route(web, "/")])
        shop.http_route("shop", gateway="public", hosts=["shop.example.com"],
                        routes=[Route(web, "/")])
    return shop
"""


def _kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # fixed argv, explicit kubeconfig and context
        [str(KUBECTL), "--kubeconfig", KUBECONFIG, "--context", CONTEXT, *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=check,
    )


@pytest.fixture(scope="module", autouse=True)
def gateway_api_crds():
    # Idempotent; retried because a busy API server may time out one request.
    for attempt in range(3):
        result = _kubectl("apply", "--server-side", "-f", GATEWAY_API_CRDS, check=False)
        if result.returncode == 0:
            break
        time.sleep(10 * (attempt + 1))
    else:
        raise AssertionError(f"Gateway API CRDs not installed: {result.stderr[-500:]}")
    _kubectl(
        "wait",
        "--for=condition=Established",
        "--timeout=120s",
        "crd/httproutes.gateway.networking.k8s.io",
    )


@pytest.fixture
def namespace():
    from kubernetes.client import CoreV1Api

    name = "kinds-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    core = CoreV1Api(client)
    core.create_namespace({"metadata": {"name": name}})
    try:
        yield name
    finally:
        core.delete_namespace(name)
        client.close()


def _spec(directory: Path, namespace: str, module: str) -> Path:
    # Composition modules are cached per path, so each version is a file.
    versions = {
        "full.py": {"full": True, "job_tag": "v1", "api_mode": "a"},
        "changed.py": {"full": True, "job_tag": "v1", "api_mode": "b"},
        "job_v2.py": {"full": True, "job_tag": "v2", "api_mode": "b"},
        "trimmed.py": {"full": False, "job_tag": "v2", "api_mode": "b"},
    }
    for name, values in versions.items():
        (directory / name).write_text(COMPOSITION.format(**values))
    spec = directory / "shop.toml"
    spec.write_text(
        textwrap.dedent(
            f"""
            [target]
            kubeconfig = "{KUBECONFIG}"
            context = "{CONTEXT}"
            namespace = "{namespace}"
            [target.nodes.primary]
            name = "{NODE}"
            [release]
            name = "shop"
            owner = "kinds-shop-e2e"
            field_manager = "kinds-shop-e2e"
            composition = "{module}:build"
            state_dir = "state"
            prune = true
            [execution]
            max_seconds = 1800
            readiness_seconds = 600
            [images]
            app = "docker.io/library/nginx@{DIGEST}"
            """
        )
    )
    return spec


def _run(spec: Path, *args: str) -> tuple[int, dict, str]:
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}"), result.output


def _operations(payload: dict) -> dict[str, str]:
    return {f"{a['kind']}/{a['name']}": a["operation"] for a in payload["actions"]}


def _approve(spec: Path, *command: str) -> dict:
    code, planned, output = _run(spec, *command)
    assert code in (0, 3), output
    execute = ("apply",) if command[0] == "plan" else command
    code, done, output = _run(spec, *execute, "--approve", planned["plan_hash"])
    assert code == 0, output
    assert done["execution"]["state"] == "ready", done
    return planned


def _get(namespace: str, kind: str, name: str) -> dict | None:
    result = _kubectl(
        "get", kind, name, "--namespace", namespace, "-o", "json", check=False
    )
    return json.loads(result.stdout) if result.returncode == 0 else None


def _claims(namespace: str) -> dict[str, str]:
    listed = json.loads(
        _kubectl("get", "pvc", "--namespace", namespace, "-o", "json").stdout
    )
    return {
        item["metadata"]["name"]: item["metadata"]["uid"] for item in listed["items"]
    }


def _wait_for(check, what: str, seconds: float = 180) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return
        time.sleep(2)
    raise AssertionError(f"timed out waiting for {what}")


def test_every_kind_on_kind(tmp_path, namespace):
    full = _spec(tmp_path, namespace, "full.py")
    planned = _approve(full, "plan")
    created = _operations(planned)
    assert set(created.values()) == {"create"}
    assert set(created) == {
        "Job/migrate",
        "Deployment/api",
        "Service/api",
        "HorizontalPodAutoscaler/api",
        "StatefulSet/db",
        "Service/db",
        "PodDisruptionBudget/db",
        "DaemonSet/agent",
        "CronJob/report",
        "Ingress/shop",
        "HTTPRoute/shop",
    }
    job = _get(namespace, "job", "migrate")
    assert job is not None and job["status"].get("succeeded") == 1
    sts = _get(namespace, "statefulset", "db")
    assert sts is not None and sts["status"].get("readyReplicas") == 2
    pod = sts["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsUser"] == 10001
    assert pod["nodeSelector"] == {
        "kubernetes.io/os": "linux",
        "kubernetes.io/hostname": NODE,
    }
    assert set(_claims(namespace)) == {"data-db-0", "data-db-1"}
    service = _get(namespace, "service", "db")
    assert service is not None and service["spec"]["clusterIP"] == "None"
    route = _get(namespace, "httproute", "shop")
    assert route is not None and route["spec"]["parentRefs"][0]["name"] == "public"

    # The HPA raises the Deployment to min_replicas (no metrics needed).
    def scaled() -> bool:
        deployment = _get(namespace, "deployment", "api")
        return bool(deployment) and deployment["spec"].get("replicas") == 2

    _wait_for(scaled, "the autoscaler to scale the Deployment to 2")

    # Unchanged: a no-op; the HPA's replica count is not a difference.
    code, again, output = _run(full, "plan")
    assert code == 0, output
    assert set(_operations(again).values()) == {"no-op"}, again.get("diffs")

    # A changed Deployment is applied without resetting the HPA's count.
    planned = _approve(_spec(tmp_path, namespace, "changed.py"), "plan")
    assert _operations(planned)["Deployment/api"] == "apply"
    deployment = _get(namespace, "deployment", "api")
    assert deployment is not None and deployment["spec"]["replicas"] == 2

    # A changed Job is refused until it is named for replacement.
    job_v2 = _spec(tmp_path, namespace, "job_v2.py")
    code, refused, output = _run(job_v2, "plan")
    assert code == 2, output
    assert refused["reason"] == "immutable-field-changed"
    assert refused["blocking"][0]["suggest"] == ["--replace Job/migrate"]
    old_uid = job["metadata"]["uid"]
    planned = _approve(job_v2, "plan", "--replace", "Job/migrate")
    assert _operations(planned)["Job/migrate"] == "replace"
    job = _get(namespace, "job", "migrate")
    assert job is not None and job["metadata"]["uid"] != old_uid
    assert job["status"].get("succeeded") == 1

    # Pruning deletes the kinds; the StatefulSet's claims stay.
    claims = _claims(namespace)
    trimmed = _spec(tmp_path, namespace, "trimmed.py")
    planned = _approve(trimmed, "plan")
    deletes = {k for k, v in _operations(planned).items() if v == "delete"}
    assert deletes == {
        "StatefulSet/db",
        "Service/db",
        "PodDisruptionBudget/db",
        "DaemonSet/agent",
        "CronJob/report",
        "Ingress/shop",
        "HTTPRoute/shop",
    }
    for kind, name in (("statefulset", "db"), ("httproute", "shop")):
        _wait_for(
            lambda kind=kind, name=name: _get(namespace, kind, name) is None,
            f"{kind}/{name} gone",
        )
    assert _claims(namespace) == claims

    # A rollback recreates them; the pods reattach to the same claims.
    pending = _approve(trimmed, "rollback", "previous")
    assert {k for k, v in _operations(pending).items() if v == "create"} == deletes
    sts = _get(namespace, "statefulset", "db")
    assert sts is not None and sts["status"].get("readyReplicas") == 2
    assert _claims(namespace) == claims
