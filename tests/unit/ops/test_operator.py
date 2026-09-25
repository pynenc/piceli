"""Unit tests for Piceli operator core: classification, inventory, logs, and buffers."""

import time

from piceli.k8s.observe import ObservationRef, ObservedObject
from piceli.k8s.operator import (
    BoundedInventoryBuffer,
    InventoryEvent,
    build_operator_report,
    redact_log_content,
)


class FakeInventoryReader:
    def __init__(self, objects: dict[ObservationRef, ObservedObject]) -> None:
        self.objects = objects

    def get(self, ref: ObservationRef) -> ObservedObject | None:
        return self.objects.get(ref)

    def list(self, api_version: str, kind: str, namespace: str) -> list[ObservedObject]:
        return [
            obj
            for ref, obj in self.objects.items()
            if ref.api_version == api_version
            and ref.kind == kind
            and ref.namespace == namespace
        ]


def test_operator_report_distinguishes_managed_and_unmanaged() -> None:
    ref_managed = ObservationRef("apps/v1", "Deployment", "my-ns", "piceli-app")
    ref_unmanaged = ObservationRef("apps/v1", "Deployment", "my-ns", "legacy-app")

    obs_managed = ObservedObject(ref=ref_managed, phase="Running", images=("app:v1",))
    obs_unmanaged = ObservedObject(
        ref=ref_unmanaged, phase="Running", images=("legacy:v2",)
    )

    reader = FakeInventoryReader(
        {
            ref_managed: obs_managed,
            ref_unmanaged: obs_unmanaged,
        }
    )

    # Dummy session archive declaring only ref_managed
    class DummyArchive:
        session_id = "s" * 32

        def to_dict(self) -> dict[str, object]:
            return {
                "composition": [
                    {
                        "resources": [
                            {
                                "resource": {
                                    "api_version": "apps/v1",
                                    "kind": "Deployment",
                                    "namespace": "my-ns",
                                    "name": "piceli-app",
                                }
                            }
                        ]
                    }
                ]
            }

    report = build_operator_report(
        reader, "my-ns", session_archive=DummyArchive(), include_common_types=False
    )

    assert len(report.managed) == 1
    assert report.managed[0].ref == ref_managed
    assert report.managed[0].classification == "managed"
    assert report.managed[0].state == "present"

    assert len(report.unmanaged) == 1
    assert report.unmanaged[0].ref == ref_unmanaged
    assert report.unmanaged[0].classification == "unmanaged"
    assert report.unmanaged[0].state == "present"

    data = report.to_dict()
    assert data["namespace"] == "my-ns"
    assert data["session_id"] == "s" * 32


def test_redact_log_content() -> None:
    raw_log = "Starting service with password=super-secret and token: abcdef123456"
    redacted = redact_log_content(raw_log, known_secrets=("super-secret",))
    assert "super-secret" not in redacted
    assert "abcdef123456" not in redacted
    assert "[REDACTED]" in redacted


def test_bounded_inventory_buffer() -> None:
    buffer = BoundedInventoryBuffer(capacity=5)
    ref = ObservationRef("v1", "Pod", "ns", "p1")
    now = time.time()

    for i in range(10):
        buffer.record(InventoryEvent(now + i, ref, "modified", "managed"))

    events = buffer.events_since(now + 6)
    assert len(events) == 3


def test_pods_and_replicasets_of_managed_workloads_are_derived_not_unmanaged() -> None:
    """ownerReferences chains attribute controller children to their root (B9)."""

    def ref(api: str, kind: str, name: str) -> ObservationRef:
        return ObservationRef(api, kind, "shop", name)

    deploy = ref("apps/v1", "Deployment", "web")
    replica = ref("apps/v1", "ReplicaSet", "web-7d9f")
    pod = ref("v1", "Pod", "web-7d9f-abcde")
    stray = ref("v1", "Pod", "debug")
    orphan_rs = ref("apps/v1", "ReplicaSet", "legacy-1")
    orphan_pod = ref("v1", "Pod", "legacy-1-xyz")
    labelled = ref("apps/v1", "Deployment", "api")
    api_rs = ref("apps/v1", "ReplicaSet", "api-55")
    objects = {
        deploy: ObservedObject(ref=deploy, uid="u-web"),
        replica: ObservedObject(
            ref=replica, uid="u-rs", owners=(("Deployment", "web", "u-web"),)
        ),
        pod: ObservedObject(
            ref=pod,
            uid="u-pod",
            phase="Running",
            images=("registry.example/shop/web@sha256:" + "a" * 64,),
            owners=(("ReplicaSet", "web-7d9f", "u-rs"),),
        ),
        stray: ObservedObject(ref=stray, uid="u-debug"),
        orphan_rs: ObservedObject(
            ref=orphan_rs, uid="u-l", owners=(("Deployment", "legacy", "u-gone"),)
        ),
        orphan_pod: ObservedObject(
            ref=orphan_pod, owners=(("ReplicaSet", "legacy-1", "u-l"),)
        ),
        labelled: ObservedObject(
            ref=labelled, uid="u-api", labels=(("piceli.io/managed", "true"),)
        ),
        api_rs: ObservedObject(ref=api_rs, owners=(("Deployment", "api", ""),)),
    }

    class Archive:
        session_id = "s" * 32

        def to_dict(self) -> dict[str, object]:
            resource = {
                "api_version": "apps/v1",
                "kind": "Deployment",
                "namespace": "shop",
                "name": "web",
            }
            return {"composition": [{"resources": [{"resource": resource}]}]}

    report = build_operator_report(
        FakeInventoryReader(objects), "shop", session_archive=Archive()
    )
    derived = {item.ref: item.derived_from for item in report.managed}
    assert derived == {
        deploy: None,
        replica: "Deployment/web",
        pod: "Deployment/web",
        labelled: None,
        api_rs: "Deployment/api",
    }
    assert {item.ref for item in report.unmanaged} == {stray, orphan_rs, orphan_pod}
    as_dict = report.to_dict()
    pod_entry = next(i for i in as_dict["managed"] if i["ref"]["name"] == pod.name)
    assert pod_entry["derived_from"] == "Deployment/web"
    web_entry = next(
        i
        for i in as_dict["managed"]
        if i["ref"]["kind"] == "Deployment" and i["ref"]["name"] == "web"
    )
    assert "derived_from" not in web_entry


def test_reader_summary_keeps_owner_references() -> None:
    from piceli.k8s.observe import KubernetesDynamicInventoryReader

    summary = KubernetesDynamicInventoryReader._summary(
        {
            "metadata": {
                "name": "web-1",
                "uid": "u1",
                "ownerReferences": [
                    {"kind": "ReplicaSet", "name": "web-7d9f", "uid": "u-rs"},
                    "junk",
                ],
            }
        },
        ObservationRef("v1", "Pod", "shop", "web-1"),
    )
    assert summary.owners == (("ReplicaSet", "web-7d9f", "u-rs"),)
