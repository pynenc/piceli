"""One installed UI on one disposable kind cluster, with explicit credentials.

Run only through ``scripts/ui_kind.py``. The helper owns and removes the
cluster; this test owns its namespace, images, issuer and port-forwards.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from piceli.k8s.cli.ui_remote import RemoteAccessClient
from piceli.server.cluster_install import (
    ClusterInstallConfig,
    DeployResourceRule,
    InstalledBuildConfig,
    ManualDeliveryConfig,
    cluster_install_yaml,
)
from tests.integration.kind_support import (
    CONTEXT,
    KUBECONFIG,
    kubectl,
    node_platform,
    requires_kind,
    requires_ui_kind,
    wait_for,
)
from tests.integration.test_ui_cluster_runtime_kind import (
    _docker,
    _images,
    _load_pinned,
    _tls_files,
    _ui_pod,
)
from tests.unit.host_build_support import HOST_TOML, publish_base
from tests.unit.server.test_cluster_install import _config
from tests.unit.test_registry_delivery import FakeRegistry

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(1500),
    requires_kind,
    requires_ui_kind,
]

_ISSUER = r"""
import base64, hashlib, json, os, ssl, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from joserfc import jwk, jwt

issuer = os.environ['ISSUER_URL']
key = jwk.RSAKey.generate_key(2048, parameters={'kid': 'e2e-key'})
flows = {}
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args): pass
    def send_json(self, value):
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == '/.well-known/openid-configuration':
            self.send_json({'issuer': issuer, 'authorization_endpoint': issuer+'/authorize', 'token_endpoint': issuer+'/token', 'jwks_uri': issuer+'/jwks', 'response_types_supported': ['code'], 'subject_types_supported': ['public'], 'id_token_signing_alg_values_supported': ['RS256'], 'code_challenge_methods_supported': ['S256']})
        elif parsed.path == '/jwks':
            self.send_json({'keys': [key.as_dict(private=False)]})
        elif parsed.path == '/authorize':
            values = {k: v[0] for k,v in parse_qs(parsed.query).items()}
            if values.get('client_id') != 'piceli-test' or values.get('code_challenge_method') != 'S256':
                self.send_error(400); return
            code = hashlib.sha256(values['state'].encode()).hexdigest()
            flows[code] = values
            self.send_response(302)
            self.send_header('Location', values['redirect_uri']+'?code='+code+'&state='+values['state'])
            self.end_headers()
        else: self.send_error(404)
    def do_POST(self):
        if self.path != '/token': self.send_error(404); return
        length = int(self.headers.get('Content-Length', '0'))
        if length > 4096: self.send_error(413); return
        values = {k: v[0] for k,v in parse_qs(self.rfile.read(length).decode()).items()}
        flow = flows.pop(values.get('code', ''), None)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(values.get('code_verifier','').encode()).digest()).rstrip(b'=').decode()
        if flow is None or values.get('grant_type') != 'authorization_code' or values.get('redirect_uri') != flow['redirect_uri'] or challenge != flow['code_challenge']:
            self.send_error(400); return
        now = int(time.time())
        token = jwt.encode({'alg': 'RS256', 'kid': 'e2e-key'}, {'iss': issuer, 'sub': 'operator-1', 'aud': 'piceli-test', 'iat': now, 'exp': now+300, 'nonce': flow['nonce']}, key)
        self.send_json({'access_token': 'fixture-token', 'token_type': 'Bearer', 'expires_in': 300, 'id_token': token})
server = ThreadingHTTPServer(('0.0.0.0', 8443), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain('/fixture/tls/tls.crt', '/fixture/tls/tls.key')
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
"""


def _cert(directory: Path, address: str) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, address)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=2))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(address))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert, private = directory / "issuer.crt", directory / "issuer.key"
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    private.chmod(0o600)
    return cert, private


@contextmanager
def _forward(namespace: str, service: str, remote: int) -> Iterator[int]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
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
            f"service/{service}",
            f"{port}:{remote}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PATH": os.environ["PATH"],
            "HOME": os.environ["HOME"],
            "KUBECONFIG": KUBECONFIG,
        },
    )
    try:
        wait_for(
            lambda: _reachable(port) or None, seconds=20, message=f"{service} forward"
        )
        yield port
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def _reachable(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), 0.5):
            return True
    except OSError:
        return False


def _issuer(namespace: str, image: str, directory: Path) -> tuple[str, Path, str]:
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "oidc-issuer", "namespace": namespace},
        "spec": {
            "selector": {"app": "oidc-issuer"},
            "ports": [{"port": 8443, "targetPort": 8443}],
        },
    }
    kubectl("apply", "-f", "-", stdin=json.dumps(service))
    address = json.loads(
        kubectl("get", "service", "oidc-issuer", "-o", "json", namespace=namespace)
    )["spec"]["clusterIP"]
    cert, key = _cert(directory, address)
    source = directory / "issuer.py"
    source.write_text(_ISSUER)
    kubectl(
        "create",
        "configmap",
        "oidc-issuer",
        "--from-file",
        str(source),
        namespace=namespace,
    )
    kubectl(
        "create",
        "secret",
        "tls",
        "oidc-issuer",
        "--cert",
        str(cert),
        "--key",
        str(key),
        namespace=namespace,
    )
    issuer_url = f"https://{address}:8443"
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "oidc-issuer",
            "namespace": namespace,
            "labels": {"app": "oidc-issuer"},
        },
        "spec": {
            "automountServiceAccountToken": False,
            "containers": [
                {
                    "name": "issuer",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["python", "-u", "/fixture/issuer.py"],
                    "env": [{"name": "ISSUER_URL", "value": issuer_url}],
                    "ports": [{"containerPort": 8443}],
                    "readinessProbe": {"tcpSocket": {"port": 8443}, "periodSeconds": 2},
                    "volumeMounts": [
                        {
                            "name": "source",
                            "mountPath": "/fixture/issuer.py",
                            "subPath": "issuer.py",
                        },
                        {"name": "tls", "mountPath": "/fixture/tls"},
                    ],
                }
            ],
            "volumes": [
                {"name": "source", "configMap": {"name": "oidc-issuer"}},
                {"name": "tls", "secret": {"secretName": "oidc-issuer"}},
            ],
        },
    }
    kubectl("apply", "-f", "-", stdin=json.dumps(pod))
    kubectl(
        "wait",
        "--for=condition=Ready",
        "pod/oidc-issuer",
        "--timeout=120s",
        namespace=namespace,
    )
    pod_ip = json.loads(
        kubectl("get", "pod", "oidc-issuer", "-o", "json", namespace=namespace)
    )["status"]["podIP"]
    return issuer_url, cert, pod_ip


@contextmanager
def _build_images(ui_image: str) -> Iterator[tuple[str, str]]:
    identifier = uuid.uuid4().hex[:10]
    container = f"piceli-ui-builder-install-{identifier}"
    repository = f"docker.io/piceli/ui-builder-{identifier}"
    tag = repository + ":kind"
    registry_preexisting = bool(_docker("image", "inspect", "registry:2", check=False))
    try:
        if not registry_preexisting:
            _docker("pull", "registry:2")
        _docker(
            "create",
            "--name",
            container,
            ui_image.split("@", 1)[0] + ":kind",
            "sh",
            "-c",
            "apt-get update -qq && apt-get install -y -qq --no-install-recommends git && rm -rf /var/lib/apt/lists/*",
        )
        _docker("start", "-a", container, timeout=360)
        assert _docker("inspect", "--format", "{{.State.ExitCode}}", container) == "0"
        _docker("commit", container, tag)
        yield (
            _load_pinned(tag, repository),
            _load_pinned("registry:2", "docker.io/library/registry"),
        )
    finally:
        _docker("rm", "--force", container, check=False)
        _docker("image", "rm", "--force", tag, check=False)
        if not registry_preexisting:
            _docker("image", "rm", "--force", "registry:2", check=False)


def _tcp_service(
    namespace: str,
    name: str,
    port: int,
    image: str,
    *,
    command: list[str] | None = None,
    volumes: list[dict] | None = None,
    mounts: list[dict] | None = None,
    host_network: bool = False,
    pod_labels: dict[str, str] | None = None,
) -> str:
    objects = [
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "selector": {"app": name},
                "ports": [{"port": port, "targetPort": port}],
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {"app": name, **(pod_labels or {})},
            },
            "spec": {
                "automountServiceAccountToken": False,
                **(
                    {"hostNetwork": True, "dnsPolicy": "ClusterFirstWithHostNet"}
                    if host_network
                    else {}
                ),
                "containers": [
                    {
                        "name": name,
                        "image": image,
                        "imagePullPolicy": "IfNotPresent",
                        **({"command": command} if command else {}),
                        "ports": [{"containerPort": port}],
                        "readinessProbe": {
                            "tcpSocket": {"port": port},
                            "periodSeconds": 2,
                        },
                        "volumeMounts": mounts or [],
                    }
                ],
                "volumes": volumes or [],
            },
        },
    ]
    kubectl("apply", "-f", "-", stdin=yaml.safe_dump_all(objects, sort_keys=False))
    kubectl(
        "wait",
        "--for=condition=Ready",
        f"pod/{name}",
        "--timeout=120s",
        namespace=namespace,
    )
    return json.loads(
        kubectl("get", "service", name, "-o", "json", namespace=namespace)
    )["spec"]["clusterIP"]


def _base_image(registry_port: int, platform: str) -> str:
    architecture = platform.split("/")[1]
    fake = FakeRegistry()
    try:
        base_digest = publish_base(fake, architectures=(architecture,))
        with httpx.Client(
            base_url=f"http://127.0.0.1:{registry_port}", timeout=20
        ) as client:
            for (repository, digest), body in fake.blobs.items():
                created = client.post(f"/v2/{repository}/blobs/uploads/")
                assert created.status_code == 202
                location = urlsplit(created.headers["Location"])
                location = location.path + (
                    ("?" + location.query) if location.query else ""
                )
                uploaded = client.put(
                    location + ("&" if "?" in location else "?") + "digest=" + digest,
                    content=body,
                )
                assert uploaded.status_code == 201
            for (repository, digest), (body, media_type) in fake.manifests.items():
                uploaded = client.put(
                    f"/v2/{repository}/manifests/{digest}",
                    content=body,
                    headers={"Content-Type": media_type},
                )
                assert uploaded.status_code == 201
    finally:
        fake.close()
    return base_digest


def _git_server(namespace: str, image: str, base_digest: str) -> str:
    project = HOST_TOML.replace("@PORT@", "5000").replace("@BASE@", base_digest)
    data = {
        "host-build.toml": project,
        "main.txt": "v1\n",
        "static.txt": "static 1\n",
    }
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "git-source", "namespace": namespace},
        "data": data,
    }
    kubectl("apply", "-f", "-", stdin=json.dumps(configmap))
    script = (
        "set -eu; mkdir -p /work/src/src; "
        "cp /project/host-build.toml /work/src/; "
        "cp /project/main.txt /project/static.txt /work/src/src/; "
        "git -C /work/src init -q; git -C /work/src add .; "
        "git -C /work/src -c user.name=Operator -c user.email=operator@example.test "
        "commit -qm initial; git clone -q --bare /work/src /work/shop.git; "
        "git daemon --reuseaddr --base-path=/work --export-all "
        "--listen=0.0.0.0 --port=9418 /work"
    )
    return _tcp_service(
        namespace,
        "git-source",
        9418,
        image,
        command=["sh", "-c", script],
        volumes=[
            {"name": "source", "configMap": {"name": "git-source"}},
            {"name": "work", "emptyDir": {}},
        ],
        mounts=[
            {"name": "source", "mountPath": "/project"},
            {"name": "work", "mountPath": "/work"},
        ],
    )


def _login(browser: httpx.Client, issuer_port: int) -> None:
    login = browser.get("/auth/login", follow_redirects=False)
    assert login.status_code == 302
    authorization = urlsplit(login.headers["location"])
    with httpx.Client(base_url=f"https://127.0.0.1:{issuer_port}", verify=False) as idp:
        approved = idp.get(
            authorization.path + "?" + authorization.query, follow_redirects=False
        )
    assert approved.status_code == 302
    callback = urlsplit(approved.headers["location"])
    result = browser.get(callback.path + "?" + callback.query, follow_redirects=False)
    assert result.status_code == 200


def _post(browser: httpx.Client, path: str, body: dict, *, expected: int = 202) -> dict:
    csrf = next(
        value
        for name, value in browser.cookies.items()
        if name.startswith("piceli_csrf_")
    )
    response = browser.post(
        "/api/v1" + path,
        json=body,
        headers={"Origin": "https://piceli.example.test", "X-Piceli-CSRF": csrf},
    )
    assert response.status_code == expected, (
        path,
        response.status_code,
        response.text[:300],
    )
    return response.json()


def _wait(browser: httpx.Client, path: str, *, seconds: int = 180) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        response = browser.get("/api/v1" + path)
        assert response.status_code == 200, response.status_code
        record = response.json()
        if record["state"] not in {"queued", "running", "cancelling"}:
            return record
        time.sleep(1)
    raise AssertionError(f"{path} did not finish")


def _source(
    namespace: str,
    api_address: str,
    node_address: str,
    image: str,
) -> tuple[str, str]:
    definition = f'''[target]
kubeconfig = "/var/lib/piceli/control/target.kubeconfig"
context = "piceli-incluster"
namespace = "{namespace}"

[release]
name = "installed-e2e"
owner = "installed-e2e"
field_manager = "installed-e2e"
composition = "composition.py:build"
state_dir = "/var/lib/piceli/control/release-state"

[execution]
max_seconds = 240
readiness_seconds = 180
poll_seconds = 1

[images]
installed = "{image}"
'''
    source = f"""import socket
from piceli import App

def build(context):
    for address, port in [('1.1.1.1', 443), ({api_address!r}, 443), ({node_address!r}, 6443)]:
        try:
            connection = socket.create_connection((address, port), 2)
        except OSError:
            pass
        else:
            connection.close()
            print('EGR=' + address)
            raise RuntimeError('renderer egress was allowed')
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    connection = socket.create_connection(listener.getsockname(), 2)
    accepted, _ = listener.accept()
    connection.close()
    accepted.close()
    listener.close()
    app = App('installed-proof')
    workload = app.deployment('installed-worker', image={image!r}, command=['sh', '-c', 'sleep 60; python -m http.server 8080'], ports=[8080])
    app.pre_rollout(workload, ['python', '-c', 'print(1)'])
    return app
"""
    return definition, source


def _remote_forward(browser: httpx.Client) -> None:
    def service() -> dict | None:
        response = browser.get("/api/v1/applications/cluster/resources")
        assert response.status_code == 200
        return next(
            (
                item
                for item in response.json()["items"]
                if item["identity"]["kind"] == "Service"
                and item["identity"]["name"] == "git-source"
                and 9418 in item["ports"]
            ),
            None,
        )

    resource = wait_for(service, seconds=60, message="observable Git service")
    issued = _post(
        browser,
        "/applications/cluster/remote-access",
        {
            "resource_id": resource["id"],
            "resource_uid": resource["identity"]["uid"],
            "remote_port": 9418,
            "duration_seconds": 90,
        },
        expected=201,
    )
    assert issued["session"]["state"] == "pending"
    ticket = issued["session"]["id"]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    stop = threading.Event()
    ready = threading.Event()
    outcome: dict[str, object] = {}
    kubectl_path = shutil.which("kubectl")
    assert kubectl_path is not None

    def run() -> None:
        with httpx.Client(
            base_url=str(browser.base_url),
            verify=False,
            headers={"Host": "piceli.example.test"},
            timeout=20,
        ) as transport:
            client = RemoteAccessClient(
                server=str(browser.base_url),
                kubeconfig=Path(KUBECONFIG),
                context=CONTEXT,
                local_port=port,
                kubectl=Path(kubectl_path),
                http_client=transport,
                poll_seconds=0.5,
            )
            try:
                outcome["session"] = client.run(
                    ticket,
                    issued["pairing_secret"],
                    stop=stop,
                    on_state=lambda session: (
                        ready.set() if session.state == "ready" else None
                    ),
                )
            except Exception as error:
                outcome["error"] = type(error).__name__
            finally:
                client.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        wait_for(lambda: ready.is_set() or None, seconds=60, message="local ready")
        remote = subprocess.run(
            ["git", "ls-remote", f"git://127.0.0.1:{port}/shop.git", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        assert len(remote.stdout.split()[0]) in {40, 64}
    finally:
        stop.set()
        thread.join(timeout=30)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome.get("error")
    ticket_state = browser.get(f"/api/v1/applications/cluster/remote-access/{ticket}")
    assert ticket_state.status_code == 200
    assert ticket_state.json()["state"] == "stopped"
    assert not _reachable(port)


def _backup_restore(namespace: str, image: str) -> None:
    current = _ui_pod(namespace)["metadata"]["name"]
    kubectl("scale", "deployment/piceli-ui", "--replicas=0", namespace=namespace)
    kubectl(
        "wait", "--for=delete", f"pod/{current}", "--timeout=120s", namespace=namespace
    )
    script = (
        "from pathlib import Path; import os,shutil,stat; "
        "from piceli.server.state_archive import backup,restore; "
        "root=Path('/var/lib/piceli'); "
        "backup(root/'control',root/'snapshot.tar.gz'); "
        "shutil.move(root/'control',root/'original-control'); "
        "restore(root/'snapshot.tar.gz',root/'control'); "
        "assert all(p.stat().st_uid==os.getuid() and "
        "stat.S_IMODE(p.stat().st_mode)==0o700 "
        "for p in (root/'control',root/'control'/'evaluations')); "
        "shutil.rmtree(root/'original-control'); "
        "(root/'snapshot.tar.gz').unlink()"
    )
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "ui-state-maintenance", "namespace": namespace},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "fsGroup": 10001,
                "fsGroupChangePolicy": "OnRootMismatch",
            },
            "containers": [
                {
                    "name": "maintenance",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["python", "-c", script],
                    "volumeMounts": [{"name": "state", "mountPath": "/var/lib/piceli"}],
                }
            ],
            "volumes": [
                {
                    "name": "state",
                    "persistentVolumeClaim": {"claimName": "piceli-ui-state"},
                }
            ],
        },
    }
    kubectl("apply", "-f", "-", stdin=json.dumps(pod))
    phase = wait_for(
        lambda: (
            value
            if (
                value := json.loads(
                    kubectl(
                        "get",
                        "pod",
                        "ui-state-maintenance",
                        "-o",
                        "json",
                        namespace=namespace,
                    )
                )["status"].get("phase")
            )
            in {"Succeeded", "Failed"}
            else None
        ),
        seconds=120,
        message="UI state backup and restore",
    )
    if phase != "Succeeded":
        logs = kubectl(
            "logs", "pod/ui-state-maintenance", namespace=namespace, check=False
        )
        errors = re.findall(r"\b[A-Za-z_]+(?:Error|Exception)\b", logs)
        raise AssertionError(f"UI state maintenance failed: {errors[-4:]}")
    kubectl("delete", "pod", "ui-state-maintenance", "--wait=true", namespace=namespace)
    kubectl("scale", "deployment/piceli-ui", "--replicas=1", namespace=namespace)
    try:
        kubectl(
            "rollout",
            "status",
            "deployment/piceli-ui",
            "--timeout=180s",
            namespace=namespace,
        )
    except AssertionError as error:
        pods = json.loads(kubectl("get", "pods", "-o", "json", namespace=namespace))[
            "items"
        ]
        states = [
            {
                "name": pod["metadata"]["name"],
                "phase": pod.get("status", {}).get("phase"),
                "containers": [
                    {
                        "name": item["name"],
                        "ready": item.get("ready"),
                        "waiting": item.get("state", {})
                        .get("waiting", {})
                        .get("reason"),
                        "previous_exit": item.get("lastState", {})
                        .get("terminated", {})
                        .get("exitCode"),
                    }
                    for item in pod.get("status", {}).get("containerStatuses", [])
                ],
            }
            for pod in pods
            if pod["metadata"]["name"].startswith("piceli-ui-")
        ]
        failed = next((item["name"] for item in states), "")
        logs = kubectl(
            "logs",
            f"pod/{failed}",
            "-c",
            "ui",
            "--previous",
            "--tail=80",
            namespace=namespace,
            check=False,
        )
        error_types = re.findall(
            r"Piceli cluster UI startup: [a-z]+ [A-Za-z_]+(?: [a-z-]+)?|\b[A-Za-z_]+(?:Error|Exception)\b",
            logs,
        )
        raise AssertionError(
            f"restored UI did not become ready: {states}; errors: {error_types[-8:]}"
        ) from error


def _revoke_stream(browser: httpx.Client, namespace: str, issuer: str) -> None:
    resources = browser.get("/api/v1/applications/cluster/resources")
    assert resources.status_code == 200
    service = next(
        item
        for item in resources.json()["items"]
        if item["identity"]["kind"] == "Service"
        and item["identity"]["name"] == "git-source"
    )
    access_request = {
        "resource_id": service["id"],
        "resource_uid": service["identity"]["uid"],
        "remote_port": 9418,
        "duration_seconds": 300,
    }
    pending = _post(
        browser, "/applications/cluster/remote-access", access_request, expected=201
    )
    started = threading.Event()
    ended = threading.Event()
    status: dict[str, int] = {}

    def receive() -> None:
        try:
            with httpx.Client(
                base_url=str(browser.base_url),
                verify=False,
                headers={"Host": "piceli.example.test"},
                cookies=browser.cookies,
                timeout=httpx.Timeout(130),
            ) as client:
                with client.stream(
                    "GET", "/api/v1/events?application_id=cluster"
                ) as response:
                    status["code"] = response.status_code
                    started.set()
                    try:
                        for _ in response.iter_lines():
                            pass
                    except httpx.RemoteProtocolError:
                        # The server closes the stream as soon as the scope
                        # disappears; the proxy may not send a final chunk.
                        pass
        finally:
            ended.set()

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()
    wait_for(lambda: started.is_set() or None, seconds=20, message="live event stream")
    assert status["code"] == 200
    identity = hashlib.sha256((issuer + "\0operator-1").encode()).hexdigest()
    restricted = {identity: ["inspect", "activity"]}
    kubectl(
        "patch",
        "configmap",
        "piceli-ui-grants",
        "--type=merge",
        "-p",
        json.dumps({"data": {"grants.json": json.dumps(restricted)}}),
        namespace=namespace,
    )
    wait_for(
        lambda: (
            browser.get(
                "/api/v1/applications/cluster/remote-access/" + pending["session"]["id"]
            ).status_code
            == 404
            or None
        ),
        seconds=120,
        message="local-client access grant revocation",
    )
    _post(
        browser,
        "/applications/cluster/remote-access",
        access_request,
        expected=404,
    )
    assert browser.get("/api/v1/applications/cluster").status_code == 200
    assert not ended.is_set()
    kubectl(
        "patch",
        "configmap",
        "piceli-ui-grants",
        "--type=merge",
        "-p",
        '{"data":{"grants.json":"{}"}}',
        namespace=namespace,
    )
    wait_for(
        lambda: browser.get("/api/v1/applications/cluster").status_code == 404 or None,
        seconds=120,
        message="projected grant revocation",
    )
    wait_for(lambda: ended.is_set() or None, seconds=30, message="revoked stream close")
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_installed_ui_end_to_end_in_one_kind_cluster(tmp_path: Path) -> None:
    assert CONTEXT.startswith("kind-piceli-ui-")
    namespace = "piceli-installed-" + uuid.uuid4().hex[:8]
    kubectl("create", "namespace", namespace)
    try:
        with (
            _images(tmp_path) as (ui_image, gateway_image),
            _build_images(ui_image) as (builder_image, registry_image),
        ):
            _tcp_service(
                namespace,
                "image-registry",
                5000,
                registry_image,
                host_network=True,
            )
            with _forward(namespace, "image-registry", 5000) as registry_port:
                base_digest = _base_image(registry_port, node_platform())
            git_ip = _git_server(namespace, builder_image, base_digest)
            with _forward(namespace, "git-source", 9418) as git_port:
                remote = subprocess.run(
                    [
                        "git",
                        "ls-remote",
                        f"git://127.0.0.1:{git_port}/shop.git",
                        "HEAD",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
                commit = remote.stdout.split()[0]
            assert len(commit) in {40, 64}
            kubectl(
                "create",
                "secret",
                "generic",
                "piceli-build-git",
                "--from-literal=username=unused",
                "--from-literal=password=unused",
                namespace=namespace,
            )
            kubectl(
                "label",
                "node",
                os.environ["PICELI_KIND_NODE"],
                "piceli.io/builder=true",
                "--overwrite",
            )
            issuer_url, issuer_cert, issuer_pod_ip = _issuer(
                namespace, ui_image, tmp_path
            )
            gateway_cert, gateway_key = _tls_files(tmp_path)
            kubectl(
                "create",
                "secret",
                "tls",
                "piceli-ui-tls",
                "--cert",
                str(gateway_cert),
                "--key",
                str(gateway_key),
                namespace=namespace,
            )
            api = json.loads(
                kubectl(
                    "get", "service", "kubernetes", "-o", "json", namespace="default"
                )
            )["spec"]["clusterIP"]
            node = json.loads(
                kubectl("get", "node", os.environ["PICELI_KIND_NODE"], "-o", "json")
            )
            node_ip = next(
                item["address"]
                for item in node["status"]["addresses"]
                if item["type"] == "InternalIP"
            )
            issuer_ip = urlsplit(issuer_url).hostname
            assert issuer_ip is not None
            definition, source = _source(namespace, api, node_ip, ui_image)
            manual = ManualDeliveryConfig(
                release_definition_toml=definition,
                source_files={"composition.py": source},
                source_file_allowlist=("composition.py",),
                renderer_image=ui_image,
                renderer_platform=node_platform(),
                authorized_deploy_subjects=("operator-1",),
                deploy_resources=(
                    DeployResourceRule(
                        "", "configmaps", ("get", "list", "create", "patch")
                    ),
                    DeployResourceRule(
                        "apps", "deployments", ("get", "list", "create", "patch")
                    ),
                ),
            )
            config: ClusterInstallConfig = replace(
                _config(),
                namespace=namespace,
                api_server=f"https://{node_ip}:6443",
                ui_image=ui_image,
                gateway_image=gateway_image,
                storage_class="standard",
                oidc_issuer=issuer_url,
                oidc_metadata_url=issuer_url + "/.well-known/openid-configuration",
                oidc_client_id="piceli-test",
                authorized_subjects=("operator-1",),
                authorized_access_subjects=("operator-1",),
                api_egress_cidrs=(f"{node_ip}/32",),
                oidc_egress_cidrs=(f"{issuer_ip}/32", f"{issuer_pod_ip}/32"),
                manual=manual,
                ingress_namespace="kube-system",
                ingress_pod_labels={"app.kubernetes.io/name": "ingress-nginx"},
                build=InstalledBuildConfig(
                    spec_path="host-build.toml",
                    image=builder_image,
                    repo=f"git://{git_ip}:9418/shop.git",
                    registry_url="oci://127.0.0.1:5000/shop",
                    platforms=(node_platform(),),
                    cache_size="64Mi",
                    node_arch=node_platform().split("/")[1],
                ),
            )
            objects = list(yaml.safe_load_all(cluster_install_yaml(config)))
            deployment = next(
                item
                for item in objects
                if item["kind"] == "Deployment"
                and item["metadata"]["name"] == "piceli-ui"
            )
            ui = deployment["spec"]["template"]["spec"]["containers"][0]
            ui["env"].append(
                {"name": "SSL_CERT_FILE", "value": "/var/run/oidc-ca/ca.crt"}
            )
            ui["volumeMounts"].append(
                {"name": "oidc-ca", "mountPath": "/var/run/oidc-ca", "readOnly": True}
            )
            deployment["spec"]["template"]["spec"]["volumes"].append(
                {"name": "oidc-ca", "configMap": {"name": "oidc-ca"}}
            )
            kubectl(
                "create",
                "configmap",
                "oidc-ca",
                "--from-file",
                "ca.crt=" + str(issuer_cert),
                namespace=namespace,
            )
            manifest = yaml.safe_dump_all(objects, sort_keys=False)
            kubectl("apply", "-f", "-", stdin=manifest)
            try:
                kubectl(
                    "rollout",
                    "status",
                    "deployment/piceli-ui",
                    "--timeout=90s",
                    namespace=namespace,
                )
            except AssertionError as error:
                pods = json.loads(
                    kubectl("get", "pods", "-o", "json", namespace=namespace)
                )["items"]
                status = [
                    {
                        "pod": pod["metadata"]["name"],
                        "containers": pod.get("status", {}).get(
                            "containerStatuses", []
                        ),
                    }
                    for pod in pods
                    if pod["metadata"]["name"].startswith("piceli-ui-")
                ]
                diagnostics = []
                for container in ("ui", "tls-gateway"):
                    logs = kubectl(
                        "logs",
                        "deployment/piceli-ui",
                        "-c",
                        container,
                        "--tail=60",
                        namespace=namespace,
                        check=False,
                    )
                    diagnostics.extend(
                        line
                        for line in logs.splitlines()
                        if any(
                            marker in line
                            for marker in (
                                "Error",
                                "Exception",
                                "Traceback",
                                "error:",
                            )
                        )
                    )
                raise AssertionError(
                    f"installed UI rollout: {status}; errors: {diagnostics}"
                ) from error
            assert _ui_pod(namespace)["status"]["phase"] == "Running"
            with _forward(namespace, "oidc-issuer", 8443) as issuer_port:
                with _forward(namespace, "piceli-ui", 443) as ui_port:
                    with httpx.Client(
                        base_url=f"https://127.0.0.1:{ui_port}",
                        verify=False,
                        headers={"Host": "piceli.example.test"},
                        timeout=20,
                    ) as browser:
                        _login(browser, issuer_port)
                        assert (
                            browser.get("/api/v1/applications/other").status_code == 404
                        )
                        build_plan = _post(
                            browser,
                            "/cluster-build/plans",
                            {"commit": commit, "cache_key": "main"},
                            expected=200,
                        )
                        assert build_plan["preview"]["namespace"] == namespace
                        built = _post(
                            browser,
                            "/cluster-build/operations",
                            {
                                "plan_id": build_plan["id"],
                                "approved_digest": build_plan["digest"],
                                "idempotency_key": "e2e-build",
                            },
                        )
                        finished_build = _wait(
                            browser,
                            "/cluster-build/operations/" + built["id"],
                            seconds=300,
                        )
                        assert finished_build["state"] == "succeeded", (
                            finished_build.get("error_code"),
                            finished_build.get("failure"),
                        )
                        assert finished_build["images"]
                        preview = _post(
                            browser,
                            "/applications/cluster/evaluation-preview",
                            {"intent": "deploy"},
                            expected=200,
                        )
                        evaluation = _post(
                            browser,
                            "/applications/cluster/evaluations",
                            {
                                "preview_id": preview["id"],
                                "approved_digest": preview["digest"],
                                "idempotency_key": "e2e-evaluate",
                            },
                        )
                        evaluated = _wait(browser, "/evaluations/" + evaluation["id"])
                        assert evaluated["state"] == "succeeded", evaluated.get(
                            "error_code"
                        )
                        plan_response = browser.get(
                            "/api/v1/plans/" + evaluated["plan_id"]
                        )
                        assert plan_response.status_code == 200
                        plan = plan_response.json()
                        approved = _post(
                            browser,
                            "/applications/cluster/operations",
                            {
                                "plan_id": plan["id"],
                                "approved_digest": plan["digest"],
                                "idempotency_key": "e2e-apply",
                            },
                        )
                        running = wait_for(
                            lambda: (
                                value
                                if (
                                    value := browser.get(
                                        "/api/v1/operations/" + approved["id"]
                                    ).json()
                                )["state"]
                                == "running"
                                else None
                            ),
                            seconds=120,
                            message="running installed operation",
                        )
                        assert running["approved_digest"] == plan["digest"]
                        wait_for(
                            lambda: (
                                kubectl(
                                    "get",
                                    "deployment",
                                    "installed-worker",
                                    "-o",
                                    "name",
                                    namespace=namespace,
                                    check=False,
                                ).strip()
                                == "deployment.apps/installed-worker"
                                or None
                            ),
                            seconds=120,
                            message="approved plan applied after pre-rollout",
                        )
                        first = _ui_pod(namespace)
                        kubectl(
                            "delete",
                            "pod",
                            first["metadata"]["name"],
                            "--grace-period=0",
                            "--force",
                            namespace=namespace,
                        )
                        wait_for(
                            lambda: (
                                pod
                                if (pod := _ui_pod(namespace))
                                .get("metadata", {})
                                .get("uid")
                                not in {None, first["metadata"]["uid"]}
                                and pod.get("status", {}).get("phase") == "Running"
                                else None
                            ),
                            seconds=180,
                            message="restarted installed UI",
                        )
                        kubectl(
                            "rollout",
                            "status",
                            "deployment/piceli-ui",
                            "--timeout=120s",
                            namespace=namespace,
                        )
                # The old port-forward was tied to the killed Pod. A new forward
                # reconnects to the Service and the same PVC-backed control store.
                with _forward(namespace, "piceli-ui", 443) as ui_port:
                    with httpx.Client(
                        base_url=f"https://127.0.0.1:{ui_port}",
                        verify=False,
                        headers={"Host": "piceli.example.test"},
                        timeout=20,
                    ) as browser:
                        _login(
                            browser, issuer_port
                        )  # fresh session after Pod replacement
                        recovered = browser.get("/api/v1/operations/" + approved["id"])
                        assert recovered.status_code == 200
                        assert recovered.json()["state"] in {"interrupted", "succeeded"}
                        _remote_forward(browser)
            _backup_restore(namespace, ui_image)
            with _forward(namespace, "oidc-issuer", 8443) as issuer_port:
                with _forward(namespace, "piceli-ui", 443) as ui_port:
                    with httpx.Client(
                        base_url=f"https://127.0.0.1:{ui_port}",
                        verify=False,
                        headers={"Host": "piceli.example.test"},
                        timeout=20,
                    ) as browser:
                        _login(browser, issuer_port)
                        restored = browser.get("/api/v1/operations/" + approved["id"])
                        assert restored.status_code == 200
                        assert restored.json()["approved_digest"] == plan["digest"]
                        _revoke_stream(browser, namespace, issuer_url)
    finally:
        kubectl(
            "delete",
            "namespace",
            namespace,
            "--wait=true",
            "--timeout=120s",
            check=False,
        )
