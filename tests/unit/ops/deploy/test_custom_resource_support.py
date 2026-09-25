"""Engine support for custom resources: redaction of Secret references, readiness."""

from __future__ import annotations

from typing import Any

import pytest

from piceli.k8s.ops.discovery import (
    PUBLIC_FIELDS_ANNOTATION,
    DiscoveredResource,
    ResourceScope,
    public_manifest,
)
from tests.unit.ops.deploy.test_kubernetes_provider import provider

REDACTED = "<redacted>"


def _certificate(**spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": {"name": "shop-tls", "namespace": "shop"},
        "spec": spec,
    }


def test_secret_references_are_public_but_values_are_not():
    manifest = _certificate(
        secretName="shop-tls",
        secretTemplate={"labels": {"a": "b"}, "annotations": {"c": "d"}},
        keystores={
            "jks": {
                "create": True,
                "passwordSecretRef": {"name": "jks", "key": "password"},
                "password": "literal-value",
            }
        },
        bearerTokenFile="/var/run/token",
        tokenPath="/var/run/other",
        clientSecret={"name": "oauth", "key": "secret", "optional": False},
        authToken={"name": "creds", "key": "token", "extra": "no"},
        apiToken="plain-value",
    )
    public, changed = public_manifest(manifest)
    spec = public["spec"]
    assert changed
    assert spec["secretName"] == "shop-tls"
    assert spec["secretTemplate"] == {"labels": {"a": "b"}, "annotations": {"c": "d"}}
    assert spec["keystores"]["jks"]["passwordSecretRef"] == {
        "name": "jks",
        "key": "password",
    }
    assert spec["bearerTokenFile"] == "/var/run/token"
    assert spec["tokenPath"] == "/var/run/other"
    assert spec["clientSecret"]["name"] == "oauth"
    # Values stay redacted: a literal password, a non-selector object, a token.
    assert spec["keystores"]["jks"]["password"] == REDACTED
    assert spec["authToken"] == dict.fromkeys(("name", "key", "extra"), REDACTED)
    assert spec["apiToken"] == REDACTED


def test_declared_public_fields_are_shown_and_nothing_else():
    manifest = _certificate(
        privateKey={"algorithm": "ECDSA", "size": 256},
        password="literal-value",
        items=[{"token": "a"}, {"token": "b"}],
    )
    assert public_manifest(manifest)[0]["spec"]["privateKey"] == {
        "algorithm": REDACTED,
        "size": REDACTED,
    }
    manifest["metadata"]["annotations"] = {
        PUBLIC_FIELDS_ANNOTATION: "spec.privateKey, spec.items.*.token"
    }
    spec = public_manifest(manifest)[0]["spec"]
    assert spec["privateKey"] == {"algorithm": "ECDSA", "size": 256}
    assert spec["items"] == [{"token": "a"}, {"token": "b"}]
    assert spec["password"] == REDACTED
    # A Secret's data is never made public by the annotation.
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "s",
            "annotations": {PUBLIC_FIELDS_ANNOTATION: "data.password"},
        },
        "data": {"password": "c2VjcmV0"},
    }
    assert public_manifest(secret)[0]["data"] == {"password": REDACTED}


def _discovered(manifest: dict[str, Any]) -> DiscoveredResource:
    metadata = {"uid": "uid", "resourceVersion": "1", **manifest["metadata"]}
    return DiscoveredResource.from_manifest(
        {**manifest, "metadata": metadata}, scope=ResourceScope.NAMESPACED
    )


@pytest.mark.parametrize(
    ("status", "generation", "expected"),
    [
        (None, 1, "ready"),  # nothing reports progress: applied is ready
        ({}, 1, "ready"),
        ({"conditions": [{"type": "Ready", "status": "True"}]}, 1, "ready"),
        ({"conditions": [{"type": "Ready", "status": "False"}]}, 1, "not-ready"),
        ({"conditions": [{"type": "Issuing", "status": "True"}]}, 1, "ready"),
        (
            {
                "observedGeneration": 1,
                "conditions": [{"type": "Ready", "status": "True"}],
            },
            2,
            "not-ready",
        ),
    ],
)
def test_custom_resources_follow_the_ready_condition(status, generation, expected):
    manifest = _certificate(secretName="shop-tls")
    manifest["metadata"] = {**manifest["metadata"], "generation": generation}
    if status is not None:
        manifest["status"] = status
    result = provider([]).readiness(_discovered(manifest))
    assert result.status.value == expected


def test_core_kinds_without_a_rule_stay_unsupported():
    quota = {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": "api", "namespace": "shop"},
    }
    assert provider([]).readiness(_discovered(quota)).status.value == "unsupported"
