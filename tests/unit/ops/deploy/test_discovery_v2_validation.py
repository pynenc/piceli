"""Authority claims must fail identically at wire and constructor boundaries."""

import copy
import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from piceli.k8s.ops.bounds import bounded_call
from piceli.k8s.ops.discovery import (
    DiscoveredResource,
    DiscoveryArtifact,
    DiscoveryFailureKind,
    DiscoveryLimits,
    DiscoveryPage,
    DiscoveryProvenance,
    DiscoveryRequest,
    EvidenceSource,
    PlanTarget,
    ResourceScope,
    capture_discovery,
    public_manifest,
)

FIXTURE = Path(__file__).parents[3] / "fixtures/discovery-v2/complete.json"


def fixture():
    return json.loads(FIXTURE.read_text())


@pytest.mark.parametrize(
    "path,value",
    [
        (("captured_at",), "not-a-timestamp"),
        (("captured_at",), "2026-09-09T00:00:00"),
        (("captured_at",), "2999-01-01T00:00:00Z"),
        (("schema_version",), True),
        (("schema_version",), "2"),
        (("coverage", "api_resources"), []),
        (("coverage", "completed"), [{"api_version": "v1", "kind": "Secret"}]),
        (("coverage", "provider_id"), 123),
        (("coverage", "requested"), []),
        (("limits", "max_resources"), True),
        (("limits", "max_resources"), "100"),
        (("limits", "max_resource_types"), 0),
        (("limits", "max_artifact_bytes"), 10),
        (("limits", "call_seconds"), 1000),
        (("provenance", "source"), "live"),
        (("provenance", "namespace_uid"), 1),
        (("resources", 0, "identity", "name"), "different"),
        (("resources", 0, "identity", "namespace"), "outside"),
        (("resources", 0, "manifest", "metadata", "uid"), ""),
        (("resources", 0, "manifest", "metadata", "resourceVersion"), 7),
        (("resources", 0, "content_complete"), "false"),
        (("resources", 0, "retained"), "false"),
        (("resources", 0, "retained"), None),
        (("resources", 0, "manifest", "metadata", "annotations"), []),
        (("resources", 0, "manifest", "metadata", "ownerReferences"), [{"uid": ""}]),
        (("coverage", "api_resources", 0, "scope"), "cluster"),
        (("coverage", "api_resources", 0, "plural"), "deployments/status"),
        (("defaulted_fields", 0, "json_pointer"), "/spec/bad~escape"),
        (("defaulted_fields", 0, "json_pointer"), "/metadata/uid"),
        (("defaulted_fields", 0, "json_pointer"), "/spec/absent"),
        (("defaulted_fields", 0, "resource", "name"), "absent"),
    ],
)
def test_malformed_portable_authority_is_rejected(path, value):
    data = fixture()
    current = data
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = value
    with pytest.raises(ValueError):
        DiscoveryArtifact.from_dict(data)
    with pytest.raises(ValueError):
        DiscoveryArtifact.from_json(json.dumps(data))


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("target",),
        ("limits",),
        ("coverage",),
        ("provenance",),
        ("resources", 0),
        ("resources", 0, "identity"),
        ("coverage", "requested", 0),
        ("defaulted_fields", 0),
    ],
)
def test_unknown_contract_fields_are_rejected(path):
    data = fixture()
    current = data
    for key in path:
        current = current[key]
    current["unknown"] = "forged"
    with pytest.raises(ValueError):
        DiscoveryArtifact.from_dict(data)


@pytest.mark.parametrize(
    "path",
    [
        ("coverage", "requested"),
        ("coverage", "completed"),
        ("coverage", "api_resources"),
        ("resources",),
        ("defaulted_fields",),
    ],
)
def test_duplicate_entries_are_rejected(path):
    data = fixture()
    current = data
    for key in path:
        current = current[key]
    current.append(copy.deepcopy(current[0]))
    with pytest.raises(ValueError):
        DiscoveryArtifact.from_dict(data)


def test_raw_input_byte_budget_cannot_be_bypassed_with_whitespace():
    data = fixture()
    data["limits"]["max_artifact_bytes"] = 5000
    encoded = json.dumps(data) + " " * 5000
    with pytest.raises(ValueError, match="byte"):
        DiscoveryArtifact.from_json(encoded)
    with pytest.raises(ValueError, match="byte"):
        DiscoveryArtifact.from_json(json.dumps(data), max_bytes=10)


@pytest.mark.parametrize(
    "raw", ['{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}', "[" * 1500 + "]" * 1500]
)
def test_noncanonical_or_deep_json_is_rejected(raw):
    with pytest.raises(ValueError):
        DiscoveryArtifact.from_json(raw)


def test_direct_constructor_cannot_bypass_authority_checks():
    artifact = DiscoveryArtifact.from_dict(fixture())
    with pytest.raises(ValueError, match="API metadata"):
        replace(artifact.coverage, api_resources=())
    with pytest.raises(ValueError, match="timestamp"):
        replace(artifact, captured_at="bad")
    with pytest.raises(ValueError, match="namespace"):
        replace(artifact, target=PlanTarget("cluster", "outside"))
    with pytest.raises(ValueError):
        DiscoveryLimits(max_resources=True)
    with pytest.raises(ValueError):
        replace(artifact.resources[0], content_complete="true")
    with pytest.raises(ValueError):
        DiscoveryProvenance(EvidenceSource.LIVE, "endpoint")
    assert not artifact.execution_authoritative


def test_secret_references_remain_public_but_values_and_lookalikes_are_redacted():
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "worker"},
        "spec": {
            "volumes": [{"name": "auth", "secret": {"secretName": "credentials"}}],
            "containers": [
                {
                    "name": "worker",
                    "env": [
                        {
                            "name": "PASSWORD",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": "credentials",
                                    "key": "password",
                                }
                            },
                        },
                        {"name": "PASSWORD", "value": "never-public"},
                    ],
                }
            ],
        },
    }
    redacted, changed = public_manifest(pod)
    assert changed
    assert redacted["spec"]["volumes"] == pod["spec"]["volumes"]
    env = redacted["spec"]["containers"][0]["env"]
    assert env[0] == pod["spec"]["containers"][0]["env"][0]
    assert env[1]["value"] == "<redacted>"
    lookalike, _ = public_manifest({"kind": "ConfigMap", "data": pod["spec"]})
    assert lookalike["data"]["volumes"][0]["secret"]["secretName"] == "<redacted>"
    assert pod["spec"]["containers"][0]["env"][1]["value"] == "never-public"


@pytest.mark.parametrize(
    "kind", ["Secret", "Namespace", "PersistentVolume", "PersistentVolumeClaim"]
)
def test_discovery_retention_cannot_be_overridden(kind):
    namespaced = kind in {"Secret", "PersistentVolumeClaim"}
    metadata = {"name": "retained", "uid": "uid", "resourceVersion": "1"}
    if namespaced:
        metadata["namespace"] = "test"
    raw = {"apiVersion": "v1", "kind": kind, "metadata": metadata}
    with pytest.raises(ValueError, match="retention"):
        DiscoveredResource.from_manifest(
            raw,
            scope=ResourceScope.NAMESPACED if namespaced else ResourceScope.CLUSTER,
            retained=False,
        )


class CaptureProvider:
    provider_id = "bounded-fake"

    def __init__(self, artifact):
        self.artifact = artifact
        self.calls = 0

    def discover_api_resource(self, target, kind):
        self.calls += 1
        return self.artifact.coverage.api_resources[0]

    def list_resources(self, request):
        self.calls += 1
        return DiscoveryPage(
            request.target, request.api_resource, self.artifact.resources
        )


def capture(provider, artifact, **limits):
    return capture_discovery(
        provider,
        DiscoveryRequest(
            artifact.target, artifact.coverage.requested, DiscoveryLimits(**limits)
        ),
        capture_id="capture",
        captured_at=artifact.captured_at,
        policy_revision="policy",
    )


def test_capture_stops_on_oversized_private_resource_before_serialization():
    artifact = DiscoveryArtifact.from_dict(fixture())
    raw = artifact.resources[0].manifest
    raw["spec"]["password"] = "s" * 6000
    resource = DiscoveredResource.from_manifest(raw, scope=ResourceScope.NAMESPACED)
    provider = CaptureProvider(replace(artifact, resources=(resource,)))
    result = capture(provider, artifact, max_artifact_bytes=5000)
    assert result.resources == ()
    assert result.coverage.failures[0].kind is DiscoveryFailureKind.LIMIT_EXCEEDED
    assert len(result.to_json()) < 5000
    assert provider.calls == 2


def test_hung_provider_has_wall_deadline_and_bounded_detached_capacity():
    release = threading.Event()
    start = time.monotonic()
    try:
        for _ in range(4):
            with pytest.raises(TimeoutError):
                bounded_call(lambda: release.wait(5), time.monotonic() + 0.02)
        invoked = []
        with pytest.raises(TimeoutError):
            bounded_call(lambda: invoked.append(True), time.monotonic() + 1)
        assert invoked == []
        assert time.monotonic() - start < 0.4
    finally:
        release.set()
        time.sleep(0.05)


def test_capture_deadline_is_a_failure_never_empty_authority():
    artifact = DiscoveryArtifact.from_dict(fixture())
    provider = CaptureProvider(artifact)
    release = threading.Event()

    def hung(target, kind):
        release.wait(2)
        return artifact.coverage.api_resources[0]

    provider.discover_api_resource = hung
    try:
        result = capture(provider, artifact, max_seconds=0.03, call_seconds=0.02)
        assert not result.coverage.complete
        assert (
            result.coverage.failures[0].kind is DiscoveryFailureKind.DEADLINE_EXCEEDED
        )
    finally:
        release.set()
        time.sleep(0.02)


def test_list_snapshot_version_drift_and_cross_page_duplicates_fail():
    artifact = DiscoveryArtifact.from_dict(fixture())
    provider = CaptureProvider(artifact)

    def page(request):
        return DiscoveryPage(
            request.target,
            request.api_resource,
            artifact.resources,
            continuation="next" if request.continuation is None else None,
            list_resource_version="1" if request.continuation is None else "2",
        )

    provider.list_resources = page
    result = capture(provider, artifact)
    assert result.coverage.failures[0].kind is DiscoveryFailureKind.INVALID_PAGE
    assert not result.execution_authoritative
