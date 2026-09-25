"""Opt-in: a mutating admission webhook rewrites what Piceli applies (kind).

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-m2 --kubeconfig /tmp/m2.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/m2.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-m2 \\
      uv run pytest tests/integration/test_ownership_webhook_kind.py

A real in-cluster webhook (a small Python server over TLS, with a CA the test
generates) mutates every Deployment created or updated in the test
namespace, like a sidecar or policy injector: it adds an annotation and an
``INJECTED`` environment variable to the first container, a list the release
declares. Piceli must apply through it, see a re-plan of the same release as
``no-op`` (the server dry run goes through the webhook too), apply a new
image and roll back without duplicating or losing the injected values.

A webhook is used rather than a MutatingAdmissionPolicy because it works on
every supported Kubernetes minor without feature gates.
"""

from __future__ import annotations

import base64
import datetime
import json
import textwrap
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from kind_support import (
    DIGEST_1,
    DIGEST_2,
    cli,
    get,
    kubectl,
    operations,
    requires_kind,
    wait_for,
    write_spec,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(900), requires_kind]

# python:3.13-alpine multi-arch index digest.
PYTHON = (
    "docker.io/library/python@"
    "sha256:79e7a9b9ff1cbceff819f856fb374477792a5967759d94df266de7b7b4120e6f"
)

SERVER = textwrap.dedent(
    """
    import base64, json, ssl
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            review = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            request = review["request"]
            obj = request["object"]
            patch = []
            if "annotations" not in obj["metadata"]:
                patch.append({"op": "add", "path": "/metadata/annotations",
                              "value": {}})
            patch.append({"op": "add",
                          "path": "/metadata/annotations/m2.piceli.test~1injected",
                          "value": "true"})
            container = obj["spec"]["template"]["spec"]["containers"][0]
            env = container.get("env") or []
            if not any(item.get("name") == "INJECTED" for item in env):
                if "env" not in container:
                    patch.append({"op": "add",
                                  "path": "/spec/template/spec/containers/0/env",
                                  "value": []})
                patch.append({"op": "add",
                              "path": "/spec/template/spec/containers/0/env/-",
                              "value": {"name": "INJECTED", "value": "yes"}})
            answer = {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview",
                      "response": {"uid": request["uid"], "allowed": True,
                                   "patchType": "JSONPatch",
                                   "patch": base64.b64encode(
                                       json.dumps(patch).encode()).decode()}}
            body = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("", 8443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/tls/tls.crt", "/tls/tls.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
    """
)

COMPOSITION = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent


def build(ctx):
    labels = {"app": "web"}
    web = ResourceIntent.from_manifest({
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "web", "namespace": ctx.namespace},
        "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                 "template": {"metadata": {"labels": labels}, "spec": {
                     "containers": [{"name": "web", "image": ctx.image("web"),
                                     "env": [{"name": "MODE", "value": "blue"}],
                                     "resources": {"requests": {"cpu": "10m",
                                                                "memory": "16Mi"}}}]}}},
    })
    return DeploymentComposition((DeploymentComponent("web", (web,)),))
"""


def _certificates(host: str) -> tuple[bytes, bytes, bytes]:
    """A CA and a serving certificate for ``host`` (PEM: ca, cert, key)."""
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "m2-webhook-ca")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), False)
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    return (
        ca.public_bytes(pem),
        cert.public_bytes(pem),
        key.private_bytes(
            pem,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


@pytest.fixture
def webhook(kind_namespace: str) -> Iterator[str]:
    """Run the webhook for Deployments in ``kind_namespace``; yield the namespace."""
    namespace = kind_namespace
    host = f"m2-webhook.{namespace}.svc"
    ca, cert, key = _certificates(host)
    b64 = lambda value: base64.b64encode(value).decode()  # noqa: E731
    objects = [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "m2-webhook-tls"},
            "type": "kubernetes.io/tls",
            "data": {"tls.crt": b64(cert), "tls.key": b64(key)},
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "m2-webhook"},
            "data": {"server.py": SERVER},
        },
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "m2-webhook", "labels": {"app": "m2-webhook"}},
            "spec": {
                "containers": [
                    {
                        "name": "webhook",
                        "image": PYTHON,
                        "command": ["python", "/app/server.py"],
                        "ports": [{"containerPort": 8443}],
                        "readinessProbe": {
                            "tcpSocket": {"port": 8443},
                            "periodSeconds": 1,
                        },
                        "volumeMounts": [
                            {"name": "tls", "mountPath": "/tls"},
                            {"name": "app", "mountPath": "/app"},
                        ],
                    }
                ],
                "volumes": [
                    {"name": "tls", "secret": {"secretName": "m2-webhook-tls"}},
                    {"name": "app", "configMap": {"name": "m2-webhook"}},
                ],
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "m2-webhook"},
            "spec": {
                "selector": {"app": "m2-webhook"},
                "ports": [{"port": 443, "targetPort": 8443}],
            },
        },
    ]
    kubectl(
        "apply",
        "-f",
        "-",
        namespace=namespace,
        stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}),
    )
    kubectl(
        "wait", "--for=condition=Ready", "pod/m2-webhook", "--timeout=300s",
        namespace=namespace,
    )  # fmt: skip
    label = uuid.uuid4().hex[:8]
    kubectl("label", "namespace", namespace, f"m2.piceli.test/inject={label}")
    name = f"m2-inject-{label}"
    configuration = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "MutatingWebhookConfiguration",
        "metadata": {"name": name},
        "webhooks": [
            {
                "name": "inject.m2.piceli.test",
                "admissionReviewVersions": ["v1"],
                "sideEffects": "None",
                "failurePolicy": "Fail",
                "timeoutSeconds": 10,
                "namespaceSelector": {"matchLabels": {"m2.piceli.test/inject": label}},
                "rules": [
                    {
                        "apiGroups": ["apps"],
                        "apiVersions": ["v1"],
                        "operations": ["CREATE", "UPDATE"],
                        "resources": ["deployments"],
                    }
                ],
                "clientConfig": {
                    "service": {
                        "namespace": namespace,
                        "name": "m2-webhook",
                        "path": "/mutate",
                    },
                    "caBundle": b64(ca),
                },
            }
        ],
    }
    kubectl("apply", "-f", "-", stdin=json.dumps(configuration))
    try:
        # The Service's endpoints can lag behind the ready Pod: wait until a
        # server dry run of a Deployment comes back mutated.
        wait_for(
            lambda: (
                '"m2.piceli.test/injected"'
                in kubectl(
                    "create",
                    "deployment",
                    "warm-up",
                    "--image=busybox",
                    "--dry-run=server",
                    "-o",
                    "json",
                    namespace=namespace,
                    check=False,
                )
            ),
            seconds=180,
            message="the webhook to answer",
        )
        yield namespace
    finally:
        kubectl("delete", "mutatingwebhookconfiguration", name, check=False)


def _injected(namespace: str) -> tuple[str | None, list[str], str]:
    live = get("deployment", "web", namespace)
    container = live["spec"]["template"]["spec"]["containers"][0]
    return (
        live["metadata"].get("annotations", {}).get("m2.piceli.test/injected"),
        [item["name"] for item in container.get("env", [])],
        container["image"],
    )


def test_mutating_webhook_is_applied_through_without_perpetual_diff(
    tmp_path: Path, webhook: str
) -> None:
    namespace = webhook
    (tmp_path / "web.py").write_text(COMPOSITION)
    spec = write_spec(tmp_path, namespace, "web.py")
    code, applied = cli(spec, "apply", "--auto-approve")
    assert code == 0 and applied["release_state"] == "ready", applied
    first = applied["release"]
    annotation, env, image = _injected(namespace)
    assert (annotation, env) == ("true", ["MODE", "INJECTED"])

    code, planned = cli(spec, "plan")
    assert code == 0, planned
    assert operations(planned) == {"Deployment/web": "no-op"}, planned["diffs"]

    spec = write_spec(tmp_path, namespace, "web.py", digest=DIGEST_2)
    code, applied = cli(spec, "apply", "--auto-approve")
    assert code == 0 and applied["release_state"] == "ready", applied
    annotation, env, image = _injected(namespace)
    assert (annotation, env) == ("true", ["MODE", "INJECTED"])
    assert image.endswith(DIGEST_2)

    code, rolled = cli(spec, "rollback", first, "--auto-approve")
    assert code == 0 and rolled["selected"] == first, rolled
    annotation, env, image = _injected(namespace)
    assert (annotation, env) == ("true", ["MODE", "INJECTED"])
    assert image.endswith(DIGEST_1)
