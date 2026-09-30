"""Registry retention (``piceli artifacts retention``): keep the last releases,
pinned and live digests; delete exactly the approved rest."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.cli import main
from piceli.artifacts.registry import (
    RegistryEndpoint,
    RegistryError,
    RegistryTarget,
    StreamedOciRegistryClient,
)
from piceli.artifacts.retention import (
    LiveWorkloads,
    RetentionError,
    RetentionGrant,
    RetentionPolicy,
    collect_inventory,
    delete_collectable,
    live_digests_from_pods,
    load_releases,
    parse_size,
    plan_retention,
)
from tests.retention_registry import RetentionRegistry

REPO = "app/api"
BASE = b"B" * 1000  # a base layer every release shares


@pytest.fixture
def registry() -> Iterator[RetentionRegistry]:
    fake = RetentionRegistry()
    yield fake
    fake.close()


def release(
    registry: RetentionRegistry, tmp_path: Path, number: int, repo: str = REPO
) -> str:
    """Push release ``number`` (shared base layer + its own) and write its receipt."""
    digest = registry.add_image(
        repo, f"1.{number}", [BASE, f"layer-{number}".encode() * 100]
    )
    receipt = {
        "state": "published",
        "registry": f"127.0.0.1:{registry.port}",
        "finished_at": f"2026-09-{number:02d}T10:00:00.000Z",
        "images": {
            "api": {"repository": f"127.0.0.1:{registry.port}/{repo}", "digest": digest}
        },
    }
    (tmp_path / "receipts").mkdir(exist_ok=True)
    (tmp_path / "receipts" / f"{number:02d}.json").write_text(json.dumps(receipt))
    return digest


def target(registry: RetentionRegistry, prefix: str = "app") -> RegistryTarget:
    return RegistryTarget.parse(f"oci://127.0.0.1:{registry.port}/{prefix}")


def plan_for(
    registry: RetentionRegistry,
    tmp_path: Path,
    policy: RetentionPolicy,
    live: LiveWorkloads | None = None,
) -> tuple[Any, Any]:
    tgt = target(registry)
    releases = load_releases([tmp_path / "receipts"])
    inventory = collect_inventory(tgt, tgt.endpoint(), releases)
    plan = plan_retention(
        tgt, inventory, releases, policy, live or LiveWorkloads(True, frozenset())
    )
    return plan, inventory


def collectable_digests(plan: Any) -> set[str]:
    return {digest for _, digest in plan.collectable}


# ------------------------------------------------------------------ keep rules


def test_keeps_last_n_pinned_and_live_and_collects_the_rest(
    registry: RetentionRegistry, tmp_path: Path
) -> None:
    digests = {n: release(registry, tmp_path, n) for n in range(1, 7)}
    policy = RetentionPolicy(keep=2, pins=frozenset({digests[1]}))
    live = LiveWorkloads(True, frozenset({digests[3]}))
    plan, _ = plan_for(registry, tmp_path, policy, live)
    assert set(plan.kept) == {digests[6], digests[5], digests[1], digests[3]}
    assert plan.kept[digests[1]] == ("pinned",)
    assert plan.kept[digests[3]] == ("live",)
    assert collectable_digests(plan) == {digests[2], digests[4]}


def test_releases_are_ordered_by_time_not_by_file_name(
    registry: RetentionRegistry, tmp_path: Path
) -> None:
    digests = {n: release(registry, tmp_path, n) for n in range(1, 4)}
    # Rename so that the alphabetical order is the reverse of the time order.
    receipts = tmp_path / "receipts"
    for number in (1, 2, 3):
        (receipts / f"{number:02d}.json").rename(receipts / f"z{4 - number}.json")
    plan, _ = plan_for(registry, tmp_path, RetentionPolicy(keep=1))
    assert set(plan.kept) == {digests[3]}


def test_delivery_receipts_and_journals_count_as_releases(
    registry: RetentionRegistry, tmp_path: Path
) -> None:
    first = registry.add_image(REPO, "a", [BASE, b"one" * 50])
    second = registry.add_image(REPO, "b", [BASE, b"two" * 50])
    journal = tmp_path / "journal.jsonl"
    lines = []
    for when, digest in (("2026-09-01", first), ("2026-09-02", second)):
        lines.append(
            json.dumps(
                {
                    "state": "succeeded",
                    "finished_at": when,
                    "target": {"repository": REPO},
                    "image": {"manifest_digest": digest},
                }
            )
        )
    lines.append(json.dumps({"state": "failed", "target": {"repository": REPO}}))
    journal.write_text("\n".join(lines) + "\n")
    releases = load_releases([journal])
    assert [r.digests[0][1] for r in releases] == [second, first]


def test_unledgered_tags_are_kept_unless_asked(
    registry: RetentionRegistry, tmp_path: Path
) -> None:
    for n in (1, 2, 3):
        release(registry, tmp_path, n)
    manual = registry.add_image(REPO, "latest", [BASE, b"manual" * 10])
    plan, _ = plan_for(registry, tmp_path, RetentionPolicy(keep=1))
    assert plan.kept[manual] == ("unledgered",)
    plan, _ = plan_for(
        registry, tmp_path, RetentionPolicy(keep=1, collect_unledgered=True)
    )
    assert manual in collectable_digests(plan)


def test_an_index_keeps_its_children_and_referrers(
    registry: RetentionRegistry, tmp_path: Path
) -> None:
    def indexed(number: int) -> tuple[str, list[str], str]:
        children = [
            registry.add_image(
                REPO, None, [BASE, f"{arch}-{number}".encode() * 40], arch=arch
            )
            for arch in ("amd64", "arm64")
        ]
        index = registry.add_index(REPO, f"2.{number}", children)
        sbom = registry.attest(REPO, children[0], f"sbom{number}".encode())
        receipt = {
            "state": "published",
            "registry": f"127.0.0.1:{registry.port}",
            "finished_at": f"2026-09-{number:02d}",
            "images": {"api": {"repository": f"x/{REPO}", "digest": index}},
        }
        (tmp_path / "receipts").mkdir(exist_ok=True)
        (tmp_path / "receipts" / f"{number}.json").write_text(json.dumps(receipt))
        return index, children, sbom

    old, old_children, old_sbom = indexed(1)
    new, new_children, new_sbom = indexed(2)
    plan, _ = plan_for(registry, tmp_path, RetentionPolicy(keep=1))
    assert set(plan.kept) == {new, *new_children, new_sbom}
    assert collectable_digests(plan) == {old, *old_children, old_sbom}
    assert plan.kept[new_children[0]] == ("referenced",)


# ----------------------------------------------------------------------- budget


def test_budget_counts_shared_blobs_once(
    registry: RetentionRegistry, tmp_path: Path
) -> None:
    digests = {n: release(registry, tmp_path, n) for n in range(1, 6)}
    everything = RetentionPolicy(keep=1, budget=10**9)
    plan, inventory = plan_for(registry, tmp_path, everything)
    assert collectable_digests(plan) == set()

    def cost(numbers: list[int]) -> int:
        return inventory.bytes_of(inventory.closure(digests[n] for n in numbers))

    three = cost([5, 4, 3])
    naive = sum(cost([n]) for n in (5, 4, 3))
    assert three < naive  # the 1000 byte base layer is counted once
    plan, _ = plan_for(registry, tmp_path, RetentionPolicy(keep=1, budget=three))
    assert set(plan.kept) == {digests[5], digests[4], digests[3]}
    assert plan.kept_bytes == three
    plan, _ = plan_for(registry, tmp_path, RetentionPolicy(keep=1, budget=three - 1))
    assert set(plan.kept) == {digests[5], digests[4]}
    # Never below the minimum, whatever the budget.
    plan, _ = plan_for(registry, tmp_path, RetentionPolicy(keep=2, budget=0))
    assert set(plan.kept) == {digests[5], digests[4]}
    # A pinned or live digest counts against the budget before any release.
    pinned = RetentionPolicy(keep=1, budget=three, pins=frozenset({digests[1]}))
    plan, _ = plan_for(registry, tmp_path, pinned)
    assert digests[1] in plan.kept and digests[3] not in plan.kept


def test_reclaimable_bytes_are_the_blobs_nothing_kept_references(
    registry: RetentionRegistry, tmp_path: Path
) -> None:
    for n in range(1, 5):
        release(registry, tmp_path, n)
    plan, _ = plan_for(registry, tmp_path, RetentionPolicy(keep=1))
    assert plan.reclaimable_bytes > 0
    target_ = target(registry)
    inventory = collect_inventory(
        target_, target_.endpoint(), load_releases([tmp_path / "receipts"])
    )
    receipt = delete_collectable(
        plan, inventory, RetentionGrant(plan.digest), target_.endpoint()
    )
    assert receipt["state"] == "deleted"
    assert registry.garbage_collect() == receipt["reclaimed_bytes"]
    assert receipt["reclaimed_bytes"] == plan.reclaimable_bytes
    # The shared base layer is still there for the kept release.
    assert any(len(blob) == 1000 for blob in registry.blobs.values())


# ----------------------------------------------------------------- deleting


def test_delete_removes_exactly_the_approved_set(
    registry: RetentionRegistry, tmp_path: Path
) -> None:
    digests = {n: release(registry, tmp_path, n) for n in range(1, 6)}
    plan, inventory = plan_for(registry, tmp_path, RetentionPolicy(keep=2))
    tgt = target(registry)
    receipt = delete_collectable(
        plan, inventory, RetentionGrant(plan.digest), tgt.endpoint()
    )
    assert {row["digest"] for row in receipt["deleted"]} == {
        digests[1],
        digests[2],
        digests[3],
    }
    assert {d for (_, d) in registry.manifests} == {digests[4], digests[5]}
    assert sorted(registry.deletes()) == sorted(
        f"/v2/{REPO}/manifests/{digests[n]}" for n in (1, 2, 3)
    )


def test_a_grant_for_another_plan_deletes_nothing(
    registry: RetentionRegistry, tmp_path: Path
) -> None:
    for n in range(1, 4):
        release(registry, tmp_path, n)
    plan, inventory = plan_for(registry, tmp_path, RetentionPolicy(keep=1))
    with pytest.raises(RetentionError) as refused:
        delete_collectable(
            plan, inventory, RetentionGrant("0" * 64), target(registry).endpoint()
        )
    assert refused.value.code == "retention-not-approved"
    assert registry.deletes() == []


def test_a_live_digest_is_never_deleted_even_from_a_tampered_plan(
    registry: RetentionRegistry, tmp_path: Path
) -> None:
    digests = {n: release(registry, tmp_path, n) for n in range(1, 4)}
    plan, inventory = plan_for(registry, tmp_path, RetentionPolicy(keep=1))
    assert digests[1] in collectable_digests(plan)
    live = LiveWorkloads(True, frozenset({digests[1]}))
    tampered = dataclasses.replace(plan, live=live)  # same delete list, now live
    with pytest.raises(RetentionError) as refused:
        delete_collectable(
            tampered,
            inventory,
            RetentionGrant(tampered.digest),
            target(registry).endpoint(),
        )
    assert refused.value.code == "retention-not-approved"
    assert registry.deletes() == []


def test_a_registry_that_disallows_deletes_reports_its_code(
    registry: RetentionRegistry, tmp_path: Path
) -> None:
    for n in range(1, 4):
        release(registry, tmp_path, n)
    registry.delete_enabled = False
    plan, inventory = plan_for(registry, tmp_path, RetentionPolicy(keep=1))
    receipt = delete_collectable(
        plan, inventory, RetentionGrant(plan.digest), target(registry).endpoint()
    )
    assert receipt["state"] == "failed"
    assert receipt["reason"] == "registry-delete-disabled"
    assert receipt["deleted"] == []
    assert len(registry.manifests) == 3


def test_deleting_needs_a_known_live_inventory(
    registry: RetentionRegistry, tmp_path: Path
) -> None:
    for n in range(1, 4):
        release(registry, tmp_path, n)
    plan, inventory = plan_for(
        registry, tmp_path, RetentionPolicy(keep=1), LiveWorkloads.unknown()
    )
    with pytest.raises(RetentionError) as refused:
        delete_collectable(
            plan, inventory, RetentionGrant(plan.digest), target(registry).endpoint()
        )
    assert refused.value.code == "retention-live-unknown"


# ------------------------------------------------------------ inputs and client


def test_live_digests_from_pod_specs_and_statuses() -> None:
    a, b, c = ("sha256:" + x * 64 for x in "abc")
    pods = [
        {
            "spec": {
                "containers": [{"image": f"127.0.0.1:5000/app/api@{a}"}],
                "initContainers": [{"image": "localhost:5000/app/job:1.2"}],
            },
            "status": {
                "containerStatuses": [
                    {
                        "image": "127.0.0.1:5000/app/api:1.0",
                        "imageID": f"docker-pullable://127.0.0.1:5000/app/api@{b}",
                    }
                ],
                "initContainerStatuses": [
                    {"imageID": f"127.0.0.1:5000/app/db@{c}", "image": "x"}
                ],
            },
        }
    ]
    digests, tags = live_digests_from_pods(pods)
    assert digests == {a, b, c}
    assert ("app/job", "1.2") in tags and ("app/api", "1.0") in tags


def test_a_tag_only_pod_keeps_the_tags_digest(
    registry: RetentionRegistry, tmp_path: Path
) -> None:
    digests = {n: release(registry, tmp_path, n) for n in range(1, 4)}
    live = LiveWorkloads(True, frozenset(), frozenset({(REPO, "1.1")}))
    plan, _ = plan_for(registry, tmp_path, RetentionPolicy(keep=1), live)
    assert plan.kept[digests[1]] == ("live",)


def test_sizes_and_policy_are_validated() -> None:
    assert parse_size("10GiB") == 10 * 1024**3
    assert parse_size("1.5MB") == 1_500_000
    assert parse_size("512") == 512
    for bad in ("", "GiB", "10XB", "-1"):
        with pytest.raises(RetentionError):
            parse_size(bad)
    for keep in (0, -1, True):
        with pytest.raises(RetentionError):
            RetentionPolicy(keep=keep)  # type: ignore[arg-type]
    with pytest.raises(RetentionError):
        RetentionPolicy(pins=frozenset({"latest"}))


def test_the_client_pages_tags_and_reads_a_missing_catalog(
    registry: RetentionRegistry,
) -> None:
    for n in range(5):
        registry.add_image(REPO, f"t{n}", [BASE, bytes([n]) * 10])
    registry.page = 2
    client = StreamedOciRegistryClient(
        RegistryEndpoint("127.0.0.1", registry.port, False), actions="pull,delete"
    )
    assert client.list_tags(REPO) == [f"t{n}" for n in range(5)]
    assert client.list_repositories() == [REPO]
    assert client.list_tags("missing/repo") == []
    registry.catalog = False
    assert client.list_repositories() is None
    with pytest.raises(RegistryError) as refused:
        registry.delete_enabled = False
        client.delete_manifest(REPO, next(iter(registry.tags.values())))
    assert refused.value.reason == "registry-delete-disabled"


# ---------------------------------------------------------------------- the CLI


def run(
    capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, dict[str, Any], str]:
    code = main(["retention", *args])
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    return code, json.loads(lines[-1]) if lines else {}, captured.err


def common(registry: RetentionRegistry, tmp_path: Path) -> list[str]:
    return [
        "--to",
        f"oci://127.0.0.1:{registry.port}/app",
        "--receipts",
        str(tmp_path / "receipts"),
        "--keep",
        "2",
    ]


def test_cli_reports_then_asks_approval_then_deletes(
    registry: RetentionRegistry, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    digests = {n: release(registry, tmp_path, n) for n in range(1, 6)}
    live = tmp_path / "live.txt"
    live.write_text(f"# in use\n{digests[2]}\n")
    base = [*common(registry, tmp_path), "--live-file", str(live)]

    code, report, _ = run(capsys, *base)
    assert code == 0 and report["state"] == "report"
    assert report["collectable"]["manifests"] == 2
    assert {d["digest"] for d in report["collectable"]["deletions"]} == {
        digests[1],
        digests[3],
    }
    assert report["kept"]["manifests"] == 3
    assert registry.deletes() == []

    code, asked, err = run(capsys, *base, "--delete")
    assert code == 3 and asked["state"] == "approval-required"
    assert asked["digest"] == report["digest"]
    assert report["digest"] in err and registry.deletes() == []

    code, refused, _ = run(capsys, *base, "--delete", "--approve", "0" * 64)
    assert (code, refused["reason"]) == (2, "retention-not-approved")
    assert registry.deletes() == []

    code, done, _ = run(capsys, *base, "--delete", "--approve", report["digest"])
    assert code == 0 and done["state"] == "deleted"
    assert {row["digest"] for row in done["deleted"]} == {digests[1], digests[3]}
    assert {d for (_, d) in registry.manifests} == {digests[2], digests[4], digests[5]}
    assert registry.garbage_collect() == done["reclaimed_bytes"] > 0


def test_cli_refuses_an_approval_when_the_registry_changed(
    registry: RetentionRegistry, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for n in range(1, 5):
        release(registry, tmp_path, n)
    live = tmp_path / "live.txt"
    live.write_text("[]")
    base = [*common(registry, tmp_path), "--live-file", str(live)]
    _, report, _ = run(capsys, *base)
    release(registry, tmp_path, 5)  # a new release lands after the review
    code, refused, _ = run(capsys, *base, "--delete", "--approve", report["digest"])
    assert (code, refused["reason"]) == (2, "retention-not-approved")
    assert registry.deletes() == []


def test_cli_a_workload_that_starts_using_a_digest_changes_the_plan(
    registry: RetentionRegistry, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    digests = {n: release(registry, tmp_path, n) for n in range(1, 5)}
    live = tmp_path / "live.txt"
    live.write_text("[]")
    base = [*common(registry, tmp_path), "--live-file", str(live)]
    _, report, _ = run(capsys, *base)
    live.write_text(json.dumps([digests[1]]))  # a rollback pod now runs release 1
    code, refused, _ = run(capsys, *base, "--delete", "--approve", report["digest"])
    assert (code, refused["reason"]) == (2, "retention-not-approved")
    assert digests[1] in {d for (_, d) in registry.manifests}


def test_cli_delete_needs_a_live_source(
    registry: RetentionRegistry, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for n in range(1, 4):
        release(registry, tmp_path, n)
    code, refused, _ = run(capsys, *common(registry, tmp_path), "--delete")
    assert (code, refused["reason"]) == (2, "retention-live-unknown")
    # A report alone does not need one, and says so.
    code, report, _ = run(capsys, *common(registry, tmp_path))
    assert code == 0 and report["live"]["read"] is False


def test_cli_input_errors_are_fixed_codes(
    registry: RetentionRegistry, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = common(registry, tmp_path)
    for extra in (
        ["--keep", "0"],
        ["--budget", "lots"],
        ["--pin", "latest"],
        ["--approve", "abc"],
        ["--kubeconfig", str(tmp_path / "k")],
    ):
        code, refused, _ = run(capsys, *base, *extra)
        assert (code, refused["reason"]) == (2, "retention-invalid"), extra


def test_cli_budget_and_pin_file(
    registry: RetentionRegistry, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    digests = {n: release(registry, tmp_path, n) for n in range(1, 6)}
    pins = tmp_path / "pins"
    pins.write_text(f"{digests[1]}  # the LTS\n")
    base = [
        "--to",
        f"oci://127.0.0.1:{registry.port}/app",
        "--receipts",
        str(tmp_path / "receipts"),
        "--keep",
        "1",
        "--pin-file",
        str(pins),
    ]
    _, small, _ = run(capsys, *base, "--budget", "1KB")
    _, large, _ = run(capsys, *base, "--budget", "1GB")
    assert small["collectable"]["manifests"] == 3  # 2..4 go; 5 (keep) and 1 (pin) stay
    assert large["collectable"]["manifests"] == 0
    assert small["policy"]["budget_bytes"] == 1000


# ------------------------------------------------- a node registry's growth model

MB = 1_000_000


def test_a_node_registry_fed_by_thirty_releases_stays_within_its_budget(
    registry: RetentionRegistry, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Three images share a runtime layer each and change one 300 MB layer per
    release (about 900 MB of new data per release, as a Rust app's node
    registry grows). Thirty releases are ~28 GB; a 5 GiB budget keeps the
    newest releases that fit, and the space the collector frees is reported."""
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    digests: dict[int, list[str]] = {}
    for number in range(1, 31):
        images = {}
        for name in ("api", "worker", "web"):
            digest = registry.add_sparse_image(
                f"app/{name}",
                f"1.{number}",
                [(f"runtime-{name}", 300 * MB), (f"binary-{name}-{number}", 300 * MB)],
            )
            digests.setdefault(number, []).append(digest)
            images[name] = {
                "repository": f"127.0.0.1:{registry.port}/app/{name}",
                "digest": digest,
            }
        receipt = {
            "state": "published",
            "registry": f"127.0.0.1:{registry.port}",
            "finished_at": f"2026-09-{number:02d}T10:00:00Z",
            "images": images,
        }
        (receipts / f"{number:02d}.json").write_text(json.dumps(receipt))
    budget = 5 * 1024**3
    base = [
        "--to",
        f"oci://127.0.0.1:{registry.port}/app",
        "--receipts",
        str(receipts),
        "--keep",
        "2",
        "--budget",
        "5GiB",
        "--live-file",
        str(tmp_path / "live"),
    ]
    (tmp_path / "live").write_text(f"{digests[29][0]}\n")  # a pod still runs 29's api
    code, report, _ = run(capsys, *base)
    assert code == 0
    kept = [item for item in report["releases"] if item["kept"]]
    assert report["kept"]["bytes"] <= budget
    # 900 MB of shared runtimes plus 900 MB per release: four releases fit in
    # 5 GiB (4.5 GB), five do not (5.4 GB). Release 29's api is live anyway.
    assert len(kept) == 4 and kept[0]["release"].startswith("30")
    assert report["collectable"]["manifests"] == 3 * 26
    assert 4.4e9 < report["kept"]["bytes"] <= budget
    everything = 30 * 900 * MB + 900 * MB
    assert report["kept"]["bytes"] + report["collectable"]["reclaimable_bytes"] == (
        pytest.approx(everything, rel=0.001)
    )
    code, done, _ = run(capsys, *base, "--delete", "--approve", report["digest"])
    assert (
        code == 0
        and done["reclaimed_bytes"] == report["collectable"]["reclaimable_bytes"]
    )
    code, again, _ = run(capsys, *base)
    assert again["collectable"]["manifests"] == 0
    assert again["kept"]["bytes"] == report["kept"]["bytes"]
