"""Acceptance: typed custom resources (``app.resource``) through ``piceli release``.

The Certificate and ServiceMonitor specs are the modules ``piceli codegen crd``
generated from the pinned CRDs in ``tests/fixtures/crds``
(``examples/environments/crds``). A cluster-scoped ``Widget`` follows the
per-namespace ownership rules of ClusterRoles: the release manages, changes
and prunes only the objects that carry its owner and its namespace.
"""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from piceli.k8s.ops.discovery import RELEASE_NAMESPACE_ANNOTATION
from piceli.testing import TARGET, TYPES, FakeAPI, serve, write_kubeconfig

ROOT = Path(__file__).resolve().parents[2]
DIGEST = "sha256:" + "5" * 64
OWNER = "crd-shop"
NS = TARGET.namespace
CRD_TYPES = {
    **TYPES,
    "certificates": ("cert-manager.io/v1", "Certificate", True),
    "servicemonitors": ("monitoring.coreos.com/v1", "ServiceMonitor", True),
}

COMPOSITION = """
from crds import certificate, service_monitor

from piceli import App, ServicePort

WIDGET = {widget!r}
SCOPE = {scope!r}
HOSTS = {hosts!r}


def build(ctx):
    shop = App("shop")
    api = shop.deployment("api", image=ctx.image("api"), ports=[8080])
    shop.service(api, ports=[ServicePort(port=80, target_port=8080, name="http")])
    shop.resource(
        certificate.API_VERSION,
        certificate.KIND,
        "shop-tls",
        certificate.CertificateSpec(
            secret_name="shop-tls",
            dns_names=HOSTS,
            issuer_ref={{"name": "letsencrypt", "kind": "ClusterIssuer"}},
            private_key={{"algorithm": "ECDSA", "size": 256}},
        ),
        # Key settings, not key material: public in plans.
        public=["spec.privateKey"],
    )
    shop.resource(
        service_monitor.API_VERSION,
        service_monitor.KIND,
        "api",
        service_monitor.ServiceMonitorSpec(
            selector={{"matchLabels": api.selector_labels}},
            endpoints=[
                {{
                    "port": "http",
                    "interval": "30s",
                    # A Secret reference, never the token: public in plans.
                    "bearerTokenSecret": {{"name": "api-token", "key": "token"}},
                }}
            ],
        ),
        component="monitoring",
    )
    if WIDGET:
        shop.resource(
            "example.test/v1", "Widget", "shop-widget", {{"size": 1}}, scope=SCOPE
        )
    return shop
"""


def _widget(name: str, namespace: str | None, owner: str = OWNER) -> dict:
    annotations = {"piceli.io/owner": owner}
    if namespace is not None:
        annotations[RELEASE_NAMESPACE_ANNOTATION] = namespace
    return {
        "apiVersion": "example.test/v1",
        "kind": "Widget",
        "metadata": {"name": name, "annotations": annotations},
        "spec": {"size": 9},
    }


def _module(directory: Path, name: str, **values: object) -> str:
    settings = {"widget": True, "scope": "cluster", "hosts": ["shop.example.com"]}
    settings.update(values)
    (directory / name).write_text(COMPOSITION.format(**settings))
    return name


def _spec(directory: Path, module: str) -> Path:
    spec = directory / "release.toml"
    spec.write_text(
        textwrap.dedent(
            f"""
            [target]
            kubeconfig = "kubeconfig"
            context = "fake"
            namespace = "{NS}"
            transport = "loopback-http"

            [release]
            name = "shop"
            owner = "{OWNER}"
            field_manager = "{OWNER}"
            composition = "{module}:build"
            state_dir = "state"
            prune = true

            [execution]
            max_seconds = 30
            readiness_seconds = 1
            poll_seconds = 0.05

            [images]
            api = "registry.example/api@{DIGEST}"
            """
        )
    )
    return spec


def _run(spec: Path, *args: str) -> tuple[int, dict, str]:
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}"), result.output


def _operations(payload: dict) -> dict[str, str]:
    return {f"{a['kind']}/{a['name']}": a["operation"] for a in payload["actions"]}


@pytest.fixture
def crd_env(tmp_path):
    shutil.copytree(ROOT / "examples" / "environments" / "crds", tmp_path / "crds")
    api = FakeAPI(types=CRD_TYPES)
    # The same owner released the app in another namespace.
    api.put(_widget("other-widget", "other-ns"))
    with serve(api) as (api, url):
        write_kubeconfig(url, tmp_path / "kubeconfig")
        yield api, tmp_path


def test_typed_custom_resources_plan_apply_noop_and_prune(crd_env):
    api, directory = crd_env
    spec = _spec(directory, _module(directory, "full.py"))
    code, planned, output = _run(spec, "plan")
    assert code == 0, output
    assert _operations(planned) == {
        "Deployment/api": "create",
        "Service/api": "create",
        "Certificate/shop-tls": "create",
        "ServiceMonitor/api": "create",
        "Widget/shop-widget": "create",
    }
    scoped = {a["name"] for a in planned["actions"] if a.get("cluster_scoped")}
    assert scoped == {"shop-widget"}
    assert "Widget/shop-widget  [cluster-scoped]" in output
    code, applied, output = _run(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, output
    assert applied["execution"]["state"] == "ready", applied
    certificate = api.objects[("Certificate", "shop-tls")]
    assert certificate["spec"] == {
        "secretName": "shop-tls",
        "dnsNames": ["shop.example.com"],
        "issuerRef": {"name": "letsencrypt", "kind": "ClusterIssuer"},
        "privateKey": {"algorithm": "ECDSA", "size": 256},
    }
    assert certificate["metadata"]["namespace"] == NS
    assert certificate["metadata"]["annotations"]["piceli.io/public-fields"] == (
        "spec.privateKey"
    )
    monitor = api.objects[("ServiceMonitor", "api")]
    assert monitor["spec"]["endpoints"] == [
        {
            "port": "http",
            "interval": "30s",
            "bearerTokenSecret": {"name": "api-token", "key": "token"},
        }
    ]
    widget = api.objects[("Widget", "shop-widget")]
    assert "namespace" not in widget["metadata"]
    assert widget["metadata"]["annotations"]["piceli.io/owner"] == OWNER
    assert widget["metadata"]["annotations"][RELEASE_NAMESPACE_ANNOTATION] == NS

    code, again, output = _run(spec, "plan")
    assert code == 0, output
    assert set(again["summary"]) == {"no-op"}, again["summary"]

    # A typed change is one update; dropping the Widget prunes only ours.
    spec = _spec(
        directory,
        _module(directory, "trimmed.py", widget=False, hosts=["shop.example.org"]),
    )
    code, planned, output = _run(spec, "plan")
    assert code == 0, output
    changes = {k: v for k, v in _operations(planned).items() if v != "no-op"}
    assert changes == {"Certificate/shop-tls": "apply", "Widget/shop-widget": "delete"}
    api.terminating_reads = 1
    code, applied, output = _run(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, output
    assert applied["execution"]["state"] == "ready", applied
    assert ("Widget", "shop-widget") not in api.objects
    assert ("Widget", "other-widget") in api.objects
    assert api.objects[("Certificate", "shop-tls")]["spec"]["dnsNames"] == [
        "shop.example.org"
    ]


def test_a_cluster_object_of_another_namespace_needs_adoption(crd_env):
    api, directory = crd_env
    api.put(_widget("shop-widget", "other-ns"))
    spec = _spec(directory, _module(directory, "full.py"))
    code, refused, output = _run(spec, "plan")
    assert code == 2, output
    assert refused["reason"] == "resource-requires-adoption"
    assert "Widget" in output


def test_a_scope_that_discovery_contradicts_is_refused(crd_env):
    _, directory = crd_env
    spec = _spec(directory, _module(directory, "wrong.py", scope="namespaced"))
    code, refused, output = _run(spec, "plan")
    assert code == 2, output
    assert refused["state"] == "rejected"
    assert refused["reason"] == "resource-scope-mismatch", refused


def test_raw_compositions_follow_the_same_cluster_rules(tmp_path):
    source = textwrap.dedent(
        """
        from piceli.k8s.ops.plan import (
            DeploymentComponent, DeploymentComposition, ResourceIntent,
        )

        KIND, API, ANNOTATED = {kind!r}, {api!r}, {annotated!r}


        def build(ctx):
            meta = {{"name": "thing"}}
            if ANNOTATED:
                meta["annotations"] = {{"piceli.io/namespace": ctx.namespace}}
            body = {{"apiVersion": API, "kind": KIND, "metadata": meta}}
            return DeploymentComposition(
                (DeploymentComponent("c", (ResourceIntent.from_manifest(body),)),)
            )
        """
    )
    with serve() as (api, url):
        write_kubeconfig(url, tmp_path / "kubeconfig")
        cases = [
            ("annotated.py", "Widget", "example.test/v1", True, 0),
            ("namespace.py", "Namespace", "v1", True, 2),
            ("volume.py", "PersistentVolume", "v1", True, 2),
        ]
        for module, kind, api_version, annotated, expected in cases:
            (tmp_path / module).write_text(
                source.format(kind=kind, api=api_version, annotated=annotated)
            )
            code, payload, output = _run(_spec(tmp_path, module), "plan")
            assert code == expected, output
            if expected:
                assert payload["reason"] == "invalid-composition"
            else:
                assert payload["actions"][0]["cluster_scoped"] is True
