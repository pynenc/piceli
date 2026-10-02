"""Retention of the in-cluster registry works out what to keep on its own.

``piceli artifacts retention --cluster infra.py:CLUSTER`` (no receipts, no
pins) keeps what runs, the rollback target of every workload (the newest
non-current ReplicaSet or ControllerRevision) and what Piceli itself uses (the
controller, the builder named in its config, the UI); every other manifest of
the registry, tagged or not, is collectable, including digest-only manifests
nothing names (found by listing the registry's storage).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts import retention_cli
from piceli.artifacts.cli import main
from piceli.artifacts.registry import RegistryEndpoint
from piceli.artifacts.retention import (
    LiveWorkloads,
    RetentionError,
    RetentionGrant,
    RetentionPolicy,
    StableTarget,
    collect_inventory,
    delete_collectable,
    plan_retention,
)
from piceli.profiles import save_profile
from piceli.testing import write_kubeconfig
from tests.retention_registry import RetentionRegistry

HOST = "piceli-registry.piceli-system.svc:5000"
API = "https://10.0.0.1:6443"
APP = "shop-main"
SYSTEM = "piceli-system"
GB = 1_000_000_000


@pytest.fixture
def registry() -> Iterator[RetentionRegistry]:
    fake = RetentionRegistry()
    yield fake
    fake.close()


def _containers(image: str) -> dict[str, Any]:
    return {"spec": {"containers": [{"name": "c", "image": image}]}}


def _pod(name: str, ns: str, image: str, digest: str) -> dict[str, Any]:
    return {
        "kind": "Pod",
        "metadata": {"name": name, "namespace": ns},
        "spec": {"containers": [{"name": "c", "image": image}]},
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {"name": "c", "image": image, "imageID": f"{HOST}/x@{digest}"}
            ],
        },
    }


def _deployment(
    name: str, ns: str, image: str, revision: int, labels: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": labels or {},
            "annotations": {"deployment.kubernetes.io/revision": str(revision)},
        },
        "spec": {"replicas": 1, "template": _containers(image)},
    }


def _replica_set(
    owner: str, ns: str, image: str, revision: int, current: bool
) -> dict[str, Any]:
    return {
        "kind": "ReplicaSet",
        "metadata": {
            "name": f"{owner}-{revision}",
            "namespace": ns,
            "annotations": {"deployment.kubernetes.io/revision": str(revision)},
            "ownerReferences": [{"kind": "Deployment", "name": owner}],
        },
        "spec": {"replicas": 1 if current else 0, "template": _containers(image)},
        "status": {"replicas": 1 if current else 0},
    }


def _revision(owner: str, ns: str, image: str, revision: int) -> dict[str, Any]:
    return {
        "kind": "ControllerRevision",
        "metadata": {
            "name": f"{owner}-r{revision}",
            "namespace": ns,
            "ownerReferences": [{"kind": "StatefulSet", "name": owner}],
        },
        "revision": revision,
        "data": {"spec": {"template": _containers(image)}, "$patch": "replace"},
    }


@pytest.fixture
def scene(registry: RetentionRegistry) -> dict[str, Any]:
    """A registry holding live, rollback and in-use images, old controller and
    builder copies and orphans; every image digest-only (no tag, no receipt)."""

    def image(repo: str, label: str, size: int = 10_000) -> str:
        return registry.add_sparse_image(repo, None, [(label, size)])

    d = {
        "api_old": image("shop/api", "api-1"),
        "api_prev": image("shop/api", "api-2"),
        "api_now": image("shop/api", "api-3"),
        "db_now": image("shop/db", "db-2"),
        "redis": image("mirror/docker.io/library/redis", "redis"),
        "ctl_old": image("piceli/controller", "ctl-1", 400_000_000),
        "ctl_now": image("piceli/controller", "ctl-2", 400_000_000),
        "builder_old": image("piceli/builder", "builder-1", 2 * GB),
        "builder_now": image("piceli/builder", "builder-2", 2 * GB),
        "ui_now": image("piceli/ui", "ui-1"),
        "orphan": image("shop/tool", "tool", GB),
    }
    children = [
        registry.add_image("shop/db", None, [b"db" * 10, arch.encode() * 10], arch=arch)
        for arch in ("amd64", "arm64")
    ]
    d["db_prev_children"] = children
    d["db_prev"] = registry.add_index("shop/db", None, children)
    piceli = {"app.kubernetes.io/managed-by": "piceli"}
    config = {"build": {"builder_image": f"{HOST}/piceli/builder@{d['builder_now']}"}}
    objects = [
        # the app: a Deployment with two old revisions, a StatefulSet with one
        _deployment("api", APP, f"{HOST}/shop/api@{d['api_now']}", 3),
        _replica_set("api", APP, f"{HOST}/shop/api@{d['api_old']}", 1, False),
        _replica_set("api", APP, f"{HOST}/shop/api@{d['api_prev']}", 2, False),
        _replica_set("api", APP, f"{HOST}/shop/api@{d['api_now']}", 3, True),
        _pod("api-3-x", APP, f"{HOST}/shop/api@{d['api_now']}", d["api_now"]),
        {
            "kind": "StatefulSet",
            "metadata": {"name": "db", "namespace": APP},
            "spec": {"template": _containers(f"{HOST}/shop/db@{d['db_now']}")},
            "status": {"currentRevision": "db-r2", "updateRevision": "db-r2"},
        },
        _revision("db", APP, f"{HOST}/shop/db@{d['db_prev']}", 1),
        _revision("db", APP, f"{HOST}/shop/db@{d['db_now']}", 2),
        _pod("db-0", APP, f"{HOST}/shop/db@{d['db_now']}", d["db_now"]),
        _pod("cache-0", APP, f"{HOST}/mirror/docker.io/library/redis@{d['redis']}", d["redis"]),
        # Piceli itself: the controller (and its previous copy), the UI, the builder
        _deployment(
            "piceli-gitops", SYSTEM, f"{HOST}/piceli/controller@{d['ctl_now']}", 2,
            {**piceli, "piceli.io/component": "gitops"},
        ),
        _replica_set("piceli-gitops", SYSTEM, f"{HOST}/piceli/controller@{d['ctl_old']}", 1, False),
        _replica_set("piceli-gitops", SYSTEM, f"{HOST}/piceli/controller@{d['ctl_now']}", 2, True),
        _deployment(
            "piceli-ui", SYSTEM, f"{HOST}/piceli/ui@{d['ui_now']}", 1,
            {**piceli, "piceli.io/component": "ui"},
        ),
        {
            "kind": "ConfigMap",
            "metadata": {"name": "piceli-gitops-config", "namespace": SYSTEM, "labels": piceli},
            "data": {"config.json": json.dumps(config)},
        },
    ]  # fmt: skip
    return {"digests": d, "objects": objects}


@pytest.fixture
def cluster(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry: RetentionRegistry,
    scene: dict[str, Any],
) -> dict[str, Any]:
    """A composition declaring the cluster, its profile, and the seams: the
    cluster read, the storage listing and the port-forward to the registry."""
    monkeypatch.setenv("PICELI_PROFILES_DIR", str(tmp_path / "profiles"))
    save_profile("my-cluster", write_kubeconfig(API, tmp_path / "kubeconfig"), "fake")
    (tmp_path / "infra.py").write_text(
        f"""
from piceli import Registry
from piceli.infra import Cluster, Node

my_cluster = Cluster(
    "my-cluster",
    api="{API}",
    credentials="my-cluster",
    nodes=[Node("node-a", arch="arm64", roles=["registry"])],
    registry=Registry.in_cluster(on="node-a"),
)
"""
    )
    calls: dict[str, Any] = {"reads": [], "forwards": [], "listings": 0}

    def read(kubeconfig: Path, context: str, namespaces: Any, **kw: Any) -> Any:
        calls["reads"].append((kubeconfig, context, namespaces))
        return json.loads(json.dumps(scene["objects"]))

    def stored(access: Any, found: Any) -> set[tuple[str, str]]:
        calls["listings"] += 1
        return set(registry.manifests)

    @contextlib.contextmanager
    def forward(found: Any, access: Any, args: Any) -> Iterator[int]:
        calls["forwards"].append((found.namespace, found.name, found.port))
        yield registry.port

    monkeypatch.setattr(retention_cli, "read_live_pods", read)
    monkeypatch.setattr(retention_cli, "read_stored_manifests", stored)
    monkeypatch.setattr(retention_cli, "cluster_forward", forward)
    monkeypatch.chdir(tmp_path)
    return calls


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, Any]:
    code = main(["retention", *args])
    out = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    return code, json.loads(out[-1]) if out else {}


@pytest.mark.parametrize("ref", ["infra.py:my_cluster", "infra.py"])
def test_cluster_plan_lists_exactly_old_copies_and_orphans(
    ref: str,
    registry: RetentionRegistry,
    scene: dict[str, Any],
    cluster: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = scene["digests"]
    code, report = run(capsys, "--cluster", ref)
    assert code == 0, report
    deletions = {row["digest"] for row in report["collectable"]["deletions"]}
    assert deletions == {d["api_old"], d["ctl_old"], d["builder_old"], d["orphan"]}
    rows = {row["digest"]: row for row in report["manifests"]}
    assert "rollback" in rows[d["api_prev"]]["reasons"]
    assert "rollback" in rows[d["db_prev"]]["reasons"]
    for child in d["db_prev_children"]:
        assert rows[child]["kept"]
    assert rows[d["builder_now"]]["kept"] and rows[d["ctl_now"]]["kept"]
    assert report["storage_listing"]["read"] is True
    # derived from the Cluster: all namespaces, the registry's Service
    assert cluster["reads"][0][1:] == ("fake", None)
    assert cluster["forwards"] == [("piceli-system", "piceli-registry", 5000)]
    assert report["target"]["registry"] == HOST


def test_cluster_delete_needs_the_hash_then_deletes_only_the_plan(
    registry: RetentionRegistry,
    scene: dict[str, Any],
    cluster: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = scene["digests"]
    code, asked = run(capsys, "--cluster", "infra.py:my_cluster", "--delete")
    assert code == 3 and asked["state"] == "approval-required"
    assert asked["approve_command"] == (
        "piceli artifacts retention --cluster infra.py:my_cluster --delete "
        f"--approve {asked['digest']}"
    )
    assert registry.deletes() == []
    code, done = run(
        capsys,
        "--cluster",
        "infra.py:my_cluster",
        "--delete",
        "--approve",
        asked["digest"],
    )
    assert code == 0 and done["state"] == "deleted", done
    gone = {row["digest"] for row in done["deleted"]}
    assert gone == {d["api_old"], d["ctl_old"], d["builder_old"], d["orphan"]}
    left = {digest for _, digest in registry.manifests}
    for name in ("api_prev", "api_now", "db_prev", "db_now", "redis", "ctl_now",
                 "builder_now", "ui_now"):  # fmt: skip
        assert d[name] in left, name
    assert set(d["db_prev_children"]) <= left
    gc = done["garbage_collect"]["commands"]
    assert gc[0].endswith(
        "-n piceli-system exec deploy/piceli-registry -- registry garbage-collect "
        "--dry-run /etc/distribution/config.yml"
    )


def test_old_flags_keep_the_rollback_target_too(
    registry: RetentionRegistry,
    scene: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = scene["digests"]
    monkeypatch.setattr(
        retention_cli, "read_live_pods", lambda *a, **k: scene["objects"]
    )
    code, report = run(
        capsys, "--to", f"oci://127.0.0.1:{registry.port}/shop", "--keep", "0",
        "--kubeconfig", str(tmp_path / "kubeconfig"), "--context", "my-cluster",
    )  # fmt: skip
    assert code == 0
    deletions = {row["digest"] for row in report["collectable"]["deletions"]}
    assert d["api_prev"] not in deletions and d["db_prev"] not in deletions
    assert d["api_old"] in deletions


def test_a_rollback_target_is_never_deleted_even_from_a_tampered_plan(
    registry: RetentionRegistry, scene: dict[str, Any]
) -> None:
    d = scene["digests"]
    target = StableTarget(HOST, "")
    endpoint = RegistryEndpoint("127.0.0.1", registry.port, False)
    live = LiveWorkloads.from_objects(scene["objects"])
    assert d["api_prev"] in live.rollback and d["db_prev"] in live.rollback
    assert d["api_old"] not in live.rollback and d["ctl_old"] not in live.rollback
    inventory = collect_inventory(target, endpoint, [], stored=set(registry.manifests))
    plan = plan_retention(
        target, inventory, [], RetentionPolicy(keep=0, collect_unledgered=True), live
    )
    assert "rollback" in plan.kept[d["api_prev"]]
    tampered = plan.__class__(
        **{
            **plan.__dict__,
            "kept": {k: v for k, v in plan.kept.items() if k != d["api_prev"]},
            "collectable": (*plan.collectable, ("shop/api", d["api_prev"])),
        }
    )
    with pytest.raises(RetentionError) as refused:
        delete_collectable(
            tampered, inventory, RetentionGrant(tampered.digest), endpoint
        )
    assert refused.value.code == "retention-not-approved"
    assert registry.deletes() == []


def test_an_unreadable_storage_is_reported_and_finds_less(
    registry: RetentionRegistry,
    scene: dict[str, Any],
    cluster: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from piceli.artifacts.registry_storage import StorageReadError

    def refuse(access: Any, found: Any) -> Any:
        raise StorageReadError("registry-storage-unreadable")

    monkeypatch.setattr(retention_cli, "read_stored_manifests", refuse)
    code, report = run(capsys, "--cluster", "infra.py:my_cluster")
    assert code == 0
    assert report["storage_listing"] == {
        "read": False,
        "reason": "registry-storage-unreadable",
    }
    deletions = {row["digest"] for row in report["collectable"]["deletions"]}
    assert scene["digests"]["orphan"] not in deletions  # not found, so not touched


def test_cluster_refusals(
    cluster: dict[str, Any], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / "hosted.py").write_text(
        "from piceli import Registry\nregistry = Registry('oci://registry.example/x')\n"
    )
    code, refused = run(capsys, "--cluster", "hosted.py:registry")
    assert (code, refused["reason"]) == (2, "cluster-registry-invalid")
    code, refused = run(capsys, "--cluster", "infra.py:my_cluster", "--to", "oci://h/x")
    assert (code, refused["reason"]) == (2, "retention-invalid")
    code, refused = run(capsys)
    assert (code, refused["reason"]) == (2, "retention-invalid")
