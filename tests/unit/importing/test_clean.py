"""Scrubbing server-populated fields and Kubernetes defaults."""

from __future__ import annotations

from piceli.importing.clean import (
    PRIVATE,
    is_default,
    pull_policy_default,
    scrub,
    strip_defaults,
)
from tests.unit.importing import objects


def test_scrub_removes_server_fields_and_secret_values() -> None:
    value = scrub(objects.secret(), "shop")
    assert value["data"] == {"password": PRIVATE}
    assert set(value["metadata"]) == {"name", "namespace"}
    claim = scrub(objects.claim(), "other")
    assert claim["metadata"] == {"name": "cache-data", "namespace": "other"}
    assert "status" not in claim


def test_scrub_keeps_user_annotations_and_finalizers() -> None:
    item = objects.config()
    item["metadata"]["annotations"] = {
        "team": "a",
        "kubectl.kubernetes.io/last-applied-configuration": "{}",
        "piceli.io/owner": "x",
    }
    item["metadata"]["finalizers"] = [
        "example.com/keep",
        "kubernetes.io/pvc-protection",
    ]
    value = scrub(item)
    assert value["metadata"]["annotations"] == {"team": "a"}
    assert value["metadata"]["finalizers"] == ["example.com/keep"]


def test_string_data_is_scrubbed_into_data() -> None:
    item = objects.secret()
    item["stringData"] = {"url": "redis://:pw@cache"}
    value = scrub(item)
    assert value["data"] == {"password": PRIVATE, "url": PRIVATE}
    assert "stringData" not in value


def test_strip_defaults() -> None:
    deployment = strip_defaults(scrub(objects.deployment()))
    spec = deployment["spec"]
    assert "progressDeadlineSeconds" not in spec and "strategy" not in spec
    assert "replicas" not in spec
    pod = spec["template"]["spec"]
    assert set(pod) == {"containers", "initContainers", "volumes", "tolerations"}
    redis = pod["containers"][0]
    assert "terminationMessagePath" not in redis and "imagePullPolicy" not in redis
    assert redis["readinessProbe"] == {"tcpSocket": {"port": 6379}, "periodSeconds": 5}
    assert pod["volumes"][2] == {"name": "conf", "configMap": {"name": "settings"}}
    service = strip_defaults(scrub(objects.service()))
    assert service["spec"] == {"ports": [{"port": 6379}], "selector": {"app": "cache"}}


def test_headless_service_keeps_cluster_ip_none() -> None:
    item = objects.service()
    item["spec"]["clusterIP"] = "None"
    item["spec"]["clusterIPs"] = ["None"]
    assert strip_defaults(item)["spec"]["clusterIP"] == "None"


def test_pull_policy_default() -> None:
    assert pull_policy_default("redis") == "Always"
    assert pull_policy_default("redis:latest") == "Always"
    assert pull_policy_default("redis:7") == "IfNotPresent"
    assert pull_policy_default("host:5000/redis") == "Always"
    assert pull_policy_default("redis@sha256:" + "0" * 64) == "IfNotPresent"


def test_is_default_depends_on_the_parent() -> None:
    assert is_default("Service", ("spec", "ports", "*", "targetPort"), 80, {"port": 80})
    assert not is_default(
        "Service", ("spec", "ports", "*", "targetPort"), 81, {"port": 80}
    )
    assert is_default("NetworkPolicy", ("spec", "policyTypes"), ["Ingress"], {})
    assert not is_default(
        "NetworkPolicy", ("spec", "policyTypes"), ["Ingress"], {"egress": []}
    )
