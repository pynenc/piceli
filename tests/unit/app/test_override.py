"""``App.override`` and ``app.deployment(container=...)``."""

from __future__ import annotations

import pytest

from piceli import App
from piceli.app.app import merge_override
from piceli.k8s.ops.secret_versions import SecretVersionRef

REF = SecretVersionRef("0" * 32, "1" * 32)


def _manifest(app: App, kind: str, name: str) -> dict:
    composition = app.render("shop")
    return next(
        resource.manifest
        for component in composition.components
        for resource in component.resources
        if (resource.ref.kind, resource.ref.name) == (kind, name)
    )


def test_merge_rules() -> None:
    base = {
        "a": {"b": 1, "c": 2},
        "items": [{"name": "x", "v": 1}, {"name": "y", "v": 2}],
        "plain": [1, 2],
    }
    patch = {
        "a": {"c": None, "d": 3},
        "items": [{"name": "y", "v": 5}, {"name": "z", "v": 6}],
        "plain": [3],
    }
    assert merge_override(base, patch) == {
        "a": {"b": 1, "d": 3},
        "items": [{"name": "x", "v": 1}, {"name": "y", "v": 5}, {"name": "z", "v": 6}],
        "plain": [3],
    }
    assert base["a"] == {"b": 1, "c": 2}  # pure


def test_duplicate_names_are_replaced_not_merged() -> None:
    base = {"m": [{"name": "v", "path": "/a"}, {"name": "v", "path": "/b"}]}
    assert merge_override(base, {"m": [{"name": "v", "path": "/c"}]}) == {
        "m": [{"name": "v", "path": "/c"}]
    }


def test_override_applies_in_order_to_the_rendered_manifest() -> None:
    app = App("shop")
    api = app.deployment("api", image="example.org/api:1", container="server")
    app.override(
        api,
        {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "server",
                                "securityContext": {"runAsNonRoot": True},
                            }
                        ],
                        "tolerations": [{"key": "dedicated", "operator": "Exists"}],
                    }
                }
            }
        },
    )
    app.override(api, {"metadata": {"annotations": {"team": "a"}}})
    manifest = _manifest(app, "Deployment", "api")
    pod = manifest["spec"]["template"]["spec"]
    assert pod["containers"] == [
        {
            "name": "server",
            "image": "example.org/api:1",
            "securityContext": {"runAsNonRoot": True},
        }
    ]
    assert pod["tolerations"] == [{"key": "dedicated", "operator": "Exists"}]
    assert manifest["metadata"]["annotations"] == {"team": "a"}


def test_override_keeps_secret_bindings_and_refuses_secret_data() -> None:
    app = App("shop")
    secret = app.secret("token", {"value": REF})
    app.override(secret, {"metadata": {"labels": {"tier": "auth"}}})
    composition = app.render("shop")
    resource = composition.components[0].resources[0]
    assert resource.manifest["metadata"]["labels"]["tier"] == "auth"
    assert [binding.json_pointer for binding in resource.secret_bindings] == [
        "/data/value"
    ]
    with pytest.raises(ValueError, match="Secret data"):
        app.override(secret, {"data": {"value": "plain"}})
    with pytest.raises(ValueError, match="Secret data"):
        app.override(secret, {"stringData": {"value": "plain"}})


@pytest.mark.parametrize(
    "patch",
    [
        {"kind": "Service"},
        {"apiVersion": "v2"},
        {"metadata": {"name": "other"}},
        {"metadata": {"namespace": "other"}},
        {"metadata": "x"},
        {"spec": {"value": float("nan")}},
    ],
)
def test_identity_cannot_be_overridden(patch: dict) -> None:
    app = App("shop")
    config = app.config("settings", {"a": "b"})
    with pytest.raises(ValueError):
        app.override(config, patch)


def test_override_needs_an_object_of_this_app() -> None:
    app, other = App("shop"), App("other")
    config = other.config("settings", {"a": "b"})
    with pytest.raises(ValueError, match="declared on this app"):
        app.override(config, {"data": {"a": "c"}})


def test_container_name_defaults_to_the_deployment() -> None:
    app = App("shop")
    app.deployment("api", image="example.org/api:1")
    manifest = _manifest(app, "Deployment", "api")
    assert manifest["spec"]["template"]["spec"]["containers"][0]["name"] == "api"
