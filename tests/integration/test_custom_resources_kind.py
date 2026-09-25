"""Opt-in: typed custom resources and ``piceli codegen crd`` on a real cluster (kind).

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-crd --kubeconfig /tmp/crd.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/crd.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-crd \\
      uv run pytest tests/integration/test_custom_resources_kind.py

The test never reads the ambient kubeconfig. It installs the pinned
cert-manager ``Certificate`` and Prometheus-operator ``ServiceMonitor`` CRDs
from ``tests/fixtures/crds`` (no operator runs) and a cluster-scoped
``Widget`` CRD, then:

* ``piceli codegen crd --from-cluster`` generates byte for byte the modules
  generated from the files (``examples/environments/crds``);
* a typed app with a Certificate, a ServiceMonitor and a cluster-scoped
  Widget is planned, applied and re-planned as a no-op;
* a namespaced declaration of the Widget is refused
  (``resource-scope-mismatch``), and dropping it prunes it.

The CRDs, and with them every custom resource, are deleted afterwards.
"""

from __future__ import annotations

import json
import os
import shutil
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from piceli.k8s.ops.discovery import RELEASE_NAMESPACE_ANNOTATION
from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

# nginx:1.27-alpine multi-arch index digest (as in kind_support).
DIGEST_1 = "sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "crds"
GOLDEN = ROOT / "examples" / "environments" / "crds"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(600),
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT",
    ),
]

WIDGET_CRD = {
    "apiVersion": "apiextensions.k8s.io/v1",
    "kind": "CustomResourceDefinition",
    "metadata": {"name": "widgets.example.test"},
    "spec": {
        "group": "example.test",
        "scope": "Cluster",
        "names": {"kind": "Widget", "plural": "widgets", "singular": "widget"},
        "versions": [
            {
                "name": "v1",
                "served": True,
                "storage": True,
                "schema": {
                    "openAPIV3Schema": {
                        "type": "object",
                        "properties": {
                            "spec": {
                                "type": "object",
                                "properties": {"size": {"type": "integer"}},
                            }
                        },
                    }
                },
            }
        ],
    },
}

COMPOSITION = """
from crds import certificate, service_monitor

from piceli import App

WIDGET = {widget!r}
SCOPE = {scope!r}


def build(ctx):
    shop = App("shop")
    shop.config("settings", {{"LOG_LEVEL": "info"}})
    shop.resource(
        certificate.API_VERSION,
        certificate.KIND,
        "shop-tls",
        certificate.CertificateSpec(
            secret_name="shop-tls",
            dns_names=["shop.example.com"],
            issuer_ref={{"name": "letsencrypt", "kind": "ClusterIssuer"}},
            private_key={{"algorithm": "ECDSA", "size": 256}},
        ),
        public=["spec.privateKey"],
    )
    shop.resource(
        service_monitor.API_VERSION,
        service_monitor.KIND,
        "api",
        service_monitor.ServiceMonitorSpec(
            selector={{"matchLabels": {{"app.kubernetes.io/name": "api"}}}},
            endpoints=[
                {{
                    "port": "http",
                    "interval": "30s",
                    "bearerTokenSecret": {{"name": "api-token", "key": "token"}},
                }}
            ],
        ),
    )
    if WIDGET:
        shop.resource(
            "example.test/v1", "Widget", ctx.namespace + "-widget", {{"size": 1}},
            scope=SCOPE,
        )
    return shop
"""


def _crds() -> list[dict[str, Any]]:
    return [
        *(
            yaml.safe_load((FIXTURES / name).read_text())
            for name in sorted(os.listdir(FIXTURES))
        ),
        WIDGET_CRD,
    ]


@pytest.fixture
def cluster():
    from kubernetes.client import ApiextensionsV1Api, CoreV1Api

    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    extensions = ApiextensionsV1Api(client)
    core = CoreV1Api(client)
    names = []
    for crd in _crds():
        extensions.create_custom_resource_definition(crd)
        names.append(crd["metadata"]["name"])
    deadline = time.monotonic() + 120
    for name in names:
        while True:
            status = extensions.read_custom_resource_definition(name).status
            conditions = {c.type: c.status for c in (status.conditions or [])}
            if conditions.get("Established") == "True":
                break
            assert time.monotonic() < deadline, f"{name} not established"
            time.sleep(1)
    namespace = "crd-" + uuid.uuid4().hex[:8]
    core.create_namespace({"metadata": {"name": namespace}})
    try:
        yield namespace
    finally:
        core.delete_namespace(namespace)
        for name in names:
            extensions.delete_custom_resource_definition(name)
        client.close()


def _spec(directory: Path, namespace: str, module: str) -> Path:
    spec = directory / "shop.toml"
    spec.write_text(
        textwrap.dedent(
            f"""
            [target]
            kubeconfig = "{KUBECONFIG}"
            context = "{CONTEXT}"
            namespace = "{namespace}"
            [release]
            name = "shop"
            owner = "crd-shop-e2e"
            field_manager = "crd-shop-e2e"
            composition = "{module}:build"
            state_dir = "state"
            prune = true
            [execution]
            readiness_seconds = 60
            [images]
            # Unused by the composition; a spec declares at least one image.
            unused = "docker.io/library/nginx@{DIGEST_1}"
            """
        )
    )
    return spec


def _release(spec: Path, *args: str) -> tuple[int, dict, str]:
    result = CliRunner().invoke(cli, ["release", *args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}"), result.output


def _apply(spec: Path) -> dict:
    code, planned, output = _release(spec, "plan")
    assert code == 0, output
    code, done, output = _release(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, output
    assert done["execution"]["state"] == "ready", done
    return planned


def _custom(group: str, plural: str, name: str, namespace: str | None) -> Any:
    from kubernetes.client import CustomObjectsApi
    from kubernetes.client.exceptions import ApiException

    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    try:
        api = CustomObjectsApi(client)
        if namespace is None:
            return api.get_cluster_custom_object(group, "v1", plural, name)
        return api.get_namespaced_custom_object(group, "v1", namespace, plural, name)
    except ApiException as error:
        if error.status == 404:
            return None
        raise
    finally:
        client.close()


def test_codegen_and_typed_custom_resources_on_kind(cluster, tmp_path):
    namespace = cluster
    runner = CliRunner()
    for name, golden in (
        ("certificates.cert-manager.io", "certificate.py"),
        ("servicemonitors.monitoring.coreos.com", "service_monitor.py"),
    ):
        result = runner.invoke(
            cli,
            [
                "codegen",
                "crd",
                "--from-cluster",
                "--kubeconfig",
                KUBECONFIG,
                "--context",
                CONTEXT,
                "--crd",
                name,
            ],
        )
        assert result.exit_code == 0, result.output
        assert result.stdout == (GOLDEN / golden).read_text()

    shutil.copytree(GOLDEN, tmp_path / "crds")
    for module, widget, scope in (
        ("full.py", True, "cluster"),
        ("wrong.py", True, "namespaced"),
        ("trimmed.py", False, "cluster"),
    ):
        (tmp_path / module).write_text(COMPOSITION.format(widget=widget, scope=scope))

    planned = _apply(_spec(tmp_path, namespace, "full.py"))
    scoped = {a["name"] for a in planned["actions"] if a.get("cluster_scoped")}
    assert scoped == {f"{namespace}-widget"}
    certificate = _custom("cert-manager.io", "certificates", "shop-tls", namespace)
    assert certificate["spec"]["privateKey"] == {"algorithm": "ECDSA", "size": 256}
    monitor = _custom("monitoring.coreos.com", "servicemonitors", "api", namespace)
    assert monitor["spec"]["endpoints"][0]["bearerTokenSecret"] == {
        "name": "api-token",
        "key": "token",
    }
    widget = _custom("example.test", "widgets", f"{namespace}-widget", None)
    assert widget["metadata"]["annotations"][RELEASE_NAMESPACE_ANNOTATION] == namespace

    code, again, output = _release(_spec(tmp_path, namespace, "full.py"), "plan")
    assert code == 0, output
    assert set(again["summary"]) == {"no-op"}, again["summary"]

    code, refused, output = _release(_spec(tmp_path, namespace, "wrong.py"), "plan")
    assert code == 2, output
    assert refused["reason"] == "resource-scope-mismatch"

    planned = _apply(_spec(tmp_path, namespace, "trimmed.py"))
    deletes = {a["name"] for a in planned["actions"] if a["operation"] == "delete"}
    assert deletes == {f"{namespace}-widget"}
    assert _custom("example.test", "widgets", f"{namespace}-widget", None) is None
