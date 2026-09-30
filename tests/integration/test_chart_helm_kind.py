"""Opt-in: ``helm install`` of a chart ``piceli chart publish`` pushed (disposable kind).

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-chart --kubeconfig /tmp/chart.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/chart.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-chart \\
      uv run pytest tests/integration/test_chart_helm_kind.py

It needs ``docker``, ``kubectl`` and ``helm`` on ``PATH`` and network access
for the pinned public images. The test never reads the ambient kubeconfig.

1. A throwaway registry container listens on a loopback port.
2. ``piceli chart publish`` prints the digest (exit 3), then pushes the
   chart with ``--approve``.
3. The client's side: a Secret created by hand, a values file (storage
   class, replicas, the Secret's name, a config value) and ``helm install``
   from ``oci://`` with only that values file. Every workload becomes Ready
   and the objects carry the values.

Everything it created is removed afterwards (release, namespace, registry).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
import time
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from piceli.k8s.templates.deployable.node_local_registry import DEFAULT_REGISTRY_IMAGE

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
NGINX = "nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
BUSYBOX = (
    "docker.io/library/busybox:1.37.0"
    "@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
)
APP = f"""
from piceli import App, ClaimTemplate, Random, Resources, Secrets

app = App("shop")
secrets = Secrets(password=Random(24))
credentials = app.secret("credentials", {{"password": secrets.ref("password")}})
settings = app.config("settings", {{"greeting": "hello"}})
web = app.deployment(
    "web", image="{NGINX}", ports=[80], replicas=1,
    env={{"GREETING": settings.key("greeting")}},
    ready=app.probe.http("/", 80),
    resources=Resources(cpu="10m", memory="32Mi"),
)
app.service(web, port=80)
app.stateful_set(
    "store", image="{BUSYBOX}", command=["sh", "-c", "sleep 3600"],
    env={{"PASSWORD": credentials.key("password")}},
    volumes={{"/data": ClaimTemplate("data", size="16Mi", storage_class="fast-ssd")}},
    resources=Resources(cpu="10m", memory="16Mi"),
)
"""

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (
            KUBECONFIG
            and CONTEXT
            and shutil.which("docker")
            and shutil.which("kubectl")
            and shutil.which("helm")
        ),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT; needs docker, "
        "kubectl and helm",
    ),
]


def _env() -> dict[str, str]:
    return {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}


def _docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=True, timeout=300
    ).stdout.strip()


def kubectl(*args: str, stdin: str = "", check: bool = True) -> str:
    result = subprocess.run(
        ["kubectl", "--kubeconfig", KUBECONFIG, "--context", CONTEXT, *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=600,
        env=_env(),
    )
    if check and result.returncode:
        raise AssertionError(f"kubectl {args[:2]} failed: {result.stderr[-2000:]}")
    return result.stdout


def helm(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["helm", "--kubeconfig", KUBECONFIG, "--kube-context", CONTEXT, *args],
        capture_output=True,
        text=True,
        timeout=600,
        env=_env(),
    )
    if check and result.returncode:
        raise AssertionError(f"helm {args[:1]} failed: {result.stderr[-2000:]}")
    return result.stdout


def _wait(probe: Any, seconds: float, message: str) -> Any:
    end = time.monotonic() + seconds
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting: {message}")
        time.sleep(2)


def _v2(host: str) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}/v2/", timeout=5) as response:
            return bool(response.status == 200)
    except OSError:
        return False


@pytest.fixture(scope="module")
def registry() -> Iterator[str]:
    """``host:port`` of a throwaway registry on loopback."""
    name = "piceli-chart-registry-" + uuid.uuid4().hex[:8]
    _docker(
        "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::5000",
        DEFAULT_REGISTRY_IMAGE,
    )  # fmt: skip
    try:
        port = _docker("port", name, "5000/tcp").splitlines()[0].rsplit(":", 1)[1]
        _wait(lambda: _v2(f"127.0.0.1:{port}"), 60, "the registry container")
        yield f"127.0.0.1:{port}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _storage_class() -> str:
    classes = json.loads(kubectl("get", "storageclass", "-o", "json"))["items"]
    for item in classes:
        annotations = item["metadata"].get("annotations") or {}
        if annotations.get("storageclass.kubernetes.io/is-default-class") == "true":
            return str(item["metadata"]["name"])
    pytest.skip("the cluster has no default storage class")


def test_helm_installs_the_published_chart_with_a_values_file(
    tmp_path: Path, registry: str
) -> None:
    module = tmp_path / "shop.py"
    module.write_text(textwrap.dedent(APP))
    runner = CliRunner()
    base = [
        "chart", "publish", f"{module}:app", "--to", f"oci://{registry}/charts",
        "--version", "1.0.0",
    ]  # fmt: skip
    preview = runner.invoke(cli, base)
    assert preview.exit_code == 3, preview.output
    digest = json.loads(preview.stdout)["digest"]
    published = runner.invoke(cli, [*base, "--approve", digest])
    assert published.exit_code == 0, published.output
    assert json.loads(published.stdout)["tag"] == "1.0.0"

    # The client's side: only the published chart, a Secret and a values file.
    namespace = "chart-" + uuid.uuid4().hex[:8]
    release = "shop-" + namespace[-8:]
    storage_class = _storage_class()
    values = tmp_path / "client-values.yaml"
    values.write_text(
        yaml.safe_dump(
            {
                "secrets": {"credentials": {"name": "client-credentials"}},
                "config": {"settings": {"greeting": "hola"}},
                "workloads": {
                    "web": {"replicas": 2},
                    "store": {"storage": {"data": {"storageClassName": storage_class}}},
                },
            }
        )
    )
    kubectl("create", "namespace", namespace)
    try:
        kubectl(
            "-n", namespace, "create", "secret", "generic", "client-credentials",
            "--from-literal=password=not-a-real-password",
        )  # fmt: skip
        helm(
            "install", release, f"oci://{registry}/charts/shop", "--version", "1.0.0",
            "--plain-http", "--namespace", namespace, "--values", str(values),
            "--wait", "--timeout", "600s",
        )  # fmt: skip
        web = json.loads(
            kubectl("-n", namespace, "get", "deployment", "web", "-o", "json")
        )
        assert web["status"].get("readyReplicas") == 2
        assert web["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "Helm"
        store = json.loads(
            kubectl("-n", namespace, "get", "statefulset", "store", "-o", "json")
        )
        assert store["status"].get("readyReplicas") == 1
        pod = store["spec"]["template"]["spec"]
        ref = pod["containers"][0]["env"][0]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "client-credentials"
        claim = json.loads(
            kubectl("-n", namespace, "get", "pvc", "data-store-0", "-o", "json")
        )
        assert claim["spec"]["storageClassName"] == storage_class
        assert claim["status"]["phase"] == "Bound"
        config = json.loads(
            kubectl("-n", namespace, "get", "configmap", "settings", "-o", "json")
        )
        assert config["data"] == {"greeting": "hola"}
        secrets = kubectl("-n", namespace, "get", "secrets", "-o", "name")
        assert "secret/credentials" not in secrets.split()  # never rendered
    finally:
        helm("uninstall", release, "--namespace", namespace, "--wait", check=False)
        kubectl("delete", "namespace", namespace, "--wait=false", check=False)
