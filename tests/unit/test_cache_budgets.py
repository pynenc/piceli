"""Cache budgets, disk-pressure alarms and fail-fast scheduling (0.35.0).

On a storage class without quotas (k3s ``local-path``) a cache claim is a
directory on the node's disk: Piceli itself keeps each build cache within
its budget, the controller alarms on node disk pressure and on a cache still
over budget, the cache-clear Jobs run on a node under disk pressure, and a
development run whose pod finds no node fails within a minute with the
scheduler's reason. Everything runs on plain directories and fakes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts import cache_budget
from piceli.dev import pod
from piceli.dev.model import DevBuilds, DevError
from piceli.gitops.alarms import changes, storage_alarms
from tests.unit.infra.test_controller_otel import world  # noqa: F401 (fixture)

IMAGE = "ghcr.io/x/builder@sha256:" + "a" * 64
NOW = 2_000_000_000.0
GiB = 2**30


def _lineage(cache: Path, name: str, size: int, **meta: Any) -> Path:
    path = cache / "lineages" / name
    (path / "target").mkdir(parents=True)
    (path / "target" / "blob").write_bytes(b"x" * size)
    if meta is not None:
        (path / "meta.json").write_text(json.dumps(meta))
    return path


# ------------------------------------------------------- development cache


def test_everything_in_the_dev_cache_counts_against_its_budget(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    old = _lineage(cache, "app-rust/0", 8192, last_used=1, bytes=8192)
    new = _lineage(cache, "app-rust/1", 8192, last_used=2, bytes=8192)
    (cache / "toolchains").mkdir()
    (cache / "toolchains" / "rustc").write_bytes(b"x" * 16384)
    # The lineages alone fit; with the toolchains the cache does not.
    report = pod.evict(cache, 30_000, now=NOW)
    assert report["evicted"] == ["app-rust/0"] and not old.exists() and new.exists()
    assert report["other_bytes"] >= 16384 and report["evicted_for"] == "over-budget"
    assert report["used_bytes"] <= 30_000 and report["over_budget"] is False
    kept = json.loads((cache / pod.BUDGET_FILE).read_text())
    assert kept["schema"] == "piceli.cache-budget.v1" and kept["other_at"] == NOW


def test_lineages_a_killed_run_left_are_measured_and_evicted(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    # Never released: the meta still says what it held before that run.
    unreleased = _lineage(
        cache, "a-rust/0", 65536, last_used=5, bytes=10, taken_at=6, runs=1
    )
    # A first run killed before it wrote any meta.
    bare = cache / "lineages" / "b-rust" / "0"
    (bare / "target").mkdir(parents=True)
    (bare / "target" / "blob").write_bytes(b"x" * 65536)
    os.utime(bare, (1, 1))
    report = pod.evict(cache, 70_000, now=NOW)
    assert report["evicted"] == ["b-rust/0"] and not bare.exists()
    meta = json.loads((unreleased / "meta.json").read_text())
    assert meta["bytes"] >= 65536 and "taken_at" not in meta and meta["last_used"] == 5


def test_a_lineage_a_run_holds_is_counted_never_evicted(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    held = _lineage(cache, "app-rust/0", 4096, last_used=1, bytes=50_000)
    descriptor = pod._try_lock(held)
    assert descriptor is not None
    try:
        report = pod.evict(cache, 10_000, now=NOW)
    finally:
        os.close(descriptor)
    assert held.exists() and report["evicted"] == []
    assert report["over_budget"] is True and report["used_bytes"] >= 50_000


def test_a_nearly_full_disk_evicts_even_under_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    first = _lineage(cache, "app-rust/0", 4096, last_used=1, bytes=4096)
    second = _lineage(cache, "app-rust/1", 4096, last_used=2, bytes=4096)
    disk = {"free": 10}

    def filesystem(_path: Path) -> tuple[int, int]:
        return disk["free"], 100

    monkeypatch.setattr(pod, "filesystem", filesystem)
    original = pod._remove_tree

    def remove(path: Path) -> None:
        original(path)
        disk["free"] += 10  # each lineage frees 10% of the disk

    monkeypatch.setattr(pod, "_remove_tree", remove)
    report = pod.evict(cache, 10**12, min_free_ratio=0.15, now=NOW)
    assert (
        report["evicted"] == ["app-rust/0"]
        and report["evicted_for"] == "filesystem-low"
    )
    assert not first.exists() and second.exists() and report["fs_low"] is False


def test_a_run_reports_its_budget_check(tmp_path: Path) -> None:
    from tests.unit.dev.test_pod import _archive, _run, _spec

    (tmp_path / "cache" / "git").mkdir(parents=True)
    (tmp_path / "cache" / "git" / "mirror").write_bytes(b"x" * 4096)
    data = _archive({"app/a.txt": "x"})
    result, lines = _run(tmp_path, data, _spec(data, ["true"], cache_max_bytes=1))
    cache = result["cache"]
    assert cache["over_budget"] is True and cache["max_bytes"] == 1
    assert {"other_bytes", "fs_free_bytes", "fs_total_bytes", "fs_low"} <= set(cache)
    assert any(line.startswith("[piceli-dev] cache over budget") for line in lines)


def test_the_budget_is_a_share_of_the_claim_and_runs_carry_the_disk_floor() -> None:
    from piceli.dev.jobs import run_job

    dev = DevBuilds(node="builder-1", image=IMAGE, cache_size="100Gi")
    assert dev.cache_budget == 0.8
    job = run_job(
        dev, dev.profile(None), run="r1", spec={}, requester="a", priority="agent"
    )
    env = {
        item["name"]: item.get("value")
        for item in job["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    spec = json.loads(env["PICELI_DEV_SPEC"])
    assert spec["cache_max_bytes"] == int(100 * GiB * 0.8)
    assert spec["cache_min_free_ratio"] == pod.MIN_FREE_RATIO
    again = DevBuilds.from_dict({**dev.describe(), "cache_budget": 0.5})
    assert again.cache_budget == 0.5
    from piceli.infra.cluster import ClusterError

    with pytest.raises(ClusterError):
        DevBuilds(node="builder-1", image=IMAGE, cache_budget=1.0)


# ------------------------------------------------------- component cache


def test_the_component_cache_evicts_its_least_recently_used_targets(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    for unit in ("target/amd64-4096", "target/arm64-16384", "stage/web", "blobs"):
        (cache / unit).mkdir(parents=True)
        (cache / unit / "data").write_bytes(b"x" * 16384)
    cache_budget.mark_used(cache, [cache / "target/arm64-16384"], now=1)
    cache_budget.mark_used(
        cache,
        [cache / "target/amd64-4096", cache / "stage/web", cache / "blobs"],
        now=2,
    )
    report = cache_budget.enforce(cache, 50_000, now=NOW)
    assert report["evicted"] == ["target/arm64-16384"]
    assert not (cache / "target/arm64-16384").exists()
    assert (cache / "target/amd64-4096").exists() and (cache / "blobs").exists()
    used = json.loads((cache / cache_budget.USED_FILE).read_text())
    assert "target/arm64-16384" not in used and used["stage/web"] == 2


def test_build_jobs_carry_their_budget_and_receipts_report_it(tmp_path: Path) -> None:
    from piceli.infra.builders import JobSettings, component_job

    settings = JobSettings(
        image=IMAGE,
        namespace="piceli-system",
        registry_url="oci://registry:5000/x",
        node_registry="registry:5000",
        storage="10Gi",
    )
    job = component_job(settings, [], {})
    env = {
        item["name"]: item.get("value")
        for item in job["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env[cache_budget.MAX_BYTES_ENV] == str(int(10 * GiB * 0.8))
    assert env[cache_budget.MIN_FREE_ENV] == str(pod.MIN_FREE_RATIO)
    cache = tmp_path / "cache"
    (cache / "target" / "amd64-4096").mkdir(parents=True)
    (cache / "target" / "amd64-4096" / "x").write_bytes(b"x" * 8192)
    assert cache_budget.from_environment(cache, {}) is None
    said: list[str] = []
    report = cache_budget.from_environment(
        cache, {cache_budget.MAX_BYTES_ENV: "1"}, say=said.append
    )
    assert report is not None and report["evicted"] == ["target/amd64-4096"]
    assert said[0].startswith("[build] cache budget: evicted target/amd64-4096")


# ------------------------------------------------------- cache-clear Jobs


def test_cache_clear_jobs_run_on_a_node_under_disk_pressure() -> None:
    from piceli.dev.jobs import cache_clear_job as dev_clear
    from piceli.infra.builders import JobSettings
    from piceli.infra.builders import cache_clear_job as build_clear

    toleration = {
        "key": "node.kubernetes.io/disk-pressure",
        "operator": "Exists",
        "effect": "NoSchedule",
    }
    dev = DevBuilds(node="builder-1", image=IMAGE)
    settings = JobSettings(
        image=IMAGE,
        namespace="piceli-system",
        registry_url="oci://registry:5000/x",
        node_registry="registry:5000",
    )
    for job in (
        dev_clear(dev, name="abcd", lineage=None, crates=[]),
        build_clear(settings, [], "abcd"),
    ):
        assert job["spec"]["template"]["spec"]["tolerations"] == [toleration]


# ------------------------------------------------------- alarms


def _status(storage: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "piceli.gitops-status.v1",
        "controller": {"version": "0.35.0", "storage": storage},
        "envs": {},
    }


def test_disk_pressure_and_a_cache_over_budget_alarm_and_resolve() -> None:
    storage = {
        "nodes": {
            "builder-1": {"disk_pressure": True, "since": "2033-05-18T00:00:00Z"}
        },
        "caches": {
            "piceli-dev/piceli-dev-cache": {
                "used_bytes": 130 * GiB,
                "max_bytes": 64 * GiB,
                "over_budget": True,
                "fs_low": True,
                "clear": "piceli dev cache clear",
            },
            "piceli-system/piceli-component-cache": {
                "used_bytes": GiB,
                "max_bytes": 16 * GiB,
                "over_budget": False,
            },
        },
    }
    memory: dict[str, Any] = {"version": "0.35.0"}
    found = {alarm.key: alarm for alarm in changes(memory, _status(storage), NOW, 600)}
    assert set(found) == {
        "node-disk-pressure:builder-1",
        "cache-over-budget:piceli-dev/piceli-dev-cache",
    }
    pressure = found["node-disk-pressure:builder-1"]
    assert pressure.title == "piceli: node builder-1 disk pressure"
    assert "DiskPressure since 2033-05-18T00:00:00Z" in pressure.text
    over = found["cache-over-budget:piceli-dev/piceli-dev-cache"]
    assert "130.0 GiB of a 64.0 GiB budget" in over.text
    assert over.text.endswith("Next: piceli dev cache clear")
    assert changes(memory, _status(storage), NOW + 60, 600) == []  # once
    resolved = changes(memory, _status({}), NOW + 120, 600)
    assert sorted(alarm.key for alarm in resolved) == sorted(found)
    assert all(alarm.state == "resolved" and alarm.ok for alarm in resolved)
    assert resolved[0].title.endswith("has disk space again")
    assert storage_alarms({}) == []


def test_the_controller_publishes_storage_and_alarms_on_it(
    world: dict[str, Any],  # noqa: F811
) -> None:
    controller, ports = world["controller"], world["ports"]
    reads: list[int] = []

    def storage() -> dict[str, Any]:
        reads.append(1)
        return {
            "nodes": {
                "builder-1": {"disk_pressure": True, "since": "2033-05-18T00:00:00Z"}
            },
            "dev_cache": {
                "used_bytes": 90 * GiB,
                "max_bytes": 64 * GiB,
                "over_budget": True,
                "at": "2033-05-18T00:00:00Z",
            },
        }

    ports.storage = storage
    ports.builder.cache_report = {
        "used_bytes": GiB,
        "max_bytes": 16 * GiB,
        "over_budget": False,
        "fs_low": False,
        "at": NOW,
        "claim": "piceli-component-cache",
        "namespace": "piceli-system",
    }
    status = controller.poll_once()
    found = status["controller"]["storage"]
    assert found["nodes"] == {
        "builder-1": {"disk_pressure": True, "since": "2033-05-18T00:00:00Z"}
    }
    dev = found["caches"]["piceli-dev/piceli-dev-cache"]
    assert dev["over_budget"] is True and dev["clear"] == "piceli dev cache clear"
    component = found["caches"]["piceli-system/piceli-component-cache"]
    assert component["at"] == "2033-05-18T03:33:20Z"
    assert component["clear"] == "piceli gitops cache clear"
    controller.poll_once()
    assert reads == [1]  # read at most once a minute
    alarms = {a.key for a in changes({}, status, NOW, 600)}
    assert alarms == {
        "node-disk-pressure:builder-1",
        "cache-over-budget:piceli-dev/piceli-dev-cache",
    }


# ------------------------------------------------------- fail fast


class _Api:
    """``piceli-dev``'s API for one run whose pod the scheduler cannot place."""

    def __init__(self, conditions: list[dict[str, Any]]) -> None:
        self.conditions = conditions

    def call(self, path: str, method: str, *args: Any, **kwargs: Any) -> Any:
        if "/jobs" in path:
            return {"items": [{"metadata": {"name": "j"}, "spec": {}}]}
        if "/pods" in path:
            return {
                "items": [
                    {
                        "metadata": {"name": "p"},
                        "status": {"phase": "Pending", "conditions": self.conditions},
                    }
                ]
            }
        return None


UNSCHEDULABLE = {
    "type": "PodScheduled",
    "status": "False",
    "reason": "Unschedulable",
    "message": "0/3 nodes are available: 1 node(s) had untolerated taint "
    "{node.kubernetes.io/disk-pressure: }.",
}


def test_a_run_whose_pod_finds_no_node_fails_fast_with_the_reason() -> None:
    from piceli.dev.cluster import DevCluster

    port = DevCluster(_Api([UNSCHEDULABLE]), poll_seconds=0.01)
    said: list[str] = []
    with pytest.raises(DevError) as raised:
        port.wait_started("r1", 60, said.append, unschedulable_timeout=0.05)
    assert raised.value.code == "dev-run-unschedulable"
    assert "disk-pressure" in str(raised.value)
    assert said[0].startswith("Unschedulable: 0/3 nodes are available")


def test_a_new_run_says_the_queue_cache_and_node_state_at_once() -> None:
    from piceli.dev.client import queue_lines
    from piceli.dev.scheduler import disk_pressure

    node = {
        "metadata": {"name": "builder-1"},
        "status": {
            "conditions": [
                {
                    "type": "DiskPressure",
                    "status": "True",
                    "lastTransitionTime": "2033-05-18T00:00:00Z",
                }
            ]
        },
    }
    pressure = disk_pressure(node)
    assert pressure is not None and pressure["node"] == "builder-1"
    assert disk_pressure({"status": {"conditions": []}}) is None
    lines = queue_lines(
        {
            "slots": 2,
            "running": [{}, {}],
            "queued": [{}],
            "cache": {
                "used_bytes": 70 * GiB,
                "max_bytes": 64 * GiB,
                "over_budget": True,
            },
            "node_disk_pressure": pressure,
        }
    )
    assert lines == [
        "queue: 2 running of 2 slot(s), 1 queued",
        "cache: 70.0 GiB of a 64.0 GiB budget (over budget)",
        "node builder-1 is under disk pressure since 2033-05-18T00:00:00Z: runs "
        "cannot start there; piceli dev cache clear frees the cache",
    ]
    assert queue_lines(None) == []


# ------------------------------------------------------- 0.38.1: during a run


def test_a_lineage_growing_past_its_cap_stops_the_run_and_is_evicted(
    tmp_path: Path,
) -> None:
    import time

    from tests.unit.dev.test_pod import _archive, _run, _spec

    data = _archive({"app/a.txt": "x"})
    grow = 'head -c 4000000 /dev/zero > "$CARGO_TARGET_DIR/blob"; sleep 30'
    spec = _spec(
        data,
        ["sh", "-c", grow],
        cache_max_bytes=1_000_000,
        cache_watch_seconds=0.05,
        cache_measure_seconds=0.05,
    )
    began = time.monotonic()
    result, lines = _run(tmp_path, data, spec)
    assert time.monotonic() - began < 20  # stopped, not the 30 s sleep
    assert result["state"] == "error" and result["reason"] == "dev-cache-over-cap"
    cache = result["cache"]
    assert cache["over_cap"] is True and cache["stopped_for"] == "over-cap"
    assert cache["cap_bytes"] <= 1_000_000 < cache["lineage_bytes"]
    assert not Path(cache["lineage_dir"]).exists()  # evicted right after
    assert cache["trim"]["dropped_bytes"] > cache["cap_bytes"]
    assert cache["trim"]["over_budget"] is False
    assert any(line.startswith("[piceli-dev] run stopped") for line in lines)


def test_the_watch_measures_only_when_the_disk_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lineage = _lineage(tmp_path / "cache", "app-rust/0", 4096)
    disk = {"used": 1000}
    clock = {"now": 0.0}
    monkeypatch.setattr(pod, "filesystem", lambda _p: (10**9 - disk["used"], 10**9))
    watch = pod.CacheWatch(
        lineage, 100_000, 4096, every=1, measure_every=5, clock=lambda: clock["now"]
    )
    for now in (1, 2, 3):  # the disk does not move: no du
        clock["now"] = now
        assert watch() is False
    disk["used"] += 200_000  # the disk lost more than the cap leaves
    clock["now"] = 4
    assert watch() is False and watch.measured == 0  # du at most every 5 s
    clock["now"] = 6
    # du: another run used that disk, this lineage still fits.
    assert watch() is False and watch.measured == 1 and watch.bytes < 100_000
    (lineage / "target" / "more").write_bytes(b"x" * 200_000)
    disk["used"] += 200_000
    clock["now"] = 12
    assert watch() is True and watch.stopped == "over-cap" and watch.bytes > 100_000
    assert json.loads((lineage / "meta.json").read_text())["bytes"] == watch.bytes


def test_every_run_trims_the_cache_to_its_budget_when_it_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.dev.test_pod import _archive, _run, _spec

    old = _lineage(tmp_path / "cache", "other-rust/0", 4096, last_used=1, bytes=4096)
    full = tmp_path / "full"  # the command fills the disk
    monkeypatch.setattr(
        pod, "filesystem", lambda _p: (10 if full.exists() else 90, 100)
    )
    original = pod._remove_tree

    def remove(path: Path) -> None:
        original(path)
        full.unlink(missing_ok=True)  # evicting frees it again

    monkeypatch.setattr(pod, "_remove_tree", remove)
    data = _archive({"app/a.txt": "x"})
    spec = _spec(data, ["touch", str(full)], cache_min_free_ratio=0.15)
    result, lines = _run(tmp_path, data, spec)
    assert result["state"] == "passed" and result["cache"]["evicted"] == []
    trim = result["cache"]["trim"]
    assert trim["evicted"] == ["other-rust/0"] and not old.exists()
    assert trim["fs_low"] is False and Path(result["cache"]["lineage_dir"]).exists()
    assert any("cache budget after the run" in line for line in lines)


def test_the_scheduler_holds_runs_while_the_cache_is_over_budget() -> None:
    from piceli.dev.client import queue_lines
    from piceli.dev.scheduler import Scheduler
    from tests.unit.dev.test_scheduler import FakePort, _dev, job

    def over(flag: bool) -> dict[str, Any]:
        trim = {"used_bytes": 70 * GiB if flag else 40 * GiB, "over_budget": flag}
        return {
            "state": "passed",
            "cache": {"used_bytes": 50 * GiB, "max_bytes": 64 * GiB, "trim": trim},
        }

    def finish(port: FakePort, run: str) -> None:
        for item in port.items:
            if item["metadata"]["labels"]["piceli.io/dev-run"] == run:
                item["status"]["conditions"] = [{"type": "Complete", "status": "True"}]

    port = FakePort(
        [
            job("done", suspend=False, done="Complete"),
            job("busy", suspend=False),
            job("q1", created="2026-10-08T07:00:01Z"),
            job("q2", created="2026-10-08T07:00:02Z"),
        ],
        {"done": over(True), "busy": over(True), "q1": over(False)},
    )
    said: list[str] = []
    scheduler = Scheduler(port, _dev(4), clock=lambda: 1000.0, say=said.append)
    status = scheduler.tick()
    # Over budget after the last run's trim: nothing starts beside "busy".
    assert port.unsuspended == [] and status["held"]["cause"] == "cache-over-budget"
    assert status["cache"]["used_bytes"] == 70 * GiB
    assert any(line.startswith("dev scheduler: runs held") for line in said)
    assert queue_lines(status)[-1].startswith("runs held: the cache holds 70.0 GiB")
    finish(port, "busy")
    scheduler.tick()  # none running: one run starts and trims at its start
    assert port.unsuspended == ["q1"]
    scheduler.tick()
    assert port.unsuspended == ["q1"]  # one at a time
    finish(port, "q1")
    status = scheduler.tick()  # q1 left the cache under budget
    assert port.unsuspended == ["q1", "q2"] and "held" not in status


def test_the_scheduler_starts_nothing_on_a_node_under_disk_pressure() -> None:
    from piceli.dev.scheduler import Scheduler
    from tests.unit.dev.test_scheduler import FakePort, _dev, job

    class Pressed(FakePort):
        def node(self, name: str) -> dict[str, Any]:
            return {
                "metadata": {"name": name},
                "status": {
                    "allocatable": {"cpu": "8", "memory": "16Gi"},
                    "conditions": [{"type": "DiskPressure", "status": "True"}],
                },
            }

    port = Pressed([job("q1")])
    status = Scheduler(port, _dev(2), clock=lambda: 1000.0).tick()
    assert port.unsuspended == [] and status["held"]["cause"] == "node-disk-pressure"
