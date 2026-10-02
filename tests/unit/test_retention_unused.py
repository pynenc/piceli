"""Retention removes images nothing uses: ``--keep 0``, liveness from what runs
or is referenced now, and digest-only manifests found without a receipt."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.cli import main
from piceli.artifacts.registry import RegistryTarget
from piceli.artifacts.retention import (
    LiveWorkloads,
    RetentionError,
    RetentionGrant,
    RetentionPolicy,
    collect_inventory,
    delete_collectable,
    live_images,
    load_releases,
    plan_retention,
)
from tests.retention_registry import RetentionRegistry

HOST = "registry.example.svc:5000"  # the stable pull name of an in-cluster registry
HOSTED = "registry.example.com/team"  # a hosted registry the controller moved to
GB = 1_000_000_000


@pytest.fixture
def registry() -> Iterator[RetentionRegistry]:
    fake = RetentionRegistry()
    yield fake
    fake.close()


def _pod(name: str, image: str, digest: str, phase: str = "Running") -> dict[str, Any]:
    return {
        "kind": "Pod",
        "metadata": {"name": name, "namespace": "apps"},
        "spec": {"containers": [{"name": "c", "image": image}]},
        "status": {
            "phase": phase,
            "containerStatuses": [
                {
                    "name": "c",
                    "image": image,
                    "imageID": f"{image.split('@')[0]}@{digest}",
                }
            ],
        },
    }


def _template(image: str) -> dict[str, Any]:
    return {"spec": {"containers": [{"name": "c", "image": image}]}}


def _old_replica_set(image: str) -> dict[str, Any]:
    """A ReplicaSet a Deployment keeps for rollout history: scaled to zero."""
    return {
        "kind": "ReplicaSet",
        "metadata": {
            "name": "controller-old",
            "ownerReferences": [{"kind": "Deployment", "name": "controller"}],
        },
        "spec": {"replicas": 0, "template": _template(image)},
        "status": {"replicas": 0},
    }


def _finished_job(image: str) -> dict[str, Any]:
    return {
        "kind": "Job",
        "metadata": {"name": "build-1"},
        "spec": {"template": _template(image)},
        "status": {"conditions": [{"type": "Complete", "status": "True"}]},
    }


def _single_release(registry: RetentionRegistry, tmp_path: Path) -> dict[str, str]:
    """One publish receipt naming a controller, a builder and a third-party
    builder (about 4 GB together), as a first cluster build leaves them."""
    digests = {
        "controller": registry.add_sparse_image(
            "app/controller", None, [("controller-base", 300_000_000)]
        ),
        "builder": registry.add_sparse_image(
            "app/builder", None, [("builder-base", 700_000_000)]
        ),
        "toolchain": registry.add_sparse_image(
            "app/toolchain", None, [("toolchain", 3 * GB)]
        ),
    }
    receipt = {
        "state": "published",
        "registry": f"127.0.0.1:{registry.port}",
        "finished_at": "2026-09-30T10:00:00Z",
        "images": {
            name: {"repository": f"app/{name}", "digest": digest}
            for name, digest in digests.items()
        },
    }
    (tmp_path / "receipts").mkdir()
    (tmp_path / "receipts" / "first.json").write_text(json.dumps(receipt))
    return digests


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The objects the (seamed) cluster read returns; tests fill it."""
    from piceli.artifacts import retention_cli

    items: list[dict[str, Any]] = []

    def read(kubeconfig: Path, context: str, namespaces: Any) -> list[dict[str, Any]]:
        return [json.loads(json.dumps(item)) for item in items]

    monkeypatch.setattr(retention_cli, "read_live_pods", read)
    return items


def run(capsys: pytest.CaptureFixture[str], *args: str) -> Any:
    code = main(["retention", *args])
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    return code, json.loads(lines[-1]) if lines else {}


def _cli(registry: RetentionRegistry, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--to",
        f"oci://127.0.0.1:{registry.port}/app",
        "--receipts",
        str(tmp_path / "receipts"),
        "--kubeconfig",
        str(tmp_path / "kubeconfig"),
        "--context",
        "my-cluster",
        *extra,
    ]


# ------------------------------------------------------------- reason 1: --keep 0


def test_keep_zero_removes_a_single_release_nothing_uses(
    registry: RetentionRegistry,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cluster: list[dict[str, Any]],
) -> None:
    digests = _single_release(registry, tmp_path)
    running = registry.add_sparse_image(
        "app/controller", None, [("controller-base", 300_000_000), ("new", 10)]
    )
    cluster[:] = [_pod("controller-1", f"{HOST}/app/controller@{running}", running)]
    base = _cli(registry, tmp_path, "--keep", "0")

    code, report = run(capsys, *base)
    assert code == 0 and report["policy"]["keep"] == 0
    deletions = {row["digest"]: row for row in report["collectable"]["deletions"]}
    assert set(deletions) == set(digests.values())
    assert deletions[digests["toolchain"]]["repository"] == "app/toolchain"
    assert deletions[digests["toolchain"]]["bytes"] >= 3 * GB
    assert deletions[digests["toolchain"]]["why"] == ["old-release"]
    assert running not in deletions

    code, asked = run(capsys, *base, "--delete")
    assert code == 3 and asked["digest"] == report["digest"]
    assert registry.deletes() == []

    code, done = run(
        capsys,
        *base,
        "--delete",
        "--approve",
        report["digest"],
    )
    assert code == 0 and done["state"] == "deleted"
    assert {row["digest"] for row in done["deleted"]} == set(digests.values())
    assert {d for (_, d) in registry.manifests} == {running}


def test_keep_zero_needs_the_cluster_read(
    registry: RetentionRegistry, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _single_release(registry, tmp_path)
    live = tmp_path / "live.txt"
    live.write_text("[]")
    code, refused = run(
        capsys,
        "--to",
        f"oci://127.0.0.1:{registry.port}/app",
        "--receipts",
        str(tmp_path / "receipts"),
        "--keep",
        "0",
        "--live-file",
        str(live),
        "--delete",
    )
    assert (code, refused["reason"]) == (2, "retention-invalid")
    assert registry.deletes() == []


def test_keep_zero_is_a_valid_policy_but_deleting_it_needs_a_cluster_read(
    registry: RetentionRegistry, tmp_path: Path
) -> None:
    digests = _single_release(registry, tmp_path)
    tgt = RegistryTarget.parse(f"oci://127.0.0.1:{registry.port}/app")
    releases = load_releases([tmp_path / "receipts"])
    inventory = collect_inventory(tgt, tgt.endpoint(), releases)
    from_file = LiveWorkloads(True, frozenset(), source="file")
    plan = plan_retention(tgt, inventory, releases, RetentionPolicy(keep=0), from_file)
    assert {digest for _, digest in plan.collectable} == set(digests.values())
    with pytest.raises(RetentionError) as refused:
        delete_collectable(plan, inventory, RetentionGrant(plan.digest), tgt.endpoint())
    assert refused.value.code == "retention-live-unknown"
    assert registry.deletes() == []


def test_keep_zero_approval_is_refused_when_a_pod_starts_using_a_digest(
    registry: RetentionRegistry,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cluster: list[dict[str, Any]],
) -> None:
    digests = _single_release(registry, tmp_path)
    base = _cli(registry, tmp_path, "--keep", "0")
    _, report = run(capsys, *base)
    cluster[:] = [
        _pod("b", f"{HOST}/app/builder@{digests['builder']}", digests["builder"])
    ]
    code, refused = run(capsys, *base, "--delete", "--approve", report["digest"])
    assert (code, refused["reason"]) == (2, "retention-not-approved")
    assert registry.deletes() == []


# ------------------------------------------------- reason 2: liveness is what runs


def test_rollout_history_finished_pods_and_jobs_are_not_live() -> None:
    old, done, built = ("sha256:" + x * 64 for x in "abc")
    digests, _ = live_images(
        [
            _old_replica_set(f"{HOST}/app/controller@{old}"),
            _pod("build-1-x", f"{HOST}/app/builder@{done}", done, phase="Succeeded"),
            _finished_job(f"{HOST}/app/toolchain@{built}"),
        ]
    )
    assert digests == {}
    live = LiveWorkloads.from_objects(
        [_old_replica_set(f"{HOST}/app/controller@{old}")]
    )
    assert old not in live.digests
    assert live.refs[("app/controller", old)] == {"replicaset:history"}


def test_a_scaled_up_or_unowned_replica_set_stays_live() -> None:
    a, b = ("sha256:" + x * 64 for x in "ab")
    rolling = _old_replica_set(f"{HOST}/app/api@{a}")
    rolling["status"] = {"replicas": 1}  # mid-rollout: its pods still run
    alone = {
        "kind": "ReplicaSet",
        "spec": {"replicas": 0, "template": _template(f"x/app/b@{b}")},
    }
    digests, _ = live_images([rolling, alone])
    assert digests == {a: {"replicaset"}, b: {"replicaset"}}


def test_the_controllers_old_in_cluster_copy_is_collectable(
    registry: RetentionRegistry,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cluster: list[dict[str, Any]],
) -> None:
    digests = _single_release(registry, tmp_path)
    hosted = "sha256:" + "e" * 64  # the controller now runs from a hosted registry
    cluster[:] = [
        _pod("controller-2", f"{HOSTED}/controller@{hosted}", hosted),
        {
            "kind": "Deployment",
            "metadata": {"name": "controller"},
            "spec": {
                "replicas": 1,
                "template": _template(f"{HOSTED}/controller@{hosted}"),
            },
        },
        _old_replica_set(f"{HOST}/app/controller@{digests['controller']}"),
    ]
    code, report = run(capsys, *_cli(registry, tmp_path, "--keep", "0"))
    assert code == 0
    rows = {row["digest"]: row for row in report["collectable"]["deletions"]}
    assert rows[digests["controller"]]["why"] == ["old-release", "not-live"]
    assert rows[digests["controller"]]["seen_in"] == ["replicaset:history"]


def test_environment_records_keep_their_digests(
    registry: RetentionRegistry,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cluster: list[dict[str, Any]],
) -> None:
    digests = _single_release(registry, tmp_path)
    labels = {"app.kubernetes.io/managed-by": "piceli"}
    record = {
        "branch": "feature",
        "state": "stopped",
        "images": {"builder": {"digest": digests["builder"]}},
    }
    cluster[:] = [
        {
            "kind": "ConfigMap",
            "metadata": {
                "name": "piceli-env",
                "namespace": "shop-feature",
                "labels": labels,
            },
            "data": {"record": json.dumps(record)},
        },
        {
            "kind": "ConfigMap",
            "metadata": {
                "name": "piceli-env-other",
                "namespace": "shop-other",
                "labels": labels,
            },
            "data": {
                "images": json.dumps(
                    {
                        "toolchain": {
                            "digest": digests["toolchain"],
                            "pull_ref": f"{HOST}/app/toolchain@{digests['toolchain']}",
                        }
                    }
                )
            },
        },
        {  # an unrelated Piceli ConfigMap naming a digest keeps nothing
            "kind": "ConfigMap",
            "metadata": {"name": "piceli-state", "namespace": "shop", "labels": labels},
            "data": {"head": digests["controller"]},
        },
    ]
    code, report = run(capsys, *_cli(registry, tmp_path, "--keep", "0"))
    assert code == 0
    deleted = {row["digest"] for row in report["collectable"]["deletions"]}
    assert deleted == {digests["controller"]}
    rows = {row["digest"]: row for row in report["manifests"]}
    assert rows[digests["builder"]]["live_by"] == ["environment"]
    assert rows[digests["toolchain"]]["live_by"] == ["environment"]


# ---------------------------------------- reason 3: digest-only, without a receipt


def test_a_digest_only_manifest_without_a_receipt_is_found_and_removed(
    registry: RetentionRegistry,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cluster: list[dict[str, Any]],
) -> None:
    (tmp_path / "receipts").mkdir()
    stale = registry.add_sparse_image("app/builder", None, [("old-builder", 2 * GB)])
    running = registry.add_sparse_image("app/builder", None, [("new-builder", GB)])
    cluster[:] = [
        _pod("build-1-x", f"{HOST}/app/builder@{stale}", stale, phase="Succeeded"),
        _pod("build-2-x", f"{HOST}/app/builder@{running}", running),
    ]
    base = _cli(registry, tmp_path, "--keep", "0")
    code, report = run(capsys, *base)
    assert code == 0
    rows = {row["digest"]: row for row in report["collectable"]["deletions"]}
    assert set(rows) == {stale}
    assert rows[stale]["why"] == ["not-live"]
    assert rows[stale]["seen_in"] == ["pod:finished"]
    code, done = run(capsys, *base, "--delete", "--approve", report["digest"])
    assert code == 0 and [row["digest"] for row in done["deleted"]] == [stale]
    assert {d for (_, d) in registry.manifests} == {running}


def test_a_live_multi_platform_image_keeps_its_index_and_children(
    registry: RetentionRegistry,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cluster: list[dict[str, Any]],
) -> None:
    (tmp_path / "receipts").mkdir()
    children = [
        registry.add_image(
            "app/api", None, [b"base" * 10, arch.encode() * 10], arch=arch
        )
        for arch in ("amd64", "arm64")
    ]
    index = registry.add_index("app/api", None, children)
    old_children = [
        registry.add_image("app/api", None, [b"old" * 10, arch.encode() * 9], arch=arch)
        for arch in ("amd64", "arm64")
    ]
    old_index = registry.add_index("app/api", None, old_children)
    cluster[:] = [
        _pod("api-1", f"{HOST}/app/api@{index}", index),
        _pod("api-0", f"{HOST}/app/api@{old_index}", old_index, phase="Failed"),
        # a finished pod that ran one child of the live index directly
        _pod("probe", f"{HOST}/app/api@{children[0]}", children[0], phase="Succeeded"),
    ]
    base = _cli(registry, tmp_path, "--keep", "0")
    code, report = run(capsys, *base)
    assert code == 0
    deleted = {row["digest"] for row in report["collectable"]["deletions"]}
    assert deleted == {old_index, *old_children}
    code, done = run(capsys, *base, "--delete", "--approve", report["digest"])
    assert code == 0
    assert {d for (_, d) in registry.manifests} == {index, *children}


def test_the_controller_status_keeps_the_digests_it_names() -> None:
    digest = "sha256:" + "f" * 64
    status = {
        "kind": "ConfigMap",
        "metadata": {"name": "piceli-gitops-status", "namespace": "piceli-system"},
        "data": {"status.json": json.dumps({"envs": {"main": {"image": digest}}})},
    }
    digests, _ = live_images([status])
    assert digests == {digest: {"controller"}}
