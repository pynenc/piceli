"""Boot the rendered UI installation and recover its PVC on disposable kind.

This is a runtime smoke test. It does not establish an authenticated deploy,
Ingress routing, CNI enforcement, or state backup/restore acceptance.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import ssl
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from piceli.server.cluster_install import cluster_install_yaml
from tests.integration.kind_support import (
    CONTEXT,
    KUBECONFIG,
    kubectl,
    requires_kind,
    requires_ui_kind,
    wait_for,
)
from tests.unit.server.test_cluster_install import _config

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(900),
    requires_kind,
    requires_ui_kind,
]
_DOCKER = "unix:///var/run/docker.sock"
_GATEWAY_TAG = "caddy:2.10.2"


def _docker(*arguments: str, check: bool = True, timeout: int = 300) -> str:
    executable = shutil.which("docker")
    assert executable is not None
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
            f"docker {arguments[:3]} failed: {completed.stderr[-1500:]}"
        )
    return completed.stdout.strip()


def _load_pinned(tag: str, repository: str) -> str:
    """Load a disposable host image, then address its exact node digest."""
    node = os.environ["PICELI_KIND_NODE"]
    kind = Path(KUBECONFIG).parent / "kind"
    assert kind.is_file() and node.startswith("piceli-ui-")
    environment = {**os.environ, "DOCKER_HOST": _DOCKER}
    environment.pop("DOCKER_CONTEXT", None)
    environment["KIND_EXPERIMENTAL_PROVIDER"] = "docker"
    loaded = subprocess.run(
        [
            str(kind),
            "load",
            "docker-image",
            tag,
            "--name",
            CONTEXT.removeprefix("kind-"),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert loaded.returncode == 0, loaded.stderr[-1500:]
    listing = _docker("exec", node, "ctr", "-n", "k8s.io", "images", "ls")
    normalized_tag = repository + ":" + tag.rsplit(":", 1)[-1]
    row = next(
        (
            line.split()
            for line in listing.splitlines()
            if line.startswith((tag + " ", normalized_tag + " "))
        ),
        None,
    )
    assert row is not None and row[2].startswith("sha256:")
    pinned = repository + "@" + row[2]
    _docker("exec", node, "ctr", "-n", "k8s.io", "images", "tag", row[0], pinned)
    return pinned


@contextmanager
def _images(tmp_path: Path) -> Iterator[tuple[str, str]]:
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "ui-package"
    source.mkdir()
    shutil.copytree(
        root / "piceli",
        source / "piceli",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(root / name, source / name)
    identifier = uuid.uuid4().hex[:12]
    container = f"piceli-ui-runtime-build-{identifier}"
    repository = f"docker.io/piceli/ui-runtime-{identifier}"
    tagged = repository + ":kind"
    gateway_existed = bool(
        _docker("image", "inspect", "--format", "{{.Id}}", _GATEWAY_TAG, check=False)
    )
    try:
        if not gateway_existed:
            _docker("pull", _GATEWAY_TAG)
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
            ".[ui]",
        )
        _docker("start", "-a", container, timeout=360)
        assert _docker("inspect", "--format", "{{.State.ExitCode}}", container) == "0"
        _docker("commit", container, tagged, timeout=120)
        ui = _load_pinned(tagged, repository)
        gateway = _load_pinned(_GATEWAY_TAG, "docker.io/library/caddy")
        yield ui, gateway
    finally:
        _docker("rm", "--force", container, check=False)
        _docker("image", "rm", "--force", tagged, check=False)
        if not gateway_existed:
            _docker("image", "rm", "--force", _GATEWAY_TAG, check=False)


def _tls_files(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "piceli.example.test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("piceli.example.test")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate = directory / "tls.crt"
    private_key = directory / "tls.key"
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private_key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    private_key.chmod(0o600)
    return certificate, private_key


def _ui_pod(namespace: str) -> dict:
    listing = json.loads(
        kubectl(
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=piceli-ui,app.kubernetes.io/component=ui",
            "-o",
            "json",
            namespace=namespace,
        )
    )
    active = [
        pod for pod in listing["items"] if not pod["metadata"].get("deletionTimestamp")
    ]
    assert len(active) <= 1, "the UI must stay single-replica"
    return active[0] if active else {}


def _tls_gateway_responds(namespace: str, pod: str, certificate: Path) -> None:
    """Probe the actual sidecar through an owned local port-forward."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        local_port = int(listener.getsockname()[1])
    process = subprocess.Popen(
        [
            "kubectl",
            "--kubeconfig",
            KUBECONFIG,
            "--context",
            CONTEXT,
            "--namespace",
            namespace,
            "port-forward",
            "--address",
            "127.0.0.1",
            f"pod/{pod}",
            f"{local_port}:8443",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/tmp"),
            "KUBECONFIG": KUBECONFIG,
        },
    )
    try:
        context = ssl.create_default_context(cafile=str(certificate))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            assert process.poll() is None, "scratch port-forward exited"
            try:
                with socket.create_connection(("127.0.0.1", local_port), 1) as raw:
                    with context.wrap_socket(
                        raw, server_hostname="piceli.example.test"
                    ) as secure:
                        secure.sendall(
                            b"GET /api/v1/applications HTTP/1.1\r\n"
                            b"Host: piceli.example.test\r\nConnection: close\r\n\r\n"
                        )
                        response = secure.recv(4096)
                        assert response.startswith(b"HTTP/1.1 403"), response[:100]
                        return
            except (OSError, ssl.SSLError):
                time.sleep(0.1)
        raise AssertionError("TLS gateway did not answer through port-forward")
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def test_installed_ui_pod_uses_private_pvc_and_survives_restart(tmp_path: Path) -> None:
    assert CONTEXT.startswith("kind-piceli-ui-")
    assert Path(KUBECONFIG).parent.name.startswith("piceli-ui-kind-")
    namespace = "piceli-runtime-" + uuid.uuid4().hex[:8]
    kubectl("create", "namespace", namespace)
    manifest = ""
    try:
        with _images(tmp_path) as (ui_image, gateway_image):
            cert, key = _tls_files(tmp_path)
            kubectl(
                "create",
                "secret",
                "tls",
                "piceli-ui-tls",
                "--cert",
                str(cert),
                "--key",
                str(key),
                namespace=namespace,
            )
            api_service = json.loads(
                kubectl(
                    "get", "service", "kubernetes", "-o", "json", namespace="default"
                )
            )
            address = ipaddress.ip_address(api_service["spec"]["clusterIP"])
            config = replace(
                _config(),
                namespace=namespace,
                ui_image=ui_image,
                gateway_image=gateway_image,
                ingress_namespace="kube-system",
                ingress_pod_labels={"app.kubernetes.io/name": "ingress-nginx"},
                api_egress_cidrs=(f"{address}/{address.max_prefixlen}",),
                storage_class="standard",
            )
            manifest = cluster_install_yaml(config)
            kubectl("apply", "-f", "-", stdin=manifest)
            kubectl(
                "rollout",
                "status",
                "deployment/piceli-ui",
                "--timeout=180s",
                namespace=namespace,
            )
            first = wait_for(
                lambda: (
                    pod
                    if (pod := _ui_pod(namespace)).get("status", {}).get("phase")
                    == "Running"
                    and len(pod["status"].get("containerStatuses", [])) == 2
                    and all(
                        item.get("ready")
                        for item in pod["status"].get("containerStatuses", [])
                    )
                    else None
                ),
                seconds=60,
                message="installed UI and gateway ready",
            )
            assert len(first["status"]["containerStatuses"]) == 2
            _tls_gateway_responds(namespace, first["metadata"]["name"], cert)
            original_uid = first["metadata"]["uid"]
            marker = "/var/lib/piceli/runtime-restart-proof"
            kubectl(
                "exec",
                first["metadata"]["name"],
                "-c",
                "ui",
                "--",
                "python",
                "-c",
                f"from pathlib import Path; Path({marker!r}).write_text('persisted')",
                namespace=namespace,
            )
            kubectl("delete", "pod", first["metadata"]["name"], namespace=namespace)
            second = wait_for(
                lambda: (
                    pod
                    if (pod := _ui_pod(namespace)).get("metadata", {}).get("uid")
                    not in {None, original_uid}
                    and pod.get("status", {}).get("phase") == "Running"
                    and len(pod["status"].get("containerStatuses", [])) == 2
                    and all(
                        item.get("ready")
                        for item in pod["status"].get("containerStatuses", [])
                    )
                    else None
                ),
                seconds=180,
                message="UI Pod replaced and ready",
            )
            assert len(second["status"]["containerStatuses"]) == 2
            observed = kubectl(
                "exec",
                second["metadata"]["name"],
                "-c",
                "ui",
                "--",
                "python",
                "-c",
                f"from pathlib import Path; print(Path({marker!r}).read_text())",
                namespace=namespace,
            )
            assert observed.strip() == "persisted"
    finally:
        if manifest:
            kubectl(
                "delete", "--ignore-not-found", "-f", "-", stdin=manifest, check=False
            )
        kubectl("delete", "namespace", namespace, "--wait=false", check=False)
