"""0.90.0: the development cache's budget follows its disk's free space, and
``piceli dev run`` says when the installed config differs from the declared one."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from piceli.dev import pod
from piceli.dev.model import DevBuilds, config_drift
from piceli.infra.cluster import ClusterError

IMAGE = "ghcr.io/example/builder@sha256:" + "a" * 64
GIB = 2**30


def _dev(**extra: object) -> DevBuilds:
    return DevBuilds(node="node-a", image=IMAGE, **extra)  # type: ignore[arg-type]


def _units(sizes: list[int]) -> list[tuple[float, int, str, Callable[[], bool]]]:
    return [
        (float(index), size, f"app/{index}", lambda: True)
        for index, size in enumerate(sizes)
    ]


def test_a_small_free_disk_lowers_the_budget_below_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pod, "filesystem", lambda _p: (20 * GIB, 200 * GIB))
    report = pod.shrink(tmp_path, _units([10 * GIB]), 0, 0, 88 * GIB, 0.0, 0.5)
    assert report["cap_bytes"] == 88 * GIB
    assert report["max_bytes"] == 20 * GIB  # 10 held + half of 20 free
    assert report["limited_by"] == "disk" and report["evicted"] == []
    line = pod.budget_line(report)
    assert line.startswith("[piceli-dev] cache budget: 20.0 GiB (")
    assert "50% of its disk's 20.0 GiB free" in line and "88.0 GiB" in line


def test_a_roomy_disk_keeps_the_configured_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pod, "filesystem", lambda _p: (500 * GIB, 900 * GIB))
    report = pod.shrink(tmp_path, _units([10 * GIB]), 0, 0, 88 * GIB, 0.0, 0.5)
    assert report["max_bytes"] == 88 * GIB and report["limited_by"] == "cap"
    assert "the configured budget" in pod.budget_line(report)


def test_the_disk_budget_evicts_least_recently_used_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pod, "filesystem", lambda _p: (2 * GIB, 200 * GIB))
    report = pod.shrink(
        tmp_path,
        _units([4 * GIB, 4 * GIB]),
        0,
        0,
        88 * GIB,
        0.0,
        0.5,
    )
    assert report["max_bytes"] == 9 * GIB  # 8 held + half of 2 free
    assert report["evicted"] == [] and report["over_budget"] is False


def test_without_a_share_the_budget_is_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pod, "filesystem", lambda _p: (1 * GIB, 200 * GIB))
    report = pod.shrink(tmp_path, _units([10 * GIB]), 0, 0, 88 * GIB, 0.0)
    assert report["max_bytes"] == 88 * GIB and report["limited_by"] == "cap"


def test_the_share_is_declared_and_checked() -> None:
    assert _dev().cache_free_share == 0.5
    assert "cache_free_share" not in _dev().describe()  # the default: unchanged
    assert _dev(cache_free_share=0).describe()["cache_free_share"] == 0
    read = DevBuilds.from_dict(_dev(cache_free_share=0.3).describe())
    assert read.cache_free_share == 0.3
    for bad in (0.05, 1.5, True, "half"):
        with pytest.raises(ClusterError):
            _dev(cache_free_share=bad)


def test_a_run_spec_carries_the_share() -> None:
    import json

    from piceli.dev import jobs

    dev = _dev(cache_free_share=0.3)
    job = jobs.run_job(
        dev,
        dev.profile(None),
        run="20261127t100000-ab12",
        spec={"command": ["true"], "archive_sha256": "0" * 64},
        requester="agent-7",
        priority="agent",
    )
    (container,) = job["spec"]["template"]["spec"]["containers"]
    env = {item["name"]: item["value"] for item in container["env"]}
    assert json.loads(env["PICELI_DEV_SPEC"])["cache_free_share"] == 0.3


def test_the_drift_names_the_fields_that_differ() -> None:
    installed = _dev(cache_size="80Gi")
    declared = _dev(cache_size="110Gi")
    assert config_drift(installed, declared) == ["cache_size: 80Gi -> 110Gi"]
    assert config_drift(declared, declared) == []
    # A config written before 0.90.0 has no share: it reads as the default.
    old = DevBuilds.from_dict(
        {k: v for k, v in declared.describe().items() if k != "cache_free_share"}
    )
    assert config_drift(old, declared) == []
