"""Workloads roll when a ConfigMap or Secret they read changes (content digests)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from piceli.app import App
from piceli.app.model import ConfigVolume, SecretVolume
from piceli.k8s.ops.content_digest import (
    CONFIG_DIGEST_ANNOTATION,
    ROLL_ON_CHANGE_ANNOTATION,
    SECRET_DIGEST_ANNOTATION,
    carry_live_secret_digests,
    content_digests,
    pod_references,
)
from piceli.k8s.ops.discovery import PlanTarget
from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    ResourceIntent,
)
from piceli.k8s.ops.secret_versions import SecretVersionRef, SecretVersionStore

IMAGE = "registry.example/app@sha256:" + "a" * 64


def _ref(version: str) -> SecretVersionRef:
    return SecretVersionRef("1" * 32, version * 32)


def _app(
    data: dict[str, str] | None = None,
    password: SecretVersionRef | None = None,
    **workload: Any,
) -> App:
    app = App("shop")
    config = app.config("settings", data or {"mode": "fast"})
    secret = app.secret("auth", {"password": password or _ref("a")})
    app.deployment(
        "api",
        image=IMAGE,
        env={"PASSWORD": secret.key("password")},
        volumes={"/etc/app": ConfigVolume(config)},
        **workload,
    )
    app.stateful_set("db", image=IMAGE, volumes={"/run/auth": SecretVolume(secret)})
    app.daemon_set("agent", image=IMAGE, env={"MODE": config.key("mode")})
    app.job("migrate", image=IMAGE, env={"PASSWORD": secret.key("password")})
    app.cron_job(
        "report",
        image=IMAGE,
        schedule="0 * * * *",
        volumes={"/c": ConfigVolume(config)},
    )
    app.deployment("plain", image=IMAGE)
    return app


def _manifests(composition: DeploymentComposition) -> dict[str, dict[str, Any]]:
    return {
        f"{resource.ref.kind}/{resource.ref.name}": resource.manifest
        for component in composition.components
        for resource in component.resources
    }


def _template_annotations(manifest: dict[str, Any]) -> dict[str, str]:
    spec = manifest["spec"]
    template = spec.get("template") or spec["jobTemplate"]["spec"]["template"]
    return dict(template["metadata"].get("annotations") or {})


def test_every_rolled_kind_carries_the_digest_of_what_it_reads():
    rendered = _manifests(_app().render("shop"))
    api = _template_annotations(rendered["Deployment/api"])
    assert set(api) == {CONFIG_DIGEST_ANNOTATION, SECRET_DIGEST_ANNOTATION}
    assert set(_template_annotations(rendered["StatefulSet/db"])) == {
        SECRET_DIGEST_ANNOTATION
    }
    assert set(_template_annotations(rendered["DaemonSet/agent"])) == {
        CONFIG_DIGEST_ANNOTATION
    }
    # Jobs start new pods every run; a Job's template is immutable.
    assert _template_annotations(rendered["Job/migrate"]) == {}
    assert _template_annotations(rendered["CronJob/report"]) == {}
    # Reads nothing of the app: no annotation, no rollout.
    assert _template_annotations(rendered["Deployment/plain"]) == {}
    for value in api.values():
        assert value.startswith("sha256:") and len(value) == len("sha256:") + 64


def test_digest_is_stable_for_the_same_content():
    first = _manifests(_app().render("shop"))
    second = _manifests(_app().render("shop"))
    assert first == second
    # Rendering an already annotated composition again changes nothing.
    again = content_digests(_app().render("shop"))
    assert _manifests(again) == first


def test_a_changed_config_changes_only_its_consumers():
    before = _manifests(_app().render("shop"))
    after = _manifests(_app(data={"mode": "slow"}).render("shop"))
    for name in ("Deployment/api", "DaemonSet/agent"):
        assert (
            _template_annotations(before[name])[CONFIG_DIGEST_ANNOTATION]
            != _template_annotations(after[name])[CONFIG_DIGEST_ANNOTATION]
        )
    assert before["StatefulSet/db"] == after["StatefulSet/db"]
    assert (
        _template_annotations(before["Deployment/api"])[SECRET_DIGEST_ANNOTATION]
        == _template_annotations(after["Deployment/api"])[SECRET_DIGEST_ANNOTATION]
    )


def test_a_new_secret_version_changes_the_secret_digest():
    before = _manifests(_app(password=_ref("a")).render("shop"))
    after = _manifests(_app(password=_ref("b")).render("shop"))
    for name in ("Deployment/api", "StatefulSet/db"):
        assert (
            _template_annotations(before[name])[SECRET_DIGEST_ANNOTATION]
            != _template_annotations(after[name])[SECRET_DIGEST_ANNOTATION]
        )
    assert before["DaemonSet/agent"] == after["DaemonSet/agent"]


def test_opting_out_renders_a_marker_and_no_digest():
    rendered = _manifests(_app(roll_on_change=False).render("shop"))
    api = rendered["Deployment/api"]
    assert api["metadata"]["annotations"] == {ROLL_ON_CHANGE_ANNOTATION: "false"}
    assert _template_annotations(api) == {}
    # The others keep theirs.
    assert _template_annotations(rendered["StatefulSet/db"])


@pytest.mark.parametrize("method", ["stateful_set", "daemon_set"])
def test_opt_out_on_stateful_and_daemon_sets(method):
    app = App("shop")
    config = app.config("settings", {"mode": "fast"})
    getattr(app, method)(
        "worker", image=IMAGE, env={"MODE": config.key("mode")}, roll_on_change=False
    )
    (manifest,) = (
        item
        for key, item in _manifests(app.render("shop")).items()
        if key.endswith("/worker") and not key.startswith("Service/")
    )
    assert _template_annotations(manifest) == {}


def test_the_annotation_never_holds_a_secret_value():
    value = "correct-horse-battery-staple"
    target = PlanTarget("cluster", "shop")

    def fingerprint(store: SecretVersionStore):
        return lambda ref: store.fingerprint(target, ref)

    def annotated(store: SecretVersionStore, ref: SecretVersionRef) -> dict:
        composition = content_digests(
            _app(password=ref).render("shop"), fingerprint(store)
        )
        return _manifests(composition)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        store = SecretVersionStore(Path(directory) / "private" / "secrets.sqlite")
        try:
            first = store.put(target, value)
            again = store.put(target, value)  # a carried value: a new version
            other = store.put(target, value + "!")
            assert first != again
            one = annotated(store, first)
            text = json.dumps(one)
            assert value not in text
            assert store.fingerprint(target, first) not in text
            # Same value under a new version: same digest, nothing rolls.
            assert one == annotated(store, again)
            # A rotated value rolls its consumers.
            changed = annotated(store, other)
            assert _template_annotations(
                changed["Deployment/api"]
            ) != _template_annotations(one["Deployment/api"])
            assert store.fingerprint(target, first).startswith("hmac-sha256:")
        finally:
            store.close()


def _workload(pod: dict[str, Any], **metadata: Any) -> ResourceIntent:
    return ResourceIntent.from_manifest(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "raw", "namespace": "shop", **metadata},
            "spec": {
                "selector": {"matchLabels": {"app": "raw"}},
                "template": {"metadata": {"labels": {"app": "raw"}}, "spec": pod},
            },
        }
    )


def _object(kind: str, name: str, data: dict[str, str]) -> ResourceIntent:
    return ResourceIntent.from_manifest(
        {
            "apiVersion": "v1",
            "kind": kind,
            "metadata": {"name": name, "namespace": "shop"},
            "data": data,
        }
    )


def test_raw_compositions_cover_projected_volumes_and_env_from():
    pod = {
        "containers": [
            {
                "name": "main",
                "image": IMAGE,
                "envFrom": [{"secretRef": {"name": "env-secret"}}],
            }
        ],
        "initContainers": [
            {
                "name": "init",
                "image": IMAGE,
                "envFrom": [{"configMapRef": {"name": "env-config"}}],
            }
        ],
        "volumes": [
            {
                "name": "bundle",
                "projected": {
                    "sources": [
                        {"secret": {"name": "projected-secret"}},
                        {"configMap": {"name": "projected-config"}},
                        {"serviceAccountToken": {"path": "token"}},
                    ]
                },
            }
        ],
    }
    assert pod_references(pod) == {
        ("Secret", "env-secret"),
        ("ConfigMap", "env-config"),
        ("Secret", "projected-secret"),
        ("ConfigMap", "projected-config"),
    }
    secret = _object("Secret", "projected-secret", {"key": "<private>"}).with_secret(
        "/data/key", _ref("c")
    )
    composition = DeploymentComposition(
        (
            DeploymentComponent(
                "raw",
                (
                    _workload(pod),
                    secret,
                    _object("ConfigMap", "env-config", {"a": "1"}),
                ),
            ),
        )
    )
    manifest = _manifests(content_digests(composition))["Deployment/raw"]
    # env-secret and projected-config are not in the composition: not ours.
    assert set(_template_annotations(manifest)) == {
        CONFIG_DIGEST_ANNOTATION,
        SECRET_DIGEST_ANNOTATION,
    }
    opted_out = DeploymentComposition(
        (
            DeploymentComponent(
                "raw",
                (
                    _workload(pod, annotations={ROLL_ON_CHANGE_ANNOTATION: "false"}),
                    secret,
                ),
            ),
        )
    )
    assert (
        _template_annotations(_manifests(content_digests(opted_out))["Deployment/raw"])
        == {}
    )


def test_a_preview_keeps_the_live_secret_digest():
    rendered = _app().render("shop")
    resource = next(
        item
        for component in rendered.components
        for item in component.resources
        if item.ref.kind == "Deployment" and item.ref.name == "api"
    )
    live = resource.manifest
    live["spec"]["template"]["metadata"]["annotations"][SECRET_DIGEST_ANNOTATION] = (
        "sha256:" + "f" * 64
    )
    carried = _manifests(carry_live_secret_digests(rendered, {resource.ref: live}))
    api = _template_annotations(carried["Deployment/api"])
    assert api[SECRET_DIGEST_ANNOTATION] == "sha256:" + "f" * 64
    # The ConfigMap digest is exact: it is never carried.
    assert (
        api[CONFIG_DIGEST_ANNOTATION]
        == _template_annotations(_manifests(rendered)["Deployment/api"])[
            CONFIG_DIGEST_ANNOTATION
        ]
    )


def test_the_annotations_are_public_fields():
    """Redaction leaves them alone: the executor would refuse a redacted field."""
    for component in _app().render("shop").components:
        for resource in component.resources:
            if resource.ref.kind in ("Deployment", "StatefulSet", "DaemonSet"):
                assert resource.redacted_manifest() == resource.manifest
