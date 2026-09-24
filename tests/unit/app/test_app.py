"""App rendering: components, dependencies, selectors, claims, nodes and snapshots."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from piceli import App, ConfigVolume, Container, ExistingClaim, MemoryVolume
from piceli.app.render import placeholder_inputs, rendered, to_yaml
from piceli.k8s import templates
from piceli.k8s.ops.plan import DeploymentComponent, ResourceIntent
from piceli.k8s.release_spec import NodeRef
from tests.unit.app import shop
from tests.unit.app.conftest import canonical

SNAPSHOTS = Path(__file__).parent / "snapshots"
REF = placeholder_inputs(["token"])["token"]


def _by_kind(composition):
    return {
        (resource.ref.kind, resource.ref.name): resource
        for component in composition.components
        for resource in component.resources
    }


def _check_snapshot(name: str, text: str) -> None:
    path = SNAPSHOTS / name
    if os.environ.get("PICELI_UPDATE_SNAPSHOTS"):
        path.write_text(text)
    assert path.exists(), f"missing snapshot {name}; set PICELI_UPDATE_SNAPSHOTS=1"
    assert text == path.read_text(), (
        f"snapshot {name} changed; review, then set PICELI_UPDATE_SNAPSHOTS=1"
    )


# ------------------------------------------------------------ full example


def test_shop_needs_no_dicts_and_renders(shop_ctx):
    source = Path(shop.__file__).read_text()
    assert "apiVersion" not in source and "from_manifest" not in source
    composition = shop.build(shop_ctx)
    components = {c.name: c.dependencies for c in composition.components}
    assert components == {
        "api": ("cache", "cache-password"),
        "cache": ("cache-password", "cache-settings", "cache-tls"),
        "cache-password": (),
        "cache-settings": (),
        "cache-tls": (),
    }
    objects = _by_kind(composition)
    assert set(objects) == {
        ("ConfigMap", "cache-settings"),
        ("Secret", "cache-password"),
        ("Secret", "cache-tls"),
        ("Deployment", "cache"),
        ("Deployment", "api"),
        ("Service", "cache"),
        ("Service", "api"),
        ("NetworkPolicy", "cache-ingress"),
    }
    pod = objects[("Deployment", "cache")].manifest["spec"]["template"]["spec"]
    assert pod["shareProcessNamespace"] is True
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "node-a"}
    assert [c["name"] for c in pod["initContainers"]] == ["render-config"]
    assert [c["name"] for c in pod["containers"]] == ["cache", "exporter"]
    assert pod["volumes"] == [
        {"name": "run-cache", "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"}},
        {"name": "cache-state", "persistentVolumeClaim": {"claimName": "cache-state"}},
        {"name": "cache-tls", "secret": {"secretName": "cache-tls"}},
    ]
    service = objects[("Service", "api")].manifest
    assert service["spec"]["ports"] == [
        {"port": 80, "targetPort": 8080, "protocol": "TCP"}
    ]
    assert service["spec"]["selector"] == {"app.kubernetes.io/name": "api"}
    secret = objects[("Secret", "cache-tls")]
    assert [b.json_pointer for b in secret.secret_bindings] == [
        "/data/tls.crt",
        "/data/tls.key",
    ]
    assert secret.manifest["data"] == {"tls.crt": "<private>", "tls.key": "<private>"}


def test_shop_snapshot(shop_ctx):
    composition = shop.build(shop_ctx)
    _check_snapshot("shop.yaml", to_yaml(rendered(composition, dict(shop_ctx.secrets))))


def test_render_is_deterministic(shop_ctx):
    assert canonical(shop.build(shop_ctx)) == canonical(shop.build(shop_ctx))


# ---------------------------------------------------------- existing claims


def test_existing_claim_never_emits_a_claim(shop_ctx):
    composition = shop.build(shop_ctx)
    kinds = {r.ref.kind for c in composition.components for r in c.resources}
    assert "PersistentVolumeClaim" not in kinds
    assert all(
        r.ref.name != "cache-state" for c in composition.components for r in c.resources
    )


def test_existing_claim_refuses_a_managed_claim_of_the_same_name():
    app = App("db")
    app.deployment("db", image="db:1", volumes={"/data": ExistingClaim("db-state")})
    claim = ResourceIntent.from_manifest(
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "db-state", "namespace": "demo"},
            "spec": {"resources": {"requests": {"storage": "1Gi"}}},
        }
    )
    app.add(DeploymentComponent("storage", (claim,)))
    with pytest.raises(ValueError, match="ExistingClaim and must never be managed"):
        app.render("demo")


# --------------------------------------------------------- selector stability


def _selector(app: App, namespace: str = "demo") -> dict[str, str]:
    deployment = _by_kind(app.render(namespace))[("Deployment", "web")]
    return deployment.manifest["spec"]["selector"]["matchLabels"]


def test_selector_does_not_depend_on_app_component_or_labels():
    base = App("shop")
    base.deployment("web", image="web:1")
    renamed = App("store", labels={"team": "payments"})
    renamed.deployment(
        "web", image="web:2", component="frontend", labels={"tier": "front"}, replicas=3
    )
    assert _selector(base) == _selector(renamed) == {"app.kubernetes.io/name": "web"}
    assert _selector(base, "other") == _selector(base)


def test_pod_labels_always_carry_the_selector():
    app = App("shop", labels={"app.kubernetes.io/name": "shop"})
    web = app.deployment("web", image="web:1", selector={"app": "web"})
    manifest = _by_kind(app.render("demo"))[("Deployment", "web")].manifest
    pod_labels = manifest["spec"]["template"]["metadata"]["labels"]
    assert web.selector_labels.items() <= pod_labels.items()
    assert manifest["spec"]["selector"]["matchLabels"] == {"app": "web"}


# ------------------------------------------------------ components and nodes


def test_dependencies_are_inferred_and_explicit():
    app = App("shop")
    config = app.config("settings", {"a": "b"}, component="config")
    worker = app.deployment(
        "worker", image="w:1", volumes={"/etc/w": ConfigVolume(config)}
    )
    # same component as the config it reads: no self edge
    app.deployment("api", image="a:1", env={"A": config.key("a")}, component="config")
    batch = app.deployment("batch", image="b:1")
    app.depends(batch, on=[worker, "config"])
    composition = app.render("demo")
    assert {c.name: c.dependencies for c in composition.components} == {
        "batch": ("config", "worker"),
        "config": (),
        "worker": ("config",),
    }


def test_depends_rejects_unknown_and_self():
    app = App("shop")
    web = app.deployment("web", image="w:1")
    with pytest.raises(ValueError, match="cannot depend on itself"):
        app.depends(web, on=web)
    app.depends(web, on="database")
    with pytest.raises(ValueError, match="unknown components \\['database'\\]"):
        app.render("demo")


def test_duplicate_declarations_are_refused():
    app = App("shop")
    app.deployment("web", image="w:1")
    with pytest.raises(ValueError, match="already declared"):
        app.deployment("web", image="w:2")


def test_node_pinning_uses_verified_nodes():
    app = App("shop")
    app.deployment("web", image="w:1", node="primary")
    with pytest.raises(ValueError, match="verified nodes: \\[\\]"):
        app.render("demo")
    rendered_ = app.render("demo", nodes={"primary": NodeRef("node-a", "uid")})
    pod = _by_kind(rendered_)[("Deployment", "web")].manifest["spec"]["template"]
    assert pod["spec"]["nodeSelector"] == {"kubernetes.io/hostname": "node-a"}


def test_add_template_component_and_depend_on_it():
    registry = templates.NodeLocalRegistry(node_name="node-a")
    app = App("shop")
    web = app.deployment("web", image="127.0.0.1:5000/web@sha256:" + "1" * 64)
    app.add(registry)
    app.depends(web, on="registry")
    composition = app.render("demo")
    assert {c.name: c.dependencies for c in composition.components} == {
        "registry": (),
        "web": ("registry",),
    }
    app.config("registry", {"a": "b"})
    with pytest.raises(ValueError, match="both declared and added"):
        app.render("demo")


def test_render_rejects_invalid_namespace():
    with pytest.raises(ValueError, match="invalid namespace"):
        App("shop").render("Not Valid")


def test_app_is_a_composition_function(release_ctx):
    app = App("shop")
    app.deployment("web", image="w:1")
    assert canonical(app(release_ctx)) == canonical(app.render("release-demo"))


def test_declaration_errors_are_pydantic_errors():
    app = App("shop")
    with pytest.raises(ValidationError) as excinfo:
        app.deployment("web", image="w:1", replicas=-1)
    assert "replicas" in str(excinfo.value)
    with pytest.raises(ValidationError) as excinfo:
        app.deployment("web", image="w:1", sidecars=[Container(name="web", image="x")])
    assert "container names must be unique" in str(excinfo.value)
    with pytest.raises(ValidationError):
        app.secret("creds", {"token": "plain"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="either port= or ports="):
        app.service(app.deployment("api", image="a:1"))
    assert app.objects[-1].name == "api"


def test_memory_volume_shared_between_containers():
    app = App("shop")
    scratch = MemoryVolume()
    app.deployment(
        "web",
        image="w:1",
        volumes={"/scratch": scratch},
        sidecars=[Container(name="helper", image="h:1", volumes={"/scratch": scratch})],
    )
    pod = _by_kind(app.render("demo"))[("Deployment", "web")].manifest["spec"][
        "template"
    ]["spec"]
    assert pod["volumes"] == [{"name": "scratch", "emptyDir": {"medium": "Memory"}}]
