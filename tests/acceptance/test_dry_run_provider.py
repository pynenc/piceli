"""Acceptance: ``KubernetesProvider.preview_update`` is a side-effect-free dry run."""

from __future__ import annotations

import json

import pytest

from piceli.k8s.ops.discovery import ResourceIdentity
from piceli.k8s.ops.dry_run import update_body
from piceli.k8s.ops.kubernetes_provider import ProviderError
from piceli.k8s.ops.plan import ResourceIntent
from tests.acceptance.fake_api import TARGET, manifest


def _stored(api, provider):
    api.server_defaults = True
    api.put(manifest("Deployment", "worker"), owned=True)
    identity = ResourceIdentity("apps/v1", "Deployment", TARGET.namespace, "worker")
    current = provider.get(identity)
    assert current is not None
    return current


def test_preview_update_returns_the_defaulted_object_and_persists_nothing(local_api):
    api, provider = local_api
    current = _stored(api, provider)
    desired = manifest("Deployment", "worker")
    desired["spec"]["template"]["spec"]["containers"][0]["image"] = (
        "example.invalid/w:2"
    )
    body = update_body(
        ResourceIntent.from_manifest(desired), current, "acceptance-owner"
    )
    before = json.dumps(api.objects[("Deployment", "worker")], sort_keys=True)
    api.requests.clear()

    answer = provider.preview_update(current, body)

    container = answer["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "example.invalid/w:2"
    assert container["terminationMessagePath"] == "/dev/termination-log"
    (request,) = api.requests
    assert request["method"] == "PATCH"
    assert request["query"]["dryRun"] == ["All"]
    assert request["content_type"] == "application/merge-patch+json"
    assert json.dumps(api.objects[("Deployment", "worker")], sort_keys=True) == before


def test_preview_update_refuses_stale_or_unmanaged_input(local_api):
    api, provider = local_api
    current = _stored(api, provider)
    body = update_body(
        ResourceIntent.from_manifest(manifest("Deployment", "worker")),
        current,
        "acceptance-owner",
    )
    stale = json.loads(json.dumps(body))
    stale["metadata"]["resourceVersion"] = "1"
    with pytest.raises(ProviderError) as error:
        provider.preview_update(current, stale)
    assert error.value.category == "uid-version-precondition-failed"

    api.inject(
        "PATCH",
        "/deployments/worker",
        raw=json.dumps({"apiVersion": "apps/v1", "kind": "Deployment"}).encode(),
        dry_run=True,
    )
    with pytest.raises(ProviderError) as error:
        provider.preview_update(current, body)
    assert error.value.category == "invalid-dry-run-response"
    assert not error.value.ambiguous
