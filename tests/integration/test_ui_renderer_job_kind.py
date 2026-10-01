"""Run the real isolated renderer Job on a disposable, explicit kind target.

Invoke through ``scripts/ui_kind.py``. The test builds one disposable local
image, loads it into that scratch node, and removes its host image/container.
kind's default CNI does not establish NetworkPolicy egress enforcement, so
this gate verifies the admitted Pod shape and Job lifecycle only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from piceli.k8s.ops.provider_factory import (
    KubeconfigTarget,
    api_client_from_kubeconfig,
    read_cluster_identity,
)
from piceli.services.cluster_evaluation import (
    KubernetesJobEvaluator,
    KubernetesRendererConfig,
)
from piceli.services.evaluation import RenderInputs, SourceSelection
from tests.integration.kind_support import (
    CONTEXT,
    KUBECONFIG,
    kubectl,
    node_platform,
    requires_kind,
    requires_ui_kind,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(900),
    requires_kind,
    requires_ui_kind,
]

_DOCKER = "unix:///var/run/docker.sock"


def _docker(*arguments: str, check: bool = True, timeout: int = 300) -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise AssertionError("disposable renderer test requires docker")
    environment = {**os.environ, "DOCKER_HOST": _DOCKER}
    environment.pop("DOCKER_CONTEXT", None)
    completed = subprocess.run(
        [executable, "--host", _DOCKER, *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode:
        raise AssertionError(
            f"docker {arguments[:3]} failed: {completed.stderr[-2000:]}"
        )
    return completed.stdout.strip()


@contextmanager
def _loaded_renderer(tmp_path: Path) -> Iterator[str]:
    """Install Piceli in a throwaway container and import its image into kind."""
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "renderer-package"
    source.mkdir()
    shutil.copytree(
        root / "piceli",
        source / "piceli",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(root / name, source / name)
    identifier = uuid.uuid4().hex[:12]
    container = f"piceli-ui-renderer-build-{identifier}"
    repository = f"docker.io/piceli/ui-renderer-{identifier}"
    tagged = repository + ":kind"
    node = os.environ["PICELI_KIND_NODE"]
    kind = Path(KUBECONFIG).parent / "kind"
    assert kind.is_file() and node.startswith("piceli-ui-")
    try:
        _docker(
            "create",
            "--name",
            container,
            "--mount",
            f"type=bind,src={source},dst=/source,readonly",
            "--workdir",
            "/source",
            "python:3.12-slim",
            "python",
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            ".",
        )
        _docker("start", "-a", container, timeout=360)
        assert _docker("inspect", "--format", "{{.State.ExitCode}}", container) == "0"
        _docker("commit", container, tagged, timeout=120)
        environment = {**os.environ, "DOCKER_HOST": _DOCKER}
        environment.pop("DOCKER_CONTEXT", None)
        environment["KIND_EXPERIMENTAL_PROVIDER"] = "docker"
        loaded = subprocess.run(
            [
                str(kind),
                "load",
                "docker-image",
                tagged,
                "--name",
                CONTEXT.removeprefix("kind-"),
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert loaded.returncode == 0, loaded.stderr[-2000:]
        images = _docker("exec", node, "ctr", "-n", "k8s.io", "images", "ls")
        row = next(
            (
                line.split()
                for line in images.splitlines()
                if line.startswith(tagged + " ")
            ),
            None,
        )
        assert row is not None and row[2].startswith("sha256:")
        pinned = repository + "@" + row[2]
        _docker("exec", node, "ctr", "-n", "k8s.io", "images", "tag", tagged, pinned)
        yield pinned
    finally:
        _docker("rm", "--force", container, check=False)
        _docker("image", "rm", "--force", tagged, check=False)


def _items(kind: str, namespace: str, selector: str) -> list[dict]:
    return list(
        json.loads(
            kubectl("get", kind, "-l", selector, "-o", "json", namespace=namespace)
        )["items"]
    )


def test_real_renderer_job_is_token_free_and_cleans_owned_objects(
    tmp_path: Path,
) -> None:
    assert KUBECONFIG and CONTEXT and os.environ.get("PICELI_KIND_NODE")
    namespace = "piceli-render-" + uuid.uuid4().hex[:8]
    kubectl("create", "namespace", namespace)
    try:
        kubectl(
            "apply",
            "-f",
            "-",
            namespace=namespace,
            stdin=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "List",
                    "items": [
                        {
                            "apiVersion": "v1",
                            "kind": "ServiceAccount",
                            "metadata": {
                                "name": "piceli-renderer",
                                "namespace": namespace,
                            },
                            "automountServiceAccountToken": False,
                        },
                        {
                            "apiVersion": "networking.k8s.io/v1",
                            "kind": "NetworkPolicy",
                            "metadata": {
                                "name": "piceli-renderer-deny-egress",
                                "namespace": namespace,
                            },
                            "spec": {
                                "podSelector": {
                                    "matchLabels": {
                                        "app.kubernetes.io/component": "renderer"
                                    }
                                },
                                "policyTypes": ["Ingress", "Egress"],
                                "ingress": [],
                                "egress": [],
                            },
                        },
                    ],
                }
            ),
        )
        initial = KubeconfigTarget(Path(KUBECONFIG), CONTEXT, namespace)
        api = api_client_from_kubeconfig(initial.kubeconfig, initial.context)
        try:
            identity = read_cluster_identity(api, initial)
        finally:
            api.close()
        target = replace(
            initial,
            cluster_uid=identity.cluster_uid,
            namespace_uid=identity.namespace_uid,
        )
        source = tmp_path / "source"
        source.mkdir()
        (source / "composition.py").write_text(
            "from piceli.k8s.ops.plan import DeploymentComponent, "
            "DeploymentComposition, ResourceIntent\n"
            "import time\n"
            "def build(context):\n"
            "    time.sleep(15)\n"
            "    manifest = {'apiVersion': 'v1', 'kind': 'ConfigMap', "
            "'metadata': {'name': 'render-proof', 'namespace': context.namespace}, "
            "'data': {'mode': 'kind'}}\n"
            "    return DeploymentComposition((DeploymentComponent('settings', "
            "(ResourceIntent.from_manifest(manifest),)),))\n"
        )
        with _loaded_renderer(tmp_path) as image:
            evaluator = KubernetesJobEvaluator(
                tmp_path / "evaluation-store",
                KubernetesRendererConfig(image, node_platform(), target),
            )
            observed_log: list[str] = []
            cleanup = evaluator._cleanup

            def capture_then_cleanup(api: object, preview_id: str) -> None:
                try:
                    from kubernetes import client

                    core = client.CoreV1Api(api)
                    pods = core.list_namespaced_pod(
                        namespace, label_selector="piceli.evaluation=" + preview_id
                    ).items
                    for pod in pods:
                        output = core.read_namespaced_pod_log(
                            pod.metadata.name, namespace, container="renderer"
                        )
                        observed_log.append(f"{type(output).__name__}: {output!r}")
                finally:
                    cleanup(api, preview_id)

            evaluator._cleanup = capture_then_cleanup  # type: ignore[method-assign]
            assert evaluator.available()
            preview = evaluator.preview(
                "kind-test",
                SourceSelection(source, ("composition.py",), "composition.py:build"),
                RenderInputs(namespace, {}, {}, (), {}),
            )
            selector = "piceli.evaluation=" + preview.id
            with ThreadPoolExecutor(max_workers=1) as pool:
                completed = pool.submit(evaluator.render, preview.id, preview.digest)
                deadline = time.monotonic() + 45
                running = None
                while time.monotonic() < deadline and not completed.done():
                    pods = _items("pods", namespace, selector)
                    running = next(
                        (
                            pod
                            for pod in pods
                            if pod["status"].get("phase") == "Running"
                        ),
                        None,
                    )
                    if running is not None:
                        break
                    time.sleep(0.5)
                assert running is not None, "renderer Pod never reached Running"
                pod_spec = running["spec"]
                assert pod_spec["serviceAccountName"] == "piceli-renderer"
                assert pod_spec["automountServiceAccountToken"] is False
                assert all(
                    "projected" not in volume and "secret" not in volume
                    for volume in pod_spec["volumes"]
                )
                mounts = pod_spec["containers"][0]["volumeMounts"]
                assert {mount["name"] for mount in mounts} == {
                    "source",
                    "request",
                    "scratch",
                }
                assert all(
                    not mount["mountPath"].startswith("/var/run/secrets")
                    for mount in mounts
                )
                try:
                    rendered = completed.result(timeout=90)
                except Exception as error:
                    raise AssertionError(
                        f"renderer failed: {error}; pod logs: {observed_log!r}"
                    ) from error
            assert rendered.components[0]["resources"][0]["manifest"]["data"] == {
                "mode": "kind"
            }
            assert not _items("jobs", namespace, selector)
            assert not _items("configmaps", namespace, selector)
            assert not _items("pods", namespace, selector)
            assert not (evaluator.store / preview.id / "source").exists()
    finally:
        kubectl(
            "delete",
            "namespace",
            namespace,
            "--wait=true",
            "--timeout=120s",
            check=False,
        )
