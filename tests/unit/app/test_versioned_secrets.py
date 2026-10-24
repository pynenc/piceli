"""Versioned Secrets: content-addressed names, and readers follow them (0.27.0)."""

from __future__ import annotations

import json
import re

from piceli.app import App, Route
from piceli.app.model import SecretVolume
from piceli.k8s.ops.content_digest import (
    carry_live_secret_digests,
    content_digests,
    versioned_secret_names,
)
from piceli.k8s.ops.plan import DeploymentComposition, ResourceIntent
from piceli.k8s.ops.secret_names import (
    VERSIONED_SECRET_ANNOTATION,
    secret_names,
    versioned_logical,
)
from piceli.k8s.ops.secret_versions import SecretVersionRef

IMAGE = "registry.example/app@sha256:" + "a" * 64


def _ref(version: str) -> SecretVersionRef:
    return SecretVersionRef("1" * 32, version * 32)


def _app(value: SecretVersionRef, *, versioned: bool = True) -> App:
    app = App("relay")
    turn = app.secret("turn", {"turnserver.conf": value}, versioned=versioned)
    relay = app.deployment(
        "relay",
        image=IMAGE,
        ports=[8080],
        env={"SECRET": turn.key("turnserver.conf")},
        volumes={"/etc/turn": SecretVolume(turn)},
    )
    app.cron_job(
        "report",
        image=IMAGE,
        schedule="0 * * * *",
        volumes={"/etc/turn": SecretVolume(turn)},
    )
    app.ingress(
        "relay",
        hosts=["relay.example.org"],
        routes=[Route(app.service(relay, port=8080), "/")],
        tls_secret=turn,
    )
    return app


def _objects(composition: DeploymentComposition) -> dict[tuple[str, str], dict]:
    return {
        (resource.ref.kind, resource.ref.name): resource.manifest
        for component in composition.components
        for resource in component.resources
    }


def _secret(composition: DeploymentComposition) -> tuple[str, dict]:
    (found,) = [
        (name, manifest)
        for (kind, name), manifest in _objects(composition).items()
        if kind == "Secret"
    ]
    return found


def test_a_versioned_secret_is_named_after_its_values_and_readers_follow():
    composition = _app(_ref("a")).render("relay")
    name, secret = _secret(composition)
    assert re.fullmatch(r"turn-[0-9a-f]{10}", name)
    assert secret["immutable"] is True
    assert versioned_logical(secret) == "turn"
    assert secret["metadata"]["annotations"][VERSIONED_SECRET_ANNOTATION] == "turn"
    objects = _objects(composition)
    for kind, workload in (
        ("Deployment", "relay"),
        ("CronJob", "report"),
        ("Ingress", "relay"),
    ):
        assert secret_names(objects[(kind, workload)]) == {name}, kind


def test_the_same_values_give_the_same_name_and_a_new_value_a_new_name():
    first, _ = _secret(_app(_ref("a")).render("relay"))
    again, _ = _secret(_app(_ref("a")).render("relay"))
    changed, _ = _secret(_app(_ref("b")).render("relay"))
    assert first == again
    assert changed != first
    pod = _objects(_app(_ref("b")).render("relay"))[("Deployment", "relay")]
    assert secret_names(pod) == {changed}


def test_keyed_tokens_rename_the_secret_and_its_readers_again():
    composition = _app(_ref("a")).render("relay")
    offline, _ = _secret(composition)
    keyed = content_digests(composition, lambda ref: "hmac:" + "f" * 64, refresh=True)
    name, _ = _secret(keyed)
    assert name != offline and name.startswith("turn-")
    objects = _objects(keyed)
    assert secret_names(objects[("Deployment", "relay")]) == {name}
    # Idempotent: the same tokens keep the same name.
    assert versioned_secret_names(keyed, lambda ref: "hmac:" + "f" * 64) == keyed


def test_the_name_never_holds_a_value():
    value = "s3cr3t-value"
    composition = _app(_ref("a")).render("relay")
    keyed = content_digests(composition, lambda ref: "hmac:" + value, refresh=True)
    name, _ = _secret(keyed)
    assert value not in name
    assert value not in json.dumps(list(_objects(keyed).values()))


def test_a_plain_secret_keeps_its_name():
    composition = _app(_ref("a"), versioned=False).render("relay")
    name, secret = _secret(composition)
    assert name == "turn"
    assert "immutable" not in secret


def test_a_preview_keeps_the_live_name_a_workload_mounts():
    live_composition = content_digests(
        _app(_ref("a")).render("relay"), lambda ref: "hmac:live", refresh=True
    )
    live_name, _ = _secret(live_composition)
    live = {
        resource.ref: resource.manifest
        for component in live_composition.components
        for resource in component.resources
    }
    preview = _app(_ref("c")).render("relay")
    carried = carry_live_secret_digests(preview, live)
    name, _ = _secret(carried)
    assert name == live_name
    assert secret_names(_objects(carried)[("Deployment", "relay")]) == {live_name}


def test_dependencies_on_the_secret_follow_the_new_name():
    composition = _app(_ref("a")).render("relay")
    name, secret = _secret(composition)
    secret_ref = next(
        resource.ref
        for component in composition.components
        for resource in component.resources
        if resource.ref.kind == "Secret"
    )
    extra = ResourceIntent.from_manifest(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "extra", "namespace": "relay"},
            "data": {},
        },
        [secret_ref],
    )
    from piceli.k8s.ops.plan import DeploymentComponent

    with_extra = DeploymentComposition(
        (*composition.components, DeploymentComponent("extra", (extra,)))
    )
    keyed = versioned_secret_names(with_extra, lambda ref: "hmac:x")
    new_name, _ = _secret(keyed)
    (dependent,) = [
        resource
        for component in keyed.components
        for resource in component.resources
        if resource.ref.name == "extra"
    ]
    assert [item.name for item in dependent.dependencies] == [new_name]


def test_every_kind_of_reference_follows_the_new_name():
    composition = _app(_ref("a")).render("relay")
    name, _ = _secret(composition)
    secret = next(
        resource
        for component in composition.components
        for resource in component.resources
        if resource.ref.kind == "Secret"
    )
    container = {
        "name": "raw",
        "image": IMAGE,
        "env": [
            {
                "name": "CONF",
                "valueFrom": {"secretKeyRef": {"name": name, "key": "turnserver.conf"}},
            }
        ],
        "envFrom": [{"secretRef": {"name": name}}],
    }
    raw = ResourceIntent.from_manifest(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "raw", "namespace": "relay"},
            "spec": {
                "selector": {"matchLabels": {"app": "raw"}},
                "template": {
                    "metadata": {"labels": {"app": "raw"}},
                    "spec": {
                        "containers": [container],
                        "initContainers": [dict(container, name="init")],
                        "imagePullSecrets": [{"name": name}],
                        "volumes": [
                            {"name": "file", "secret": {"secretName": name}},
                            {
                                "name": "projected",
                                "projected": {"sources": [{"secret": {"name": name}}]},
                            },
                        ],
                    },
                },
            },
        }
    )
    from piceli.k8s.ops.plan import DeploymentComponent

    both = DeploymentComposition(
        (DeploymentComponent("turn", (secret,)), DeploymentComponent("raw", (raw,)))
    )
    keyed = versioned_secret_names(both, lambda ref: "hmac:new")
    new_name, _ = _secret(keyed)
    assert new_name != name
    manifest = _objects(keyed)[("Deployment", "raw")]
    text = json.dumps(manifest)
    assert name not in text
    assert text.count(new_name) == 7
    assert secret_names(manifest) == {new_name}
