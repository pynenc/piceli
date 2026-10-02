"""The registry claim's real use, and the listing of every stored manifest.

The kubelet's volume entry of a claim on a node-path volume (k3s
``local-path``) is the node's filesystem: ``registry status`` must not call
that the claim's use. It then reads ``du`` inside the registry pod, or says
that only the shared filesystem's use is known.
"""

from __future__ import annotations

from typing import Any

from piceli.artifacts import cluster_registry as cr
from piceli.artifacts.registry_storage import (
    DISK_USAGE,
    LIST_MANIFESTS,
    REPOSITORIES,
    parse_disk_usage,
    parse_manifest_links,
    registry_pod,
)

NS, CLAIM = "piceli-system", "piceli-registry-storage"
GiB = 1024**3


def _summary(used: int, capacity: int, node_capacity: int) -> dict[str, Any]:
    return {
        "node": {"fs": {"capacityBytes": node_capacity, "usedBytes": used}},
        "pods": [
            {
                "podRef": {"namespace": NS},
                "volume": [
                    {
                        "pvcRef": {"name": CLAIM},
                        "usedBytes": used,
                        "capacityBytes": capacity,
                    }
                ],
            }
        ],
    }


def test_a_node_path_claim_reports_du_not_the_node_filesystem() -> None:
    # local-path: the volume entry is the node's 64 GB disk, 26 GB used
    summary = _summary(26 * GiB, 60 * GiB, 60 * GiB)
    usage = cr.claim_usage(summary, NS, CLAIM, claim_bytes=20 * GiB, du=lambda: 3 * GiB)
    assert usage["used_bytes"] == 3 * GiB
    assert usage["used_source"] == "du"
    assert usage["filesystem"] == {
        "used_bytes": 26 * GiB,
        "capacity_bytes": 60 * GiB,
        "shared": True,
    }
    unknown = cr.claim_usage(summary, NS, CLAIM, claim_bytes=20 * GiB, du=lambda: None)
    assert unknown["used_bytes"] is None and unknown["used_source"] is None
    # the old reader never calls the node's filesystem the claim's any more
    assert cr.used_bytes(summary, NS, CLAIM) is None


def test_a_claim_on_its_own_volume_reports_the_kubelet_stats() -> None:
    summary = _summary(5 * GiB, 19 * GiB, 60 * GiB)
    usage = cr.claim_usage(summary, NS, CLAIM, claim_bytes=20 * GiB, du=lambda: 1)
    assert usage == {
        "used_bytes": 5 * GiB,
        "used_source": "volume-stats",
        "filesystem": {
            "used_bytes": 5 * GiB,
            "capacity_bytes": 19 * GiB,
            "shared": False,
        },
    }
    # bigger than the claim (another disk shared with other data): not the claim's
    other = _summary(5 * GiB, 200 * GiB, 60 * GiB)
    usage = cr.claim_usage(other, NS, CLAIM, claim_bytes=20 * GiB, du=lambda: 7)
    assert (usage["used_bytes"], usage["used_source"]) == (7, "du")


def test_summarize_reports_the_source_and_the_shared_filesystem() -> None:
    registry = cr_registry()
    usage = {
        "used_bytes": 3 * GiB,
        "used_source": "du",
        "filesystem": {
            "used_bytes": 26 * GiB,
            "capacity_bytes": 60 * GiB,
            "shared": True,
        },
    }
    body = cr.summarize(
        registry, deployment={}, service=None,
        claim={"status": {"phase": "Bound"}}, nodes=[], pods=[], usage=usage,
    )  # fmt: skip
    assert body["storage"]["used_bytes"] == 3 * GiB
    assert body["storage"]["used_source"] == "du"
    assert body["storage"]["filesystem"]["shared"] is True


def cr_registry() -> Any:
    from piceli import Registry

    return Registry.in_cluster(on="node-a")


def test_the_storage_listing_finds_tagged_and_untagged_manifests() -> None:
    hex1, hex2 = "a" * 64, "b" * 64
    out = "\n".join(
        [
            f"{REPOSITORIES}/shop/api/_manifests/revisions/sha256/{hex1}/link",
            f"{REPOSITORIES}/mirror/docker.io/library/redis/_manifests/revisions/sha256/{hex2}/link",
            f"{REPOSITORIES}/shop/api/_manifests/tags/latest/current/link",
            f"{REPOSITORIES}/shop/api/_layers/sha256/{hex1}/link",
            "/elsewhere/x/_manifests/revisions/sha256/" + hex1 + "/link",
            f"{REPOSITORIES}/Bad/_manifests/revisions/sha256/{hex1}/link",
        ]
    )
    assert parse_manifest_links(out) == {
        ("shop/api", "sha256:" + hex1),
        ("mirror/docker.io/library/redis", "sha256:" + hex2),
    }
    assert LIST_MANIFESTS[0] == "find" and "-delete" not in LIST_MANIFESTS
    assert DISK_USAGE == ("du", "-sk", "/var/lib/registry")
    assert parse_disk_usage("2992108\t/var/lib/registry\n") == 2992108 * 1024
    assert parse_disk_usage("du: can't open") is None


def test_the_registry_pod_is_the_running_one() -> None:
    def pod(name: str, phase: str, component: str = "registry") -> dict[str, Any]:
        labels = {
            "app.kubernetes.io/instance": "piceli-registry",
            "app.kubernetes.io/component": component,
        }
        return {
            "metadata": {"name": name, "labels": labels},
            "status": {"phase": phase},
        }

    pods = [
        pod("old", "Pending"),
        pod("agent", "Running", "mirror"),
        pod("reg", "Running"),
    ]
    assert registry_pod(pods, "piceli-registry") == "reg"
    assert registry_pod(pods[:2], "piceli-registry") is None


def test_the_listing_runs_one_read_only_exec_in_the_registry_pod(
    monkeypatch: Any,
) -> None:
    import json

    from piceli.artifacts import registry_storage
    from piceli.artifacts.registry_storage import StorageReadError, exec_in_pod
    from piceli.checks import context
    from tests.unit.restore.test_take import FakeSocket

    hex1 = "c" * 64
    line = f"{REPOSITORIES}/shop/api/_manifests/revisions/sha256/{hex1}/link\n"
    ok = b"\x03" + json.dumps({"status": "Success"}).encode()
    opened: list[Any] = []

    def open_socket(
        client: Any, ns: str, pod: str, command: Any, *a: Any, **k: Any
    ) -> Any:
        opened.append((ns, pod, tuple(command)))
        return FakeSocket([b"\x01" + line.encode(), b"\x02find: noise", ok])

    monkeypatch.setattr(context, "open_exec_socket", open_socket)
    monkeypatch.setattr(
        registry_storage,
        "_pods",
        lambda client, ns, name: [
            {
                "metadata": {
                    "name": "reg-0",
                    "labels": {
                        "app.kubernetes.io/instance": name,
                        "app.kubernetes.io/component": "registry",
                    },
                },
                "status": {"phase": "Running"},
            }
        ],
    )
    found = registry_storage.read_stored_manifests(object(), NS, "piceli-registry")
    assert found == {("shop/api", "sha256:" + hex1)}
    assert opened == [(NS, "reg-0", LIST_MANIFESTS)]

    failed = b"\x03" + json.dumps({"status": "Failure"}).encode()
    monkeypatch.setattr(
        context, "open_exec_socket", lambda *a, **k: FakeSocket([failed])
    )
    try:
        exec_in_pod(object(), NS, "reg-0", DISK_USAGE)
    except StorageReadError as error:
        assert error.code == "registry-storage-unreadable"
    else:
        raise AssertionError("a failed exec must raise")
    assert registry_storage.read_disk_usage(object(), NS, "reg-0") is None


def test_the_storage_directory_is_the_registry_claims_mount() -> None:
    from piceli.artifacts import registry_storage

    assert registry_storage.STORAGE_DIR == cr.STORAGE_DIR
