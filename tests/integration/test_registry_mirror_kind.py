"""Opt-in: mirrored images and registry takeover on a disposable kind cluster.

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-mirror --kubeconfig /tmp/mirror.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/mirror.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-mirror \\
    PICELI_KIND_NODE=piceli-mirror-control-plane \\
      uv run pytest tests/integration/test_registry_mirror_kind.py

It needs ``docker`` and ``kubectl`` on ``PATH`` and ``busybox:1.36`` plus the
registry image (``DEFAULT_REGISTRY_IMAGE``) in the local Docker engine. A
throwaway registry container on a free loopback port of this machine plays
the "public" registry: the kind node cannot reach it, so a pod that runs
proves it pulled from the node-loopback registry. The test never reads the
ambient kubeconfig.

1. A mirror-only pipeline copies ``busybox`` by digest into the node registry
   and the pod runs from ``127.0.0.1:<port>/mirror/…@<same digest>``; an
   unchanged rerun copies nothing.
2. A registry created with the Kubernetes API (hostNetwork, hostPath) holds
   an image; the pipeline refuses to start a second registry on its port,
   then adopts it with ``adopt=`` and a pod pulls the image pushed before the
   takeover (the data survived).
3. The same with ``replace=``: the Deployment is recreated, the host
   directory and its images stay.

Pods use ``imagePullPolicy: Always``, so a running pod proves the node
registry served the image.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig
from piceli.k8s.templates.deployable.node_local_registry import DEFAULT_REGISTRY_IMAGE

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
NODE = os.environ.get("PICELI_KIND_NODE", "")
SOURCE_IMAGE = "busybox:1.36"
ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(1200),
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT and NODE and shutil.which("docker")),
        reason="set PICELI_KIND_KUBECONFIG, PICELI_KIND_CONTEXT and PICELI_KIND_NODE",
    ),
]


def _docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=True, timeout=300
    ).stdout.strip()


@pytest.fixture(scope="module")
def source() -> tuple[str, str]:
    """A local registry holding busybox: ``(host:port, manifest digest)``."""
    from piceli.pipeline.backend import free_port

    name = "piceli-mirror-source-" + uuid.uuid4().hex[:8]
    # An explicit host port: Docker Desktop's engine reaches it for the push.
    host = f"127.0.0.1:{free_port()}"
    _docker("run", "-d", "--name", name, "-p", f"{host}:5000", DEFAULT_REGISTRY_IMAGE)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://{host}/v2/", timeout=2)
                break
            except OSError:
                time.sleep(0.2)
        _docker("tag", SOURCE_IMAGE, f"{host}/library/busybox:1.36")
        _docker("push", f"{host}/library/busybox:1.36")
        request = urllib.request.Request(
            f"http://{host}/v2/library/busybox/manifests/1.36",
            method="HEAD",
            headers={"Accept": ACCEPT},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            digest = response.headers["Docker-Content-Digest"]
        yield host, digest
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
        subprocess.run(  # only the tag; the image stays
            ["docker", "rmi", f"{host}/library/busybox:1.36"],
            capture_output=True,
            check=False,
        )


@pytest.fixture
def namespace():
    from kubernetes.client import CoreV1Api

    name = "mirror-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    api = CoreV1Api(client)
    api.create_namespace({"metadata": {"name": name}})
    try:
        yield name, client
    finally:
        api.delete_namespace(name)
        client.close()


def _module(
    tmp_path: Path, namespace: str, *, app: str, image: str, deliver: str
) -> Path:
    module = tmp_path / f"{app}_{uuid.uuid4().hex[:6]}.py"
    module.write_text(
        textwrap.dedent(
            f"""
            from piceli import App, NodeLoopbackRegistry, Pipeline, Target

            target = Target.kubeconfig(
                {KUBECONFIG!r}, context={CONTEXT!r}, namespace={namespace!r},
                nodes={{"primary": {NODE!r}}},
            )
            app = App({app!r})
            # Always: the node resolves the image from its registry on every
            # start, even when another test left the same digest in containerd.
            app.deployment(
                "cache", image={image!r}, command=["sleep", "3600"],
                pull_policy="Always",
            )
            pipeline = Pipeline(
                app, target, deliver={deliver},
                state_dir={str(tmp_path / (app + "-state"))!r},
                execution={{"readiness_seconds": 180, "max_seconds": 600}},
            )
            """
        )
    )
    return module


def _deploy(module: Path, *args: str) -> tuple[int, list[dict], str]:
    env = dict(os.environ)
    env.pop("KUBECONFIG", None)
    result = subprocess.run(
        [sys.executable, "-m", "piceli", "deploy", f"{module}:pipeline", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=900,
    )
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return result.returncode, events, result.stderr


def _running_pod(client, namespace: str, app: str = "cache") -> dict:
    from kubernetes.client import CoreV1Api

    api = CoreV1Api(client)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        response = api.list_namespaced_pod(namespace, _preload_content=False)
        for pod in json.loads(response.data)["items"]:
            if not pod["metadata"]["name"].startswith(app + "-"):
                continue
            statuses = pod.get("status", {}).get("containerStatuses") or []
            if statuses and statuses[0].get("state", {}).get("running"):
                return pod
        time.sleep(2)
    raise AssertionError(f"no running {app} pod in {namespace}")


def test_mirrored_image_runs_from_the_node_registry(
    source, namespace, tmp_path
) -> None:
    host, digest = source
    name, client = namespace
    reference = f"{host}/library/busybox:1.36@{digest}"
    module = _module(
        tmp_path,
        name,
        app="mirrored",
        image=reference,
        deliver=f"NodeLoopbackRegistry(port=5020, storage='1Gi', mirror=[{reference!r}])",
    )
    code, events, stderr = _deploy(module, "--plan", "--json")
    assert code == 0, stderr
    planned = events[-1]
    (mirror,) = planned["stages"]["deliver"]["mirrors"].values()
    assert mirror["action"] == "mirror" and mirror["used"] is True
    assert mirror["platform"].startswith("linux/")

    code, events, stderr = _deploy(
        module, "--approve", planned["combined_hash"], "--json"
    )
    assert code == 0, stderr
    assert events[-1]["state"] == "ready", events[-1]
    copy = f"127.0.0.1:5020/mirror/{host.replace(':', '-')}/library/busybox@{digest}"
    pod = _running_pod(client, name)
    assert pod["spec"]["containers"][0]["image"] == copy
    assert pod["spec"]["nodeSelector"] == {"kubernetes.io/hostname": NODE}
    # The node pulled the copy (pull policy Always; it cannot reach the source
    # registry on this machine).
    assert pod["spec"]["containers"][0]["imagePullPolicy"] == "Always"

    code, events, stderr = _deploy(module, "--auto-approve", "--json")
    assert code == 0, stderr
    assert events[-1]["stages"]["deliver"] == "skipped", events[-1]
    assert events[-1]["stages"]["apply"] == "skipped", events[-1]


def _old_registry(client, namespace: str, path: str, port: int = 5010) -> None:
    """A registry created without Piceli: hostNetwork, ``port``, hostPath data."""
    from kubernetes.client import AppsV1Api

    AppsV1Api(client).create_namespaced_deployment(
        namespace,
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "old-registry"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "old-registry"}},
                "template": {
                    "metadata": {"labels": {"app": "old-registry"}},
                    "spec": {
                        "hostNetwork": True,
                        "nodeSelector": {"kubernetes.io/hostname": NODE},
                        "containers": [
                            {
                                "name": "registry",
                                "image": DEFAULT_REGISTRY_IMAGE,
                                "env": [
                                    {
                                        "name": "REGISTRY_HTTP_ADDR",
                                        "value": f"127.0.0.1:{port}",
                                    }
                                ],
                                "ports": [{"containerPort": port}],
                                "volumeMounts": [
                                    {"name": "data", "mountPath": "/var/lib/registry"}
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "data",
                                "hostPath": {"path": path, "type": "DirectoryOrCreate"},
                            }
                        ],
                    },
                },
            },
        },
    )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        status = AppsV1Api(client).read_namespaced_deployment("old-registry", namespace)
        if (status.status.ready_replicas or 0) >= 1:
            return
        time.sleep(2)
    raise AssertionError("the old registry did not become ready")


def _push_into_old_registry(
    namespace: str, host: str, digest: str, remote: int = 5010
) -> None:
    """Copy busybox into the old registry through a kubectl port-forward."""
    from piceli.artifacts.mirror import MirrorSource, mirror_image
    from piceli.artifacts.registry import RegistryTarget, StreamedOciRegistryClient
    from piceli.pipeline.backend import free_port

    port = free_port()
    forward = subprocess.Popen(
        [
            "kubectl",
            "--kubeconfig",
            KUBECONFIG,
            "--context",
            CONTEXT,
            "-n",
            namespace,
            "port-forward",
            "deployment/old-registry",
            f"{port}:{remote}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/v2/", timeout=2)
                break
            except OSError:
                time.sleep(0.2)
        parsed = MirrorSource.parse(f"{host}/library/busybox@{digest}")
        receipt = mirror_image(
            parsed,
            StreamedOciRegistryClient(
                RegistryTarget.parse(parsed.url).endpoint(), actions="pull"
            ),
            StreamedOciRegistryClient(
                RegistryTarget.parse(f"oci://127.0.0.1:{port}/keep/busybox").endpoint()
            ),
            "keep/busybox",
            platform=None,
            target_registry=f"127.0.0.1:{port}",
            node_registry=f"127.0.0.1:{remote}",
        )
        assert receipt["state"] == "succeeded", receipt
    finally:
        forward.terminate()
        forward.wait(timeout=30)


def test_pipeline_adopts_a_live_registry_and_its_images_survive(
    source, namespace, tmp_path
) -> None:
    from kubernetes.client import AppsV1Api

    host, digest = source
    name, client = namespace
    path = f"/var/lib/piceli-old-registry-{name}"
    _old_registry(client, name, path)
    _push_into_old_registry(name, host, digest)
    uid = (
        AppsV1Api(client).read_namespaced_deployment("old-registry", name).metadata.uid
    )
    # The app runs the image pushed before the takeover; the delivery also
    # mirrors one image, so the pipeline manages the registry.
    image = f"127.0.0.1:5010/keep/busybox@{digest}"
    mirror = f"{host}/library/busybox@{digest}"

    plain = _module(
        tmp_path,
        name,
        app="adopted",
        image=image,
        deliver=f"NodeLoopbackRegistry(port=5010, storage='1Gi', mirror=[{mirror!r}])",
    )
    code, events, stderr = _deploy(plain, "--plan", "--json")
    assert code == 2, stderr
    assert events[-1]["reason"] == "pipeline-registry-takeover-required", events[-1]

    adopting = _module(
        tmp_path,
        name,
        app="adopted",
        image=image,
        deliver=(
            "NodeLoopbackRegistry(port=5010, adopt='old-registry', "
            f"host_path={path!r}, mirror=[{mirror!r}])"
        ),
    )
    code, events, stderr = _deploy(adopting, "--plan", "--json")
    assert code == 0, stderr
    planned = events[-1]
    registry = planned["stages"]["deliver"]["registry"]
    assert registry["existing"]["action"] == "adopt", registry
    assert registry["existing"]["data"] == "kept"
    assert {"operation": "adopt", "kind": "Deployment", "name": "old-registry"} in (
        registry["changes"]
    )
    code, events, stderr = _deploy(
        adopting, "--approve", planned["combined_hash"], "--json"
    )
    assert code == 0, stderr
    assert events[-1]["state"] == "ready", events[-1]

    live = AppsV1Api(client).read_namespaced_deployment("old-registry", name)
    assert live.metadata.uid == uid  # adopted in place, never deleted
    assert live.metadata.annotations["piceli.io/owner"] == "adopted-registry"
    assert live.spec.selector.match_labels == {"app": "old-registry"}
    assert live.spec.template.spec.volumes[0].host_path.path == path
    # The image pushed before the takeover is still served by the node
    # registry: the pod (pull policy Always) runs it.
    pod = _running_pod(client, name)
    assert pod["spec"]["containers"][0]["image"] == image


def test_pipeline_replaces_a_live_registry_and_keeps_its_data(
    source, namespace, tmp_path
) -> None:
    from kubernetes.client import AppsV1Api

    host, digest = source
    name, client = namespace
    path = f"/var/lib/piceli-old-registry-{name}"
    _old_registry(client, name, path, port=5030)
    _push_into_old_registry(name, host, digest, remote=5030)
    uid = (
        AppsV1Api(client).read_namespaced_deployment("old-registry", name).metadata.uid
    )
    replacing = _module(
        tmp_path,
        name,
        app="replaced",
        image=f"127.0.0.1:5030/keep/busybox@{digest}",
        deliver=(
            "NodeLoopbackRegistry(port=5030, replace='old-registry', "
            f"host_path={path!r}, mirror=['{host}/library/busybox@{digest}'])"
        ),
    )
    code, events, stderr = _deploy(replacing, "--plan", "--json")
    assert code == 0, stderr
    planned = events[-1]
    registry = planned["stages"]["deliver"]["registry"]
    assert registry["existing"]["action"] == "replace", registry
    assert registry["existing"]["data"] == "kept"
    assert {
        "operation": "replace",
        "kind": "Deployment",
        "name": "old-registry",
    } in registry["changes"]
    code, events, stderr = _deploy(
        replacing, "--approve", planned["combined_hash"], "--json"
    )
    assert code == 0, stderr
    live = AppsV1Api(client).read_namespaced_deployment("old-registry", name)
    assert live.metadata.uid != uid  # recreated
    assert live.spec.selector.match_labels["app.kubernetes.io/instance"] == (
        "old-registry"
    )
    assert live.spec.strategy.type == "Recreate"
    pod = _running_pod(client, name)  # pulled (Always) from the new registry
    assert pod["spec"]["containers"][0]["image"].startswith("127.0.0.1:5030/keep/")
