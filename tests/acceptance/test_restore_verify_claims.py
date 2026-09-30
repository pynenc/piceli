"""Acceptance: the scratch-claim calls of ``RestoreCluster`` against the fake API.

``piceli restore --to-new-claim`` creates, reads and deletes Piceli-owned
scratch claims and reads a volume's node affinity through the real
Kubernetes client; the rest of the flow is covered by
``tests/unit/restore/test_verify.py`` and the kind test.
"""

from __future__ import annotations

from kubernetes.client import ApiClient, Configuration

from piceli.restore.cluster import SCRATCH_LABEL, RestoreCluster, scratch_claim
from tests.acceptance.fake_api import TARGET, serve

AFFINITY = {
    "nodeSelectorTerms": [
        {
            "matchExpressions": [
                {"key": "kubernetes.io/hostname", "operator": "In", "values": ["n1"]}
            ]
        }
    ]
}


def test_scratch_claims_are_created_read_and_deleted() -> None:
    with serve() as (api, url):
        configuration = Configuration()
        configuration.host = url
        cluster = RestoreCluster(
            ApiClient(configuration), TARGET.namespace, poll_seconds=0.01
        )
        try:
            api.put(
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolume",
                    "metadata": {"name": "pv-data"},
                    "spec": {"nodeAffinity": {"required": AFFINITY}},
                }
            )
            assert cluster.volume_affinity("pv-data") == AFFINITY
            assert cluster.volume_affinity("pv-absent") is None
            assert cluster.claim("piceli-verify-abcdef-00") is None
            cluster.create_claim(
                scratch_claim(
                    "piceli-verify-abcdef-00",
                    point="rp-20260930t000000z-abcdef",
                    source="data-db-0",
                    spec={
                        "storage_class": "standard",
                        "size": "1Gi",
                        "access_modes": ["ReadWriteOnce"],
                    },
                )
            )
            live = cluster.claim("piceli-verify-abcdef-00")
            assert live is not None
            assert live["metadata"]["labels"][SCRATCH_LABEL] == "true"
            assert live["spec"]["storageClassName"] == "standard"
            assert cluster.delete_claim("piceli-verify-abcdef-00", 5)
            assert cluster.claim("piceli-verify-abcdef-00") is None
            assert cluster.delete_claim("piceli-verify-abcdef-00", 5)  # already gone
        finally:
            cluster.close()


def test_a_claim_without_the_scratch_label_is_never_deleted() -> None:
    with serve() as (api, url):
        configuration = Configuration()
        configuration.host = url
        cluster = RestoreCluster(ApiClient(configuration), TARGET.namespace)
        try:
            api.put(
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {"name": "data-db-0", "namespace": TARGET.namespace},
                    "spec": {"accessModes": ["ReadWriteOnce"]},
                }
            )
            assert cluster.delete_claim("data-db-0", 1) is False
            assert cluster.claim("data-db-0") is not None
        finally:
            cluster.close()
