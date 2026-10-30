"""A node under disk pressure that holds a Piceli cache recovers (0.38.0).

Under DiskPressure the kubelet admits only critical pods, so the cache-clear
Jobs are ``system-node-critical``; a pod the node refuses or evicts fails
the clear with the node's reason. No run or build starts on such a node, so
the controller itself evicts the cache to its budget, at most once per poll
window, publishes the measured use and stays ``degraded`` with a plain
reason while the pressure lasts. Everything runs on plain directories and
fakes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from piceli.dev import pod
from piceli.dev.model import DevBuilds, DevError
from tests.unit.infra.test_controller_otel import world  # noqa: F401 (fixture)

IMAGE = "ghcr.io/x/builder@sha256:" + "a" * 64
GiB = 2**30
PRESSURE = {"builder-1": {"disk_pressure": True, "since": "2033-05-18T00:00:00Z"}}
EVICTED = {
    "metadata": {"name": "p"},
    "status": {
        "phase": "Failed",
        "reason": "Evicted",
        "message": "The node had condition: [DiskPressure].",
    },
}


def _settings() -> Any:
    from piceli.infra.builders import JobSettings

    return JobSettings(
        image=IMAGE,
        namespace="piceli-system",
        registry_url="oci://registry:5000/x",
        node_registry="registry:5000",
        selector={"kubernetes.io/hostname": "builder-1"},
    )


# ------------------------------------------------------- the Jobs


def test_cache_clear_jobs_are_critical_and_keep_their_toleration() -> None:
    from piceli.dev.jobs import cache_clear_job as dev_clear
    from piceli.infra.builders import cache_clear_job as build_clear

    dev = DevBuilds(node="builder-1", image=IMAGE)
    for job in (
        dev_clear(dev, name="abcd", lineage=None, crates=[]),
        dev_clear(dev, name="abcd", lineage=None, crates=[], evict=True),
        build_clear(_settings(), [], "abcd"),
        build_clear(_settings(), [], "abcd", evict=True),
    ):
        spec = job["spec"]["template"]["spec"]
        assert spec["priorityClassName"] == "system-node-critical"
        assert spec["tolerations"][0]["key"] == "node.kubernetes.io/disk-pressure"
    evicting = dev_clear(dev, name="abcd", lineage=None, crates=[], evict=True)
    env = evicting["spec"]["template"]["spec"]["containers"][0]["env"]
    spec = json.loads(next(e["value"] for e in env if e["name"] == "PICELI_DEV_SPEC"))
    assert spec["evict"] is True and spec["cache_max_bytes"] == int(200 * GiB * 0.8)
    build = build_clear(_settings(), [], "abcd", evict=True)
    container = build["spec"]["template"]["spec"]["containers"][0]
    assert "from_environment" in container["command"][-1]
    assert {e["name"] for e in container["env"]} == {
        "PICELI_CACHE_MAX_BYTES",
        "PICELI_CACHE_MIN_FREE_RATIO",
    }


def test_the_clear_role_evicts_to_the_budget(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    for name in ("k/0", "k/1"):
        path = cache / "lineages" / name
        (path / "target").mkdir(parents=True)
        (path / "target" / "blob").write_bytes(b"x" * 4096)
        (path / "meta.json").write_text(json.dumps({"bytes": 4096, "last_used": 1}))
    lines: list[str] = []
    result = pod.clear_main(
        {"evict": True, "cache_max_bytes": 1, "cache_min_free_ratio": 0},
        cache,
        lines.append,
    )
    assert sorted(result["budget"]["evicted"]) == ["k/0", "k/1"]
    assert lines[-1].startswith(pod.RESULT_MARKER)


class _Api:
    """``piceli-dev`` for one cache-clear Job: its pod (or none) and events."""

    def __init__(self, pods: list[dict[str, Any]], events: list[Any] = ()) -> None:
        self.pods = pods
        self.events = list(events)
        self.deleted: list[str] = []

    def call(self, path: str, method: str, *args: Any, **kwargs: Any) -> Any:
        if method == "DELETE":
            self.deleted.append(path)
            return None
        if "/events" in path:
            return {"items": self.events}
        if "/pods" in path and method == "GET":
            return {"items": self.pods}
        return {}


def test_an_evicted_clear_pod_fails_with_the_node_reason() -> None:
    from piceli.dev.cluster import DevCluster
    from piceli.dev.jobs import cache_clear_job

    job = cache_clear_job(
        DevBuilds(node="builder-1", image=IMAGE), name="abcd", lineage=None, crates=[]
    )
    api = _Api([EVICTED])
    with pytest.raises(DevError) as raised:
        DevCluster(api, poll_seconds=0.01).clear_cache(job, timeout=5)
    assert raised.value.code == "dev-cache-clear-failed"
    assert str(raised.value) == (
        "the cache-clear pod was rejected by the node: "
        "Evicted: The node had condition: [DiskPressure]."
    )
    assert api.deleted  # the Job is removed on every outcome
    quota = {
        "reason": "FailedCreate",
        "message": 'pods "x" is forbidden: insufficient quota to match these '
        "scopes: [{PriorityClass In [system-node-critical]}]",
    }
    with pytest.raises(DevError) as raised:
        DevCluster(_Api([], [quota]), poll_seconds=0.01).clear_cache(job, timeout=5)
    assert "not created" in str(raised.value) and "quota" in str(raised.value)


def test_the_build_cache_clear_reports_the_refusal_and_eviction_its_report() -> None:
    from piceli.artifacts.cluster_build import encode_receipt
    from piceli.infra.builders import JobBuilder
    from piceli.pipeline.prerollout_cluster import Outcome, _pod_refusal

    assert _pod_refusal(EVICTED) == {
        "pod_reason": "Evicted",
        "pod_message": "The node had condition: [DiskPressure].",
    }
    assert _pod_refusal({"status": {"phase": "Running"}}) == {}

    class Cluster:
        receipt_text: str | None = None

        def __init__(self, outcome: Outcome, receipt: dict[str, Any] | None) -> None:
            self.outcome = outcome
            if receipt is not None:
                self.receipt_text = encode_receipt(receipt).split(" ", 1)[1]

        def run_job(self, job: Any) -> Outcome:
            return self.outcome

    builder = JobBuilder.__new__(JobBuilder)
    builder.settings = _settings()
    builder.cache_report = None
    refused = Outcome(
        "failed", reason="BackoffLimitExceeded", extra=_pod_refusal(EVICTED)
    )
    builder.cluster = Cluster(refused, None)
    found = builder.clear_cache([])
    assert found["state"] == "failed" and found["reason"] == "Evicted"
    assert "DiskPressure" in found["message"]
    report = {"used_bytes": 3 * GiB, "max_bytes": 16 * GiB, "evicted": ["blobs"],
              "freed_bytes": GiB, "over_budget": False, "fs_low": False,
              "fs_free_bytes": 40 * GiB, "at": 2_000_000_000.0}  # fmt: skip
    builder.cluster = Cluster(Outcome("passed"), {"cache": report})
    found = builder.evict_cache()
    assert found["state"] == "passed" and found["report"]["evicted"] == ["blobs"]
    assert builder.cache_node == "builder-1"
    assert builder.cache_key == "piceli-system/piceli-component-cache"
    assert builder.cache_report["used_bytes"] == 3 * GiB


# ------------------------------------------------------- the controller


def test_the_controller_evicts_held_caches_once_per_window_and_degrades(
    world: dict[str, Any],  # noqa: F811
) -> None:
    from piceli.k8s.cli.gitops import _health

    controller, ports, clock = world["controller"], world["ports"], world["clock"]
    pressure = {"nodes": dict(PRESSURE)}
    ports.storage = lambda: {"nodes": dict(pressure["nodes"]), "dev_cache": {}}
    calls: list[str] = []

    def evict_build() -> dict[str, Any]:
        calls.append("build")
        return {
            "state": "passed",
            "job": "piceli-build-cache-clear-1",
            "report": {"used_bytes": 3 * GiB, "max_bytes": 16 * GiB,
                       "fs_free_bytes": 9 * GiB, "evicted": ["target/amd64-4096"],
                       "freed_bytes": 5 * GiB, "at": clock["now"]},
        }  # fmt: skip

    def evict_dev(dev: DevBuilds) -> dict[str, Any]:
        calls.append(f"dev {dev.node}")
        raise DevError(
            "dev-cache-clear-failed",
            "the cache-clear pod was rejected by the node: Evicted: DiskPressure",
        )

    ports.builder.evict_cache = evict_build
    ports.builder.cache_node = "builder-1"
    ports.builder.cache_key = "piceli-system/piceli-component-cache"
    ports.evict_dev_cache = evict_dev
    controller._dev_builds = lambda: DevBuilds(node="builder-1", image=IMAGE)
    status = controller.poll_once()
    storage = status["controller"]["storage"]
    assert sorted(calls) == ["build", "dev builder-1"]
    build = storage["caches"]["piceli-system/piceli-component-cache"]
    assert build["used_bytes"] == 3 * GiB and build["max_bytes"] == 16 * GiB
    assert build["fs_free_bytes"] == 9 * GiB
    relief = storage["relief"]["caches"]
    assert relief["piceli-system/piceli-component-cache"]["evicted"] == [
        "target/amd64-4096"
    ]
    dev = relief["piceli-dev/piceli-dev-cache"]
    assert dev["state"] == "failed" and dev["reason"] == "dev-cache-clear-failed"
    assert "DiskPressure" in dev["message"]
    assert "builder-1 is under disk pressure" in storage["problem"]
    assert status["controller"]["state"] == "degraded"
    assert "piceli dev cache clear" in status["controller"]["storage_problem"]
    assert _health(status, True, clock["now"]) == "degraded"
    record = controller.state["storage"]
    done = float(record["relief"]["epoch"])
    controller._relieve(record, done + 30)
    assert len(calls) == 2  # at most once per window
    controller._relieve(record, done + 61)
    assert len(calls) == 4
    pressure["nodes"] = {}  # space again
    clock["now"] = done + 1000
    status = controller.poll_once()
    assert len(calls) == 4
    assert "storage_problem" not in status["controller"]
    assert "problem" not in status["controller"]["storage"]


def test_a_cache_on_another_node_is_left_alone(
    world: dict[str, Any],  # noqa: F811
) -> None:
    controller, ports = world["controller"], world["ports"]
    ports.storage = lambda: {"nodes": dict(PRESSURE), "dev_cache": {}}
    calls: list[str] = []
    ports.builder.evict_cache = lambda: calls.append("build") or {"state": "passed"}
    ports.builder.cache_node = "builder-2"
    status = controller.poll_once()
    assert calls == [] and "storage_problem" not in status["controller"]
