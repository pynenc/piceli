"""``app.resource``: typed generic objects of any kind, custom resources included."""

from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from piceli import App
from piceli.app.resource import Resource
from piceli.k8s.ops.discovery import RELEASE_NAMESPACE_ANNOTATION
from piceli.k8s.ops.plan import ResourceIntent, ResourceRef


class WidgetSpec(BaseModel):
    """Shaped like a generated spec: identity class variables and aliases."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    piceli_api_version: ClassVar[str] = "example.test/v1"
    piceli_kind: ClassVar[str] = "Widget"
    piceli_scope: ClassVar[str] = "cluster"

    size_gb: int = Field(alias="sizeGb", ge=1)
    color: str | None = None
    tier: str | None = "basic"  # a default the server would apply


class GadgetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    piceli_api_version: ClassVar[str] = "example.test/v1"
    piceli_kind: ClassVar[str] = "Gadget"
    piceli_scope: ClassVar[str] = "namespaced"

    replicas: int


def _objects(app: App, namespace: str = "shop") -> dict[tuple[str, str], tuple]:
    return {
        (r.ref.kind, r.ref.name): (r.ref, r.manifest, component.name)
        for component in app.render(namespace).components
        for r in component.resources
    }


def test_typed_spec_renders_by_alias_and_only_what_was_set():
    app = App("shop")
    gadget = app.resource(
        "example.test/v1", "Gadget", "api-gadget", GadgetSpec(replicas=2)
    )
    widget = app.resource(
        "example.test/v1", "Widget", "shop-widget", WidgetSpec(size_gb=3)
    )
    assert gadget.scope == "namespaced" and widget.scope == "cluster"
    objects = _objects(app)
    ref, manifest, component = objects[("Gadget", "api-gadget")]
    assert ref == ResourceRef("example.test/v1", "Gadget", "shop", "api-gadget")
    assert component == "api-gadget"
    assert manifest == {
        "apiVersion": "example.test/v1",
        "kind": "Gadget",
        "metadata": {
            "name": "api-gadget",
            "namespace": "shop",
            "labels": {"app.kubernetes.io/part-of": "shop"},
        },
        "spec": {"replicas": 2},
    }
    ref, manifest, _ = objects[("Widget", "shop-widget")]
    # Cluster-scoped: no namespace; this release's namespace in the annotation.
    assert ref.namespace == ""
    assert "namespace" not in manifest["metadata"]
    assert manifest["metadata"]["annotations"] == {RELEASE_NAMESPACE_ANNOTATION: "shop"}
    assert manifest["spec"] == {"sizeGb": 3}  # no unset default (tier)


def test_mapping_spec_fields_labels_and_annotations():
    app = App("shop")
    app.resource(
        "scheduling.k8s.io/v1",
        "PriorityClass",
        "shop-high",
        fields={"value": 1000, "globalDefault": False},
        labels={"tier": "high"},
        annotations={"note": "example"},
        component="priorities",
    )
    app.resource(
        "monitoring.coreos.com/v1",
        "ServiceMonitor",
        "api",
        {"endpoints": [{"port": "http"}]},
    )
    objects = _objects(app)
    ref, manifest, component = objects[("PriorityClass", "shop-high")]
    assert component == "priorities"
    assert ref.namespace == ""  # a built-in cluster kind defaults to cluster
    assert manifest["value"] == 1000 and manifest["globalDefault"] is False
    assert manifest["metadata"]["labels"] == {
        "app.kubernetes.io/part-of": "shop",
        "tier": "high",
    }
    assert manifest["metadata"]["annotations"] == {
        "note": "example",
        RELEASE_NAMESPACE_ANNOTATION: "shop",
    }
    _, monitor, _ = objects[("ServiceMonitor", "api")]
    assert monitor["spec"] == {"endpoints": [{"port": "http"}]}


def test_typed_spec_fails_at_declaration():
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        WidgetSpec(size_gb=0)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"api_version": "example.test/v2"}, "not example.test/v2"),
        ({"kind": "Gizmo"}, "of kind Widget, not Gizmo"),
        ({"scope": "namespaced"}, "cluster-scoped \\(from its CRD\\)"),
        ({"api_version": "Example/v1"}, "api_version"),
        ({"kind": "widget"}, "CamelCase"),
        ({"name": "Shop_Widget"}, "DNS subdomain"),
        ({"fields": {"status": {}}}, "fields cannot set \\['status'\\]"),
        ({"fields": {"spec": {}}}, "fields cannot set"),
        ({"fields": {"value": {1, 2}}}, "plain JSON"),
    ],
)
def test_resource_is_validated_at_declaration(arguments, message):
    values = {
        "api_version": "example.test/v1",
        "kind": "Widget",
        "name": "shop-widget",
        "spec": WidgetSpec(size_gb=1),
        **arguments,
    }
    with pytest.raises(ValidationError, match=message):
        App("shop").resource(**values)


def test_spec_must_be_a_model_or_mapping():
    with pytest.raises(ValidationError, match="pydantic model or a mapping"):
        App("shop").resource("example.test/v1", "Widget", "w", ["not", "a", "spec"])  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["Namespace", "CustomResourceDefinition"])
def test_cluster_administration_kinds_are_refused(kind):
    with pytest.raises(ValidationError, match="never manages"):
        App("shop").resource("v1", kind, "other", scope="cluster")


def test_a_cluster_object_naming_another_namespace_is_refused():
    app = App("shop")
    app.resource(
        "example.test/v1",
        "Widget",
        "w",
        WidgetSpec(size_gb=1),
        annotations={RELEASE_NAMESPACE_ANNOTATION: "other"},
    )
    with pytest.raises(ValueError, match="renders for namespace 'shop'"):
        app.render("shop")


def test_same_name_is_fine_across_kinds_but_not_within_one():
    app = App("shop")
    app.deployment("api", image="registry.example/api:1")
    app.resource("monitoring.coreos.com/v1", "ServiceMonitor", "api", {})
    app.resource("monitoring.coreos.com/v1", "PodMonitor", "api", {})
    with pytest.raises(ValueError, match="ServiceMonitor 'api' is already declared"):
        app.resource("monitoring.coreos.com/v1", "ServiceMonitor", "api", {})


def test_override_patches_a_resource_and_keeps_its_identity():
    app = App("shop")
    monitor = app.resource(
        "monitoring.coreos.com/v1", "ServiceMonitor", "api", {"jobLabel": "app"}
    )
    app.override(monitor, {"spec": {"sampleLimit": 100, "jobLabel": None}})
    _, manifest, _ = _objects(app)[("ServiceMonitor", "api")]
    assert manifest["spec"] == {"sampleLimit": 100}
    with pytest.raises(ValueError, match="cannot change kind"):
        app.override(monitor, {"kind": "PodMonitor"})


def test_resources_join_components_and_dependencies():
    app = App("shop")
    api = app.deployment("api", image="registry.example/api:1")
    cert = app.resource(
        "cert-manager.io/v1", "Certificate", "shop-tls", {"secretName": "shop-tls"}
    )
    app.depends(api, on=cert)
    components = {c.name: c for c in app.render("shop").components}
    assert components["api"].dependencies == ("shop-tls",)
    assert [r.ref.kind for r in components["shop-tls"].resources] == ["Certificate"]


def test_cluster_scope_is_inferred_from_the_namespace_annotation():
    """Rebuilt intents (sessions, rollbacks) keep the scope of annotated objects."""
    manifest = {
        "apiVersion": "example.test/v1",
        "kind": "Widget",
        "metadata": {
            "name": "w",
            "annotations": {RELEASE_NAMESPACE_ANNOTATION: "shop"},
        },
    }
    assert ResourceIntent.from_manifest(manifest).ref.namespace == ""
    namespaced = {**manifest, "metadata": {"name": "w", "namespace": "shop"}}
    assert ResourceIntent.from_manifest(namespaced).ref.namespace == "shop"
    plain = {**manifest, "metadata": {"name": "w"}}
    assert ResourceIntent.from_manifest(plain).ref.namespace == "default"


def test_resource_model_is_frozen():
    item = Resource(api_version="v1", kind="ConfigMap", name="x", fields={"data": {}})
    with pytest.raises(ValidationError):
        item.name = "y"  # type: ignore[misc]


def test_public_fields_render_as_an_annotation_and_are_validated():
    app = App("shop")
    app.resource(
        "cert-manager.io/v1",
        "Certificate",
        "shop-tls",
        {"privateKey": {"algorithm": "ECDSA"}},
        public=["spec.privateKey", "spec.items.*.token", "spec.privateKey"],
    )
    _, manifest, _ = _objects(app)[("Certificate", "shop-tls")]
    assert manifest["metadata"]["annotations"] == {
        "piceli.io/public-fields": "spec.items.*.token,spec.privateKey"
    }
    for bad in (["spec..x"], ["spec.privateKey,other"], [""]):
        with pytest.raises(ValidationError, match="dotted path"):
            app.resource("cert-manager.io/v1", "Certificate", "x", {}, public=bad)
    with pytest.raises(ValidationError, match="public="):
        app.resource(
            "cert-manager.io/v1",
            "Certificate",
            "y",
            {},
            annotations={"piceli.io/public-fields": "spec"},
        )
