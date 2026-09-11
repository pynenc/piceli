import copy
import hashlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from piceli.k8s.ops.discovery import (
    ApiDefaultedField,
    ApiResource,
    ApplyProbeResult,
    ApplyProbeStatus,
    DiscoveredResource,
    DiscoveryArtifact,
    DiscoveryCoverage,
    DiscoveryFailureKind,
    DiscoveryLimits,
    DiscoveryPage,
    DiscoveryRequest,
    Ownership,
    PlanTarget,
    ReadinessProbeResult,
    ReadinessStatus,
    ResourceIdentity,
    ResourceListRequest,
    ResourceScope,
    ResourceType,
    capture_discovery,
)
from piceli.k8s.ops.plan import ObservedSnapshot

TARGET = PlanTarget("kind-local", "ih-test")
DEPLOYMENT = ResourceType("apps/v1", "Deployment")


def deployment(name: str, *, uid: str, version: str) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": "ih-test",
            "uid": uid,
            "resourceVersion": version,
        },
        "spec": {"replicas": 1, "revisionHistoryLimit": 10},
    }


class FakeProvider:
    provider_id = "fake-provider/v1"

    def __init__(self, pages: dict[str | None, DiscoveryPage]) -> None:
        self.pages = pages
        self.requests: list[ResourceListRequest] = []

    def discover_api_resource(
        self, target: PlanTarget, resource_type: ResourceType
    ) -> ApiResource | DiscoveryFailureKind:
        assert target == TARGET
        assert resource_type == DEPLOYMENT
        return ApiResource(DEPLOYMENT, ResourceScope.NAMESPACED, "deployments")

    def list_resources(self, request: ResourceListRequest) -> DiscoveryPage:
        self.requests.append(request)
        return self.pages[request.continuation]

    def probe_server_side_apply(
        self, target: PlanTarget, resource: DiscoveredResource
    ) -> ApplyProbeResult:
        return ApplyProbeResult(
            resource.identity,
            ApplyProbeStatus.CONFLICT,
            ("/spec/replicas",),
        )

    def probe_readiness(
        self, target: PlanTarget, resource: ResourceIdentity
    ) -> ReadinessProbeResult:
        return ReadinessProbeResult(resource, ReadinessStatus.NOT_READY, 3)


def page(
    resources: tuple[DiscoveredResource, ...],
    *,
    continuation: str | None = None,
    failure: DiscoveryFailureKind | None = None,
    defaults: tuple[ApiDefaultedField, ...] = (),
) -> DiscoveryPage:
    return DiscoveryPage(
        TARGET,
        ApiResource(DEPLOYMENT, ResourceScope.NAMESPACED, "deployments"),
        resources,
        defaults,
        continuation,
        failure,
    )


def resource(name: str, uid: str, version: str = "1") -> DiscoveredResource:
    return DiscoveredResource.from_manifest(
        deployment(name, uid=uid, version=version),
        scope=ResourceScope.NAMESPACED,
        ownership=Ownership.MANAGED,
    )


def request(**limit_overrides: int) -> DiscoveryRequest:
    return DiscoveryRequest(
        TARGET,
        (DEPLOYMENT,),
        DiscoveryLimits(**limit_overrides),
    )


def capture(provider: FakeProvider, **limit_overrides: int) -> DiscoveryArtifact:
    return capture_discovery(
        provider,
        request(**limit_overrides),
        capture_id="fixture-capture-1",
        captured_at="2026-09-09T10:00:00Z",
        policy_revision="kubernetes-defaults-v1",
    )


def test_capture_paginates_with_explicit_bounds_and_builds_snapshot() -> None:
    first = resource("first", "uid-1")
    second = resource("second", "uid-2", "7")
    default = ApiDefaultedField(first.identity, "/spec/revisionHistoryLimit")
    provider = FakeProvider(
        {
            None: page((first,), continuation="next", defaults=(default,)),
            "next": page((second,)),
        }
    )

    artifact = capture(provider, page_size=1)
    snapshot = ObservedSnapshot.from_discovery(artifact)

    assert artifact.coverage.complete
    assert [item.continuation for item in provider.requests] == [None, "next"]
    assert [item.intent.ref.name for item in snapshot.resources] == ["first", "second"]
    assert snapshot.resources[1].precondition.uid == "uid-2"
    assert snapshot.resources[1].precondition.resource_version == "7"
    assert snapshot.defaulted_fields[0].json_pointer == "/spec/revisionHistoryLimit"


def test_partial_and_rbac_denied_reads_never_report_complete_coverage() -> None:
    provider = FakeProvider(
        {None: page((resource("first", "uid-1"),), continuation="next")}
    )
    limited = capture(provider, max_pages=1)
    assert not limited.coverage.complete
    assert limited.coverage.failures[0].kind is DiscoveryFailureKind.LIMIT_EXCEEDED

    denied = FakeProvider({None: page((), failure=DiscoveryFailureKind.RBAC_DENIED)})
    denied_artifact = capture(denied)
    assert not denied_artifact.coverage.complete
    assert denied_artifact.coverage.failures[0].kind is DiscoveryFailureKind.RBAC_DENIED


def test_repeated_continuation_is_rejected_as_an_invalid_partial_page() -> None:
    provider = FakeProvider(
        {None: page((), continuation="same"), "same": page((), continuation="same")}
    )
    artifact = capture(provider)
    assert not artifact.coverage.complete
    assert artifact.coverage.failures[0].kind is DiscoveryFailureKind.INVALID_PAGE


def test_public_artifact_round_trip_is_secret_safe_and_not_authoritative() -> None:
    secret_type = ResourceType("v1", "Secret")
    secret_api = ApiResource(secret_type, ResourceScope.NAMESPACED, "secrets")
    secret = DiscoveredResource.from_manifest(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": "credentials",
                "namespace": "ih-test",
                "uid": "secret-uid",
                "resourceVersion": "2",
            },
            "stringData": {"password": "do-not-publish"},
        },
        scope=ResourceScope.NAMESPACED,
        ownership=Ownership.MANAGED,
    )
    artifact = DiscoveryArtifact(
        TARGET,
        "2026-09-09T10:00:00Z",
        DiscoveryLimits(),
        DiscoveryCoverage(
            "fake",
            "capture",
            "policy",
            (secret_type,),
            (secret_type,),
            (secret_api,),
        ),
        (secret,),
    )

    encoded = artifact.to_json()
    restored = DiscoveryArtifact.from_json(encoded)

    assert "do-not-publish" not in encoded
    assert "<redacted>" in encoded
    assert not artifact.execution_authoritative
    assert not restored.execution_authoritative
    with pytest.raises(ValueError, match="redacted or incomplete content"):
        from piceli.k8s.ops.plan import (
            DeploymentComponent,
            DeploymentComposition,
            PlanAuthorization,
            ResourceIntent,
            build_plan,
        )

        build_plan(
            DeploymentComposition(
                (
                    DeploymentComponent(
                        "secrets", (ResourceIntent.from_manifest(secret.manifest),)
                    ),
                )
            ),
            ObservedSnapshot.from_discovery(restored),
            PlanAuthorization(TARGET),
        )


def test_provider_probe_results_model_ssa_conflict_and_readiness_without_apply() -> (
    None
):
    item = resource("worker", "uid-worker")
    provider = FakeProvider({None: page((item,))})
    original = copy.deepcopy(item.manifest)

    apply_result = provider.probe_server_side_apply(TARGET, item)
    readiness = provider.probe_readiness(TARGET, item.identity)

    assert apply_result.status is ApplyProbeStatus.CONFLICT
    assert apply_result.conflicting_fields == ("/spec/replicas",)
    assert readiness.status is ReadinessStatus.NOT_READY
    assert item.manifest == original


def test_importing_discovery_contract_does_not_import_or_create_kubernetes_client() -> (
    None
):
    probe = textwrap.dedent(
        """
        import sys
        import types

        class ClientTrap(types.ModuleType):
            def __getattr__(self, name):
                raise AssertionError(f"discovery imported Kubernetes client attribute {name}")

        sys.modules["piceli.k8s.k8s_client.client"] = ClientTrap("client")
        import piceli.k8s.ops.discovery
        """
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


def test_published_fixture_round_trips_for_preview_without_execution_authority() -> (
    None
):
    fixture = Path(__file__).parents[3] / "fixtures" / "discovery-v2" / "complete.json"
    artifact = DiscoveryArtifact.from_json(fixture.read_text())
    snapshot = ObservedSnapshot.from_discovery(artifact)

    assert not artifact.execution_authoritative
    assert artifact.coverage.complete
    assert snapshot.resources[0].precondition.uid == "worker-uid"
    assert DiscoveryArtifact.from_json(artifact.to_json()) == artifact


def test_published_contract_manifest_pins_portable_schema_and_fixture() -> None:
    from jsonschema import Draft202012Validator, FormatChecker

    repository = Path(__file__).parents[4]
    manifest_path = repository / "docs/schemas/piceli-discovery-v2.manifest.json"
    manifest = json.loads(manifest_path.read_text())

    assert manifest["contract"] == "piceli.discovery.v2"
    for name in ("schema", "fixture"):
        artifact = repository / manifest[name]["path"]
        assert artifact.is_file()
        assert (
            hashlib.sha256(artifact.read_bytes()).hexdigest()
            == manifest[name]["sha256"]
        )
    schema = json.loads((repository / manifest["schema"]["path"]).read_text())
    fixture = json.loads((repository / manifest["fixture"]["path"]).read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(fixture)
    DiscoveryArtifact.from_dict(fixture)
