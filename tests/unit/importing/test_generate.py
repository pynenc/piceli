"""The generated module is typed, exact, ruff-clean and never holds a secret."""

from __future__ import annotations

import copy
from types import MappingProxyType
from typing import Any

import pytest

from piceli.app.render import placeholder_inputs
from piceli.importing import ImportFailure
from piceli.importing.clean import scrub, strip_defaults
from piceli.importing.generate import SAME, diff, generate
from piceli.k8s.release_spec import ReleaseContext
from tests.unit.importing import objects
from tests.unit.importing.objects import NS, SECRET_VALUE


def _generate(items: list[dict[str, Any]], **kwargs: Any):
    return generate(
        [scrub(item, NS) for item in items],
        app_name="shop",
        namespace=NS,
        source="live",
        origin="namespace shop",
        **kwargs,
    )


def _render(result) -> dict[tuple[str, str], dict[str, Any]]:
    namespace: dict[str, Any] = {}
    exec(compile(result.module, "app.py", "exec"), namespace)
    ctx = ReleaseContext(
        namespace=NS,
        images=MappingProxyType({}),
        secrets=MappingProxyType(
            placeholder_inputs([item.input for item in result.secrets])
        ),
    )
    composition = namespace["build"](ctx)
    return {
        (resource.ref.kind, resource.ref.name): resource
        for component in composition.components
        for resource in component.resources
    }


def test_stack_is_typed_exact_and_ruff_clean(ruff_clean) -> None:
    result = _generate(objects.stack())
    ruff_clean(result.module)
    modes = {(item.kind, item.name): item.mode for item in result.objects}
    assert modes == {
        ("ConfigMap", "settings"): "typed",
        ("Secret", "cache-auth"): "typed",
        ("Deployment", "cache"): "typed",
        ("Service", "cache"): "typed",
        ("NetworkPolicy", "cache-ingress"): "typed",
        ("PersistentVolumeClaim", "cache-data"): "existing-claim",
    }
    rendered = _render(result)
    # every imported field, minus server-populated fields and defaults
    for item in objects.stack():
        if item["kind"] == "PersistentVolumeClaim":
            assert (item["kind"], item["metadata"]["name"]) not in rendered
            continue
        target = strip_defaults(scrub(item, NS))
        manifest = rendered[(item["kind"], item["metadata"]["name"])].manifest
        assert diff(manifest, target, item["kind"]) is SAME


def test_typed_declarations_and_one_comment_per_untyped_field() -> None:
    module = _generate(objects.stack()).module
    for fragment in (
        "settings = app.config(",
        "cache_auth = app.secret(",
        '"password": ctx.secret("cache-auth-password")',
        'container="redis"',
        '"MODE": settings.key("mode")',
        '"PASSWORD": cache_auth.key("password")',
        "ready=app.probe.tcp(6379, period_seconds=5)",
        'Resources(cpu="100m", memory="64Mi", memory_limit="128Mi")',
        '"/data": ExistingClaim("cache-data", name="data")',
        '"/etc/redis": Mount(ConfigVolume(settings, name="conf"), read_only=True)',
        'ContainerPort(port=9121, name="metrics")',
        "init=[",
        'selector={"app": "cache"}',
        "app.network_policy(cache, allow_from=[cache], ports=[6379])",
        "# not typed: spec.template.spec.tolerations",
        "# not typed: spec.template.spec.containers[redis].securityContext",
        "# not typed: spec.template.spec.containers[redis].env (order)",
        "# not typed: spec.template.spec.containers[redis].resources.limits.example.com/gpu",
        "# not typed: metadata.labels",
    ):
        assert fragment in module, fragment
    assert module.count("    app.override(") == 3  # clear the order, the rest; service


def test_secret_values_never_reach_the_module() -> None:
    result = _generate(objects.stack())
    assert SECRET_VALUE not in result.module
    assert "super-secret" not in result.module
    assert [(s.input, s.secret, s.key) for s in result.secrets] == [
        ("cache-auth-password", "cache-auth", "password")
    ]
    assert '[secrets.cache-auth-password]\n    type = "import"' in result.module


def test_unscrubbed_secrets_are_refused() -> None:
    with pytest.raises(ValueError, match="scrubbed"):
        generate(
            [objects.secret()],
            app_name="shop",
            namespace=NS,
            source="yaml",
            origin="x",
        )


def test_output_is_deterministic() -> None:
    first = _generate(objects.stack())
    second = _generate(list(reversed(objects.stack())))
    assert first.module == second.module
    assert first.sha256 == second.sha256


def test_env_order_the_model_cannot_reach_is_cleared_then_set() -> None:
    result = _generate([objects.deployment()])
    module = result.module
    # CPU (untyped) sits before LEVEL (typed): the env list is replaced.
    assert '"env": None' in module
    assert "spec.template.spec.containers[redis].env (order)" in module


def test_other_kinds_are_kept_raw_and_service_without_workload_too(ruff_clean) -> None:
    orphan = objects.service()
    orphan["metadata"]["name"] = "orphan"
    orphan["spec"]["selector"] = {"app": "elsewhere"}
    result = _generate([objects.stateful_set(), orphan, objects.token_secret()])
    ruff_clean(result.module)
    modes = {
        (item.kind, item.name): (item.mode, item.reason) for item in result.objects
    }
    assert modes[("StatefulSet", "queue")] == ("raw", "StatefulSet is not typed yet")
    assert modes[("Service", "orphan")][0] == "raw"
    assert "    DeploymentComponent,\n" in result.module
    assert [(s.kind, s.name) for s in result.skipped] == [("Secret", "default-token")]
    assert SECRET_VALUE not in result.module
    rendered = _render(result)
    assert ("StatefulSet", "queue") in rendered


def test_values_the_typed_model_rejects_fall_back_to_raw() -> None:
    item = objects.config()
    item["data"] = {"bad key!": "x"}  # not a valid ConfigMap key for the model
    result = _generate([item])
    assert result.objects[0].mode == "raw"
    assert "the typed model rejected it" in (result.objects[0].reason or "")


def test_app_labels_shared_by_every_object() -> None:
    items = [objects.config(), objects.deployment()]
    for item in items:
        item["metadata"]["labels"] = {
            "app.kubernetes.io/part-of": "shop",
            **(item["metadata"].get("labels") or {}),
        }
    items[1]["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/part-of"] = (
        "shop"
    )
    module = _generate(items).module
    assert 'app = App("shop")\n' in module


def test_named_list_reorder_uses_two_overrides() -> None:
    item = objects.deployment()
    pod = item["spec"]["template"]["spec"]
    pod["volumes"] = list(reversed(pod["volumes"]))
    result = _generate([item])
    rendered = _render(result)
    target = strip_defaults(scrub(item, NS))
    assert (
        diff(rendered[("Deployment", "cache")].manifest, target, "Deployment") is SAME
    )


def test_diff_accepts_defaults_the_model_renders() -> None:
    rendered = {"spec": {"ports": [{"port": 80, "targetPort": 80, "protocol": "TCP"}]}}
    target = {"spec": {"ports": [{"port": 80}]}}
    assert diff(rendered, target, "Service") is SAME
    notes: list[str] = []
    patch = diff({"spec": {"type": "NodePort"}}, {"spec": {}}, "Service", notes=notes)
    assert patch == {"spec": {"type": None}} and notes == ["spec.type (removed)"]


def test_roundtrip_mismatch_is_refused(monkeypatch) -> None:
    import importlib

    module = importlib.import_module("piceli.importing.generate")

    real = module.diff
    calls = {"count": 0}

    def broken(*args, **kwargs):
        calls["count"] += 1
        result = real(*args, **kwargs)
        return result if calls["count"] <= 1 else {"broken": True}

    monkeypatch.setattr(module, "diff", broken)
    with pytest.raises(ImportFailure) as raised:
        _generate([objects.config()])
    assert raised.value.code == "import-roundtrip-mismatch"


def test_input_is_not_modified() -> None:
    items = [scrub(item, NS) for item in objects.stack()]
    before = copy.deepcopy(items)
    generate(items, app_name="shop", namespace=NS, source="live", origin="x")
    assert items == before


def test_string_data_must_be_scrubbed_too() -> None:
    item = scrub(objects.secret(), NS)
    item["stringData"] = {"url": "redis://:pw@cache"}
    with pytest.raises(ValueError, match="scrubbed"):
        generate([item], app_name="shop", namespace=NS, source="yaml", origin="x")


def _kitchen_sink() -> list[dict[str, Any]]:
    deployment = objects.deployment()
    pod = deployment["spec"]["template"]["spec"]
    pod["serviceAccountName"] = "cache-runner"
    pod["serviceAccount"] = "cache-runner"
    pod["shareProcessNamespace"] = True
    deployment["spec"]["replicas"] = 3
    deployment["spec"]["strategy"] = {"type": "Recreate"}
    deployment["spec"]["template"]["metadata"]["labels"]["tier"] = "data"
    redis = pod["containers"][0]
    redis["env"] = [redis["env"][0], redis["env"][1], redis["env"][3]]
    redis["livenessProbe"] = {
        "httpGet": {"path": "/health", "port": "metrics", "scheme": "HTTP"},
        "initialDelaySeconds": 0,
        "timeoutSeconds": 1,
    }
    redis["startupProbe"] = {"exec": {"command": ["redis-cli", "ping"]}}
    redis["volumeMounts"] = [
        {"name": "data", "mountPath": "/data", "readOnly": True},
        {"name": "conf", "mountPath": "/etc/redis/redis.conf", "subPath": "redis.conf"},
        {"name": "auth", "mountPath": "/run/auth", "readOnly": True},
        {"name": "scratch", "mountPath": "/scratch"},
        {"name": "host", "mountPath": "/host"},
    ]
    pod["volumes"] = [
        {
            "name": "data",
            "persistentVolumeClaim": {"claimName": "cache-data", "readOnly": True},
        },
        {
            "name": "conf",
            "configMap": {
                "name": "settings",
                "items": [{"key": "redis.conf", "path": "redis.conf"}],
                "defaultMode": 0o440,
            },
        },
        {"name": "auth", "secret": {"secretName": "cache-auth", "defaultMode": 0o400}},
        {"name": "scratch", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}},
        {"name": "host", "hostPath": {"path": "/var/lib/host"}},
    ]
    service = objects.service()
    service["spec"]["type"] = "NodePort"
    service["spec"]["externalTrafficPolicy"] = "Cluster"
    service["spec"]["ports"] = [
        {
            "name": "redis",
            "port": 6379,
            "targetPort": 6379,
            "nodePort": 30079,
            "protocol": "TCP",
        },
        {"name": "metrics", "port": 9121, "targetPort": "metrics", "protocol": "TCP"},
    ]
    policy = objects.policy()
    policy["spec"]["ingress"] = [{}]
    return [deployment, service, objects.config(), objects.secret(), policy]


def test_kitchen_sink_round_trips(ruff_clean) -> None:
    items = _kitchen_sink()
    result = _generate(items)
    ruff_clean(result.module)
    rendered = _render(result)
    for item in items:
        target = strip_defaults(scrub(item, NS))
        key = (item["kind"], item["metadata"]["name"])
        assert diff(rendered[key].manifest, target, item["kind"]) is SAME, key
    module = result.module
    for fragment in (
        "replicas=3",
        'strategy="Recreate"',
        'service_account="cache-runner"',
        "share_process_namespace=True",
        'labels={"tier": "data"}',
        'live=app.probe.http("/health", "metrics", initial_delay_seconds=0)',
        'startup=app.probe.exec(["redis-cli", "ping"])',
        '"/data": ExistingClaim("cache-data", name="data", read_only=True)',
        'SecretVolume(cache_auth, name="auth", default_mode=0o400)',
        '"/scratch": MemoryVolume(size_limit="64Mi")',
        'sub_path="redis.conf"',
        'items={"redis.conf": "redis.conf"}',
        'ServicePort(port=9121, target_port="metrics", name="metrics")',
        'type="NodePort"',
        "# not typed: spec.template.spec.volumes[host]",
    ):
        assert fragment in module, fragment
