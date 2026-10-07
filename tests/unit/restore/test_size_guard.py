"""``RestorePoints(max_claim_bytes=...)``: claims measured before anything stops."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from piceli.restore import RestorePointError, RestorePoints
from piceli.restore.plan import plan
from piceli.restore.runner import take
from tests.unit.restore.conftest import NEW, OLD, claims, stateful_app, workloads
from tests.unit.restore.fake_cluster import FakeCluster

pytestmark = pytest.mark.skipif(
    shutil.which("sha256sum") is None, reason="needs sha256sum"
)


def _setup(tmp_path: Path) -> tuple[FakeCluster, Any]:
    live = workloads(stateful_app(OLD))
    cluster = FakeCluster(tmp_path, live)
    cluster.claim_names = ["data-db-0", "data-db-1", "cache-state"]
    (cluster.claim_dir("data-db-0") / "small").write_bytes(b"x" * 1000)
    (cluster.claim_dir("data-db-1") / "big").write_bytes(b"x" * 3 * 1024 * 1024)
    (cluster.claim_dir("cache-state") / "small").write_bytes(b"x" * 1000)
    # include="all": the cache's claim too, so one writer can be skipped whole.
    result = plan(workloads(stateful_app(NEW)), live, claims(), include="all")
    return cluster, result


def _take(cluster: FakeCluster, result: Any, tmp_path: Path, **settings: Any) -> Any:
    return take(
        cluster,  # type: ignore[arg-type]
        result,
        RestorePoints(include="all", **settings),
        tmp_path / "points",
        context={"namespace": "shop"},
        say=cluster.calls.append,
    )


def test_a_claim_over_the_limit_fails_the_stage_before_anything_stops(
    tmp_path: Path,
) -> None:
    cluster, result = _setup(tmp_path)
    with pytest.raises(RestorePointError) as caught:
        _take(cluster, result, tmp_path, max_claim_bytes=1024 * 1024)
    assert caught.value.code == "restore-point-claim-too-large"
    assert caught.value.details["claims"] == [
        {"claim": "data-db-1", "bytes": caught.value.details["claims"][0]["bytes"]}
    ]
    assert caught.value.details["claims"][0]["bytes"] >= 3 * 1024 * 1024
    assert not [call for call in cluster.calls if call.startswith(("scale", "hook"))]
    assert all("ro=True" in call for call in cluster.calls if call.startswith("job "))


def test_over_limit_skip_leaves_big_claims_out_with_a_warning(tmp_path: Path) -> None:
    cluster, result = _setup(tmp_path)
    record = _take(
        cluster, result, tmp_path, max_claim_bytes=1024 * 1024, over_limit="skip"
    )
    assert record["state"] == "verified"
    assert [item["claim"] for item in record["claims"]] == ["cache-state", "data-db-0"]
    assert [item["claim"] for item in record["skipped"]] == ["data-db-1"]
    assert any(
        "data-db-1" in call and "not in this restore point" in call
        for call in cluster.calls
    )


def test_a_writer_whose_every_claim_is_skipped_is_not_stopped(tmp_path: Path) -> None:
    cluster, result = _setup(tmp_path)
    (cluster.claim_dir("data-db-0") / "big").write_bytes(b"x" * 3 * 1024 * 1024)
    record = _take(
        cluster, result, tmp_path, max_claim_bytes=1024 * 1024, over_limit="skip"
    )
    assert [item["claim"] for item in record["claims"]] == ["cache-state"]
    assert "scale StatefulSet/db 0" not in cluster.calls
    assert "scale Deployment/cache 0" in cluster.calls


def test_every_claim_skipped_takes_no_restore_point(tmp_path: Path) -> None:
    cluster, result = _setup(tmp_path)
    record = _take(cluster, result, tmp_path, max_claim_bytes=10, over_limit="skip")
    assert record["state"] == "skipped" and record["claims"] == []
    assert not [call for call in cluster.calls if call.startswith(("scale", "hook"))]


def test_without_a_limit_nothing_is_measured(tmp_path: Path) -> None:
    cluster, result = _setup(tmp_path)
    _take(cluster, result, tmp_path)
    assert not [call for call in cluster.calls if "piceli-size-" in call]


def test_the_limit_is_part_of_the_settings_only_when_set() -> None:
    assert "max_claim_bytes" not in RestorePoints().describe()
    described = RestorePoints(max_claim_bytes=5, over_limit="skip").describe()
    assert described["max_claim_bytes"] == 5 and described["over_limit"] == "skip"
