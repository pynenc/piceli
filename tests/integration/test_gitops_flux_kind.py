"""Opt-in: Flux reconciles an artifact ``piceli publish`` pushed (disposable kind).

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-flux --kubeconfig /tmp/flux.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/flux.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-flux \\
    PICELI_KIND_NODE=piceli-flux-control-plane \\
      uv run pytest tests/integration/test_gitops_flux_kind.py

It needs ``docker`` and ``kubectl`` on ``PATH`` and network access for the
pinned Flux release manifest (or ``PICELI_FLUX_INSTALL``, a local copy with
the same digest) and the controller images. The test never reads the ambient
kubeconfig.

1. A throwaway registry container joins the kind node's Docker network; this
   machine pushes to it on a loopback port, Flux pulls from its network
   address (``insecure: true``, plain HTTP inside the Docker network).
2. Only Flux's source-controller and kustomize-controller are installed from
   the pinned ``install.yaml``.
3. ``piceli publish`` prints the digest (exit 3), then pushes with
   ``--approve``; an ``OCIRepository`` on the tag and a ``Kustomization`` apply
   the objects, and the artifact revision names the published digest.
4. A second publish to the same tag changes a ConfigMap; Flux applies it.

Everything it created is removed afterwards (objects, Flux, the registry).
"""

from __future__ import annotations

import hashlib
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
NODE = os.environ.get("PICELI_KIND_NODE", "")
FLUX_VERSION = "v2.9.5"
FLUX_URL = (
    f"https://github.com/fluxcd/flux2/releases/download/{FLUX_VERSION}/install.yaml"
)
FLUX_SHA256 = "cc3dcd743af16215838b6937e1fce83745bf24c0dcc6c59737c59df15429caaf"
FLUX_CONTROLLERS = {"source-controller", "kustomize-controller"}
IMAGE = "nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(1200),
    pytest.mark.skipif(
        not (
            KUBECONFIG
            and CONTEXT
            and NODE
            and shutil.which("docker")
            and shutil.which("kubectl")
        ),
        reason="set PICELI_KIND_KUBECONFIG, PICELI_KIND_CONTEXT and PICELI_KIND_NODE; "
        "needs docker and kubectl",
    ),
]


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
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")},
    )
    if check and result.returncode:
        raise AssertionError(f"kubectl {args[:2]} failed: {result.stderr[-2000:]}")
    return result.stdout


def _get(kind: str, name: str, namespace: str) -> dict[str, Any] | None:
    out = kubectl("get", kind, name, "-n", namespace, "-o", "json", check=False)
    return json.loads(out) if out.strip() else None


def _ready(value: dict[str, Any] | None) -> bool:
    conditions = ((value or {}).get("status") or {}).get("conditions") or []
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)


def _wait(probe: Any, seconds: float, message: str) -> Any:
    end = time.monotonic() + seconds
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting: {message}")
        time.sleep(2)


def flux_manifests() -> str:
    """The pinned Flux install manifest with only the two controllers we need."""
    local = os.environ.get("PICELI_FLUX_INSTALL")
    if local:
        body = Path(local).read_bytes()
    else:
        with urllib.request.urlopen(FLUX_URL, timeout=120) as response:
            body = response.read()
    assert hashlib.sha256(body).hexdigest() == FLUX_SHA256, "Flux manifest changed"
    kept = []
    for document in yaml.safe_load_all(body):
        if not document:
            continue
        name = document.get("metadata", {}).get("name")
        if document.get("kind") == "Deployment" and name not in FLUX_CONTROLLERS:
            continue
        kept.append(document)
    return yaml.safe_dump_all(kept, sort_keys=False)


@pytest.fixture(scope="module")
def registry() -> Iterator[tuple[str, str]]:
    """``(push host:port on loopback, pull host:port on the kind network)``."""
    networks = json.loads(
        _docker("inspect", NODE, "--format", "{{json .NetworkSettings.Networks}}")
    )
    network = "kind" if "kind" in networks else sorted(networks)[0]
    name = "piceli-flux-registry-" + uuid.uuid4().hex[:8]
    _docker(
        "run", "-d", "--rm", "--name", name, "--network", network,
        "-p", "127.0.0.1::5000", DEFAULT_REGISTRY_IMAGE,
    )  # fmt: skip
    try:
        port = _docker("port", name, "5000/tcp").splitlines()[0].rsplit(":", 1)[1]
        address = _docker(
            "inspect", name, "--format",
            "{{(index .NetworkSettings.Networks \"" + network + "\").IPAddress}}",
        )  # fmt: skip
        _wait(lambda: _v2(f"127.0.0.1:{port}"), 60, "the registry container to answer")
        yield f"127.0.0.1:{port}", f"{address}:5000"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _v2(host: str) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}/v2/", timeout=5) as response:
            return bool(response.status == 200)
    except OSError:
        return False


@pytest.fixture(scope="module")
def flux() -> Iterator[None]:
    manifests = flux_manifests()
    kubectl("apply", "--server-side", "--force-conflicts", "-f", "-", stdin=manifests)
    try:
        for controller in sorted(FLUX_CONTROLLERS):
            kubectl(
                "-n", "flux-system", "wait", f"deployment/{controller}",
                "--for=condition=Available", "--timeout=420s",
            )  # fmt: skip
        yield
    finally:
        kubectl(
            "delete", "-f", "-", "--ignore-not-found", "--timeout=180s",
            stdin=manifests, check=False,
        )  # fmt: skip


def _app(path: Path, namespace: str, greeting: str) -> str:
    path.write_text(
        textwrap.dedent(
            f"""
            from piceli import App

            app = App("shop")
            web = app.deployment("web", image="{IMAGE}", ports=[80])
            app.service(web, port=80)
            app.config("settings", {{"greeting": "{greeting}"}})
            """
        )
    )
    return f"{path}:app"


def _publish(target: str, namespace: str, to: str) -> str:
    runner = CliRunner()
    base = ["publish", target, "--namespace", namespace, "--to", to]
    preview = runner.invoke(cli, base)
    assert preview.exit_code == 3, preview.output
    digest = json.loads(preview.stdout)["digest"]
    result = runner.invoke(cli, [*base, "--approve", digest])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["state"] == "published" and body["digest"] == digest
    return str(digest)


def test_flux_reconciles_a_published_artifact(
    tmp_path: Path, registry: tuple[str, str], flux: None
) -> None:
    push_host, pull_host = registry
    namespace = "flux-" + uuid.uuid4().hex[:8]
    source = "shop-" + namespace
    kubectl("create", "namespace", namespace)
    try:
        digest = _publish(
            _app(tmp_path / "v1.py", namespace, "hello"),
            namespace,
            f"oci://{push_host}/shop/app:v1",
        )
        kubectl(
            "apply", "-f", "-",
            stdin=yaml.safe_dump_all(
                [
                    {
                        "apiVersion": "source.toolkit.fluxcd.io/v1",
                        "kind": "OCIRepository",
                        "metadata": {"name": source, "namespace": "flux-system"},
                        "spec": {
                            "interval": "10s",
                            "url": f"oci://{pull_host}/shop/app",
                            "ref": {"tag": "v1"},
                            "insecure": True,
                        },
                    },
                    {
                        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
                        "kind": "Kustomization",
                        "metadata": {"name": source, "namespace": "flux-system"},
                        "spec": {
                            "interval": "10s",
                            "sourceRef": {"kind": "OCIRepository", "name": source},
                            "path": "./",
                            "prune": True,
                            "wait": False,
                        },
                    },
                ]
            ),
        )  # fmt: skip

        def applied(expected: str) -> Any:
            kustomization = _get("kustomization", source, "flux-system")
            status = (kustomization or {}).get("status") or {}
            if not _ready(kustomization):
                return None
            if not str(status.get("lastAppliedRevision", "")).endswith(expected):
                return None
            return status

        status = _wait(lambda: applied(digest), 420, "Flux to apply the artifact")
        assert status["lastAppliedRevision"] == f"v1@{digest}"
        repository = _get("ocirepository", source, "flux-system")
        assert repository is not None and _ready(repository)
        config = _get("configmap", "settings", namespace)
        assert config is not None and config["data"] == {"greeting": "hello"}
        deployment = _get("deployment", "web", namespace)
        assert deployment is not None
        assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE
        assert _get("service", "web", namespace) is not None
        labels = config["metadata"].get("labels") or {}
        assert labels.get("kustomize.toolkit.fluxcd.io/name") == source

        # A new publish to the same tag is picked up: Flux follows the tag.
        second = _publish(
            _app(tmp_path / "v2.py", namespace, "hello again"),
            namespace,
            f"oci://{push_host}/shop/app:v1",
        )
        assert second != digest
        _wait(lambda: applied(second), 300, "Flux to apply the second artifact")
        config = _get("configmap", "settings", namespace)
        assert config is not None and config["data"] == {"greeting": "hello again"}
    finally:
        for kind in ("kustomization", "ocirepository"):
            kubectl(
                "-n", "flux-system", "delete", kind, source,
                "--ignore-not-found", "--timeout=180s", check=False,
            )  # fmt: skip
        kubectl("delete", "namespace", namespace, "--wait=false", check=False)
