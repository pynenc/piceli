"""A restore point whose process died mid-copy, and the attempt after it.

Reproduces IH's k-lab deploy of 2026-10-07 small: a 50 MB claim, the
process taking the restore point killed while the archive streams (it
leaves a ``.partial`` file, a ``running`` record and the writers at zero
replicas), then a retry.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from piceli.restore import RestorePointError, RestorePoints
from piceli.restore.plan import plan
from piceli.restore.runner import take
from piceli.restore.store import load_record, take_lock
from tests.unit.restore.conftest import NEW, OLD, claims, stateful_app, workloads
from tests.unit.restore.fake_cluster import FakeCluster

pytestmark = pytest.mark.skipif(
    shutil.which("sha256sum") is None, reason="needs sha256sum"
)

MB = 1024 * 1024
CHUNK = 256 * 1024


class StreamingCluster(FakeCluster):
    """Streams the archive in chunks; ``die_after`` bytes ends the process."""

    die_after: int | None = None

    def exec(  # type: ignore[override]
        self,
        pod: str,
        command: list[str],
        *,
        stdout: Callable[[bytes], None] | None = None,
        **kwargs: Any,
    ) -> int:
        if stdout is None or "tar -czf" not in command[-1]:
            return super().exec(pod, command, stdout=stdout, **kwargs)
        buffer = bytearray()
        code = super().exec(pod, command, stdout=buffer.extend, **kwargs)
        for start in range(0, len(buffer), CHUNK):
            if self.die_after is not None and start >= self.die_after:
                os._exit(137)  # SIGKILL: no cleanup runs
            stdout(bytes(buffer[start : start + CHUNK]))
        return code


def _cluster(root: Path) -> tuple[StreamingCluster, Any]:
    live = workloads(stateful_app(OLD))
    replicas = root / "replicas.json"
    if replicas.exists():  # what the killed process left in the "cluster"
        for key, count in json.loads(replicas.read_text()).items():
            live_workload = next(
                w for w in live if f"{w['kind']}/{w['metadata']['name']}" == key
            )
            live_workload["spec"]["replicas"] = count
    cluster = StreamingCluster(root, live)
    cluster.claim_names = ["data-db-0", "data-db-1", "cache-state"]
    for index in (0, 1):
        blob = cluster.claim_dir(f"data-db-{index}") / "blob"
        if not blob.exists():
            blob.write_bytes(os.urandom(25 * MB))  # incompressible: 50 MB in all
    scale = cluster.scale

    def persisted(kind: str, name: str, count: int) -> None:
        scale(kind, name, count)
        replicas.write_text(
            json.dumps(
                {key: w["spec"].get("replicas") for key, w in cluster.live.items()}
            )
        )

    cluster.scale = persisted  # type: ignore[method-assign]
    return cluster, plan(workloads(stateful_app(NEW)), live, claims())


def _take(root: Path, die_after: int | None = None, **kwargs: Any) -> dict[str, Any]:
    cluster, result = _cluster(root)
    cluster.die_after = die_after
    return take(
        cluster,  # type: ignore[arg-type]
        result,
        RestorePoints(),
        root / "points",
        context={"namespace": "shop"},
        **kwargs,
    )


def _killed(root: Path) -> Path:
    """Run a take in a child process that dies 10 MB into the first archive."""
    child = multiprocessing.get_context("spawn").Process(
        target=_take, args=(root, 10 * MB)
    )
    child.start()
    child.join(60)
    assert child.exitcode == 137
    (point,) = list((root / "points").glob("rp-*"))
    return point


def test_a_killed_copy_leaves_a_partial_a_running_record_and_stopped_writers(
    tmp_path: Path,
) -> None:
    point = _killed(tmp_path)
    assert sorted(path.name for path in point.iterdir()) == [
        ".00-data-db-0.tar.gz.partial",
        "record.json",
    ]
    assert json.loads((point / "record.json").read_text())["state"] == "running"
    assert json.loads((tmp_path / "replicas.json").read_text())["StatefulSet/db"] == 0


def test_the_retry_takes_a_clean_point_and_closes_the_interrupted_one(
    tmp_path: Path,
) -> None:
    killed = _killed(tmp_path)
    record = _take(tmp_path)
    assert record["state"] == "verified" and record["id"] != killed.name
    # The interrupted point: marked, its partial archive removed.
    _directory, old = load_record(tmp_path / "points", killed.name)
    assert old["state"] == "interrupted"
    assert [path.name for path in killed.iterdir()] == ["record.json"]
    # The new point holds only its own files, each verified.
    fresh = tmp_path / "points" / record["id"]
    assert sorted(path.name for path in fresh.iterdir()) == [
        "00-data-db-0.tar.gz",
        "01-data-db-1.tar.gz",
        "record.json",
    ]
    assert [item["replicas"] for item in record["writers"]] == [2]
    assert json.loads((tmp_path / "replicas.json").read_text())["StatefulSet/db"] == 2


def test_a_retry_into_the_same_point_is_refused_before_any_writer_stops(
    tmp_path: Path,
) -> None:
    killed = _killed(tmp_path)
    cluster, result = _cluster(tmp_path)
    with pytest.raises(RestorePointError) as caught:
        take(
            cluster,  # type: ignore[arg-type]
            result,
            RestorePoints(),
            tmp_path / "points",
            context={"namespace": "shop"},
            point=killed.name,
        )
    assert caught.value.code == "restore-point-exists"
    assert cluster.calls == []


def test_two_takes_into_one_directory_never_run_at_once(tmp_path: Path) -> None:
    cluster, result = _cluster(tmp_path)
    root = tmp_path / "points"
    with take_lock(root):
        with pytest.raises(RestorePointError) as caught:
            take(
                cluster,  # type: ignore[arg-type]
                result,
                RestorePoints(),
                root,
                context={"namespace": "shop"},
            )
    assert caught.value.code == "restore-point-busy"
    assert cluster.calls == []


def test_a_checksum_mismatch_says_whether_the_file_size_changed(
    tmp_path: Path,
) -> None:
    from piceli.restore.store import verify_claim

    record = _take(tmp_path)
    directory = tmp_path / "points" / record["id"]
    entry = dict(record["claims"][0])
    with (directory / entry["archive"]).open("ab") as stream:
        stream.write(b"another writer")
    with pytest.raises(RestorePointError) as caught:
        verify_claim(directory, entry)
    assert caught.value.code == "restore-point-checksum-mismatch"
    assert f"{entry['bytes']} were recorded: something else wrote it" in str(
        caught.value
    )
    assert caught.value.details["file_bytes"] == entry["bytes"] + 14
