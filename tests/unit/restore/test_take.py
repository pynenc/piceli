"""Taking a restore point: quiesce, wait for writer pods, archive, verify."""

from __future__ import annotations

import gzip
import io
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

import pytest

from piceli.restore import RestorePointError, RestorePoints
from piceli.restore.cluster import (
    RestoreCluster,
    helper_job,
    stream_exec,
)
from piceli.restore.plan import plan
from piceli.restore.runner import take
from piceli.restore.store import inspect_archive, load_record, verify_claim
from tests.unit.restore.conftest import NEW, OLD, claims, stateful_app, workloads
from tests.unit.restore.fake_cluster import FakeCluster

needs_tools = pytest.mark.skipif(
    shutil.which("sha256sum") is None, reason="needs sha256sum"
)


def _writer_pod(name: str, claim: str, *, deleting: bool = False) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name}
    if deleting:
        metadata["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    return {
        "metadata": metadata,
        "status": {"phase": "Running"},
        "spec": {
            "containers": [
                {"name": "db", "volumeMounts": [{"name": "data", "mountPath": "/d"}]}
            ],
            "volumes": [
                {"name": "data", "persistentVolumeClaim": {"claimName": claim}}
            ],
        },
    }


class ScriptedCluster(RestoreCluster):
    """RestoreCluster whose reads come from a script (one entry per poll)."""

    def __init__(self, pods: list[list[dict[str, Any]]], replicas: list[int]) -> None:
        super().__init__(client=None, namespace="shop", poll_seconds=0)
        self.script, self.replicas, self.polls = pods, replicas, 0

    def pods(self, selector: str | None = None) -> list[dict[str, Any]]:
        index = min(self.polls, len(self.script) - 1)
        self.polls += 1
        return self.script[index]

    def workload(self, kind: str, name: str) -> dict[str, Any] | None:
        index = min(self.polls - 1, len(self.replicas) - 1)
        return {"status": {"replicas": self.replicas[index]}}


WRITERS = [{"kind": "StatefulSet", "name": "db"}]


def test_the_copy_waits_while_a_terminating_writer_pod_still_exists() -> None:
    # Readiness already reports 0 replicas, but db-1 is still terminating.
    terminating = [_writer_pod("db-1", "data-db-1", deleting=True)]
    cluster = ScriptedCluster([terminating, terminating, []], replicas=[0, 0, 0])
    cluster.wait_stopped(WRITERS, ["data-db-0", "data-db-1"], seconds=30)
    assert cluster.polls == 3


def test_the_wait_also_needs_the_controller_to_report_no_replicas() -> None:
    cluster = ScriptedCluster([[], [], []], replicas=[1, 1, 0])
    cluster.wait_stopped(WRITERS, ["data-db-0"], seconds=30)
    assert cluster.polls == 3


def test_writer_pods_that_never_go_away_time_out() -> None:
    cluster = ScriptedCluster([[_writer_pod("db-0", "data-db-0")]], replicas=[0])
    with pytest.raises(RestorePointError) as caught:
        cluster.wait_stopped(WRITERS, ["data-db-0"], seconds=0)
    assert caught.value.code == "restore-point-writers-remain"
    assert "db-0" in str(caught.value)


def _setup(tmp_path: Path) -> tuple[FakeCluster, Any]:
    live = workloads(stateful_app(OLD))
    cluster = FakeCluster(tmp_path, live)
    cluster.claim_names = ["data-db-0", "data-db-1", "cache-state"]
    for index in (0, 1):
        directory = cluster.claim_dir(f"data-db-{index}")
        (directory / "marker").write_text(f"replica {index}\n")
        (directory / "sub").mkdir()
        (directory / "sub" / "log").write_bytes(b"\x00\x01" * 100)
    result = plan(workloads(stateful_app(NEW)), live, claims())
    return cluster, result


@needs_tools
def test_take_archives_each_claim_verifies_and_leaves_writers_for_the_release(
    tmp_path: Path,
) -> None:
    cluster, result = _setup(tmp_path)
    root = tmp_path / "points"
    record = take(
        cluster,  # type: ignore[arg-type]
        result,
        RestorePoints(),
        root,
        context={"app": "shop", "namespace": "shop"},
        keep_stopped={"StatefulSet/db"},
    )
    assert record["state"] == "verified"
    assert [item["claim"] for item in record["claims"]] == ["data-db-0", "data-db-1"]
    assert all(item["verified"] and item["files"] == 2 for item in record["claims"])
    assert record["writers"] == [
        {
            "workload": "StatefulSet/db",
            "kind": "StatefulSet",
            "name": "db",
            "replicas": 2,
        }
    ]
    assert record["left_stopped"] == ["StatefulSet/db"]
    # Order: stop, wait until gone, then one read-only helper per claim.
    assert cluster.calls[:3] == [
        "scale StatefulSet/db 0",
        "wait data-db-0,data-db-1",
        f"job piceli-backup-{record['id'][-6:]}-00 ro=True",
    ]
    assert "scale StatefulSet/db 2" not in cluster.calls
    directory, stored = load_record(root, record["id"])
    assert stored == record
    for entry in record["claims"]:
        path = directory / entry["archive"]
        assert path.stat().st_mode & 0o777 == 0o600
        assert (
            verify_claim(directory, entry)["content_sha256"] == entry["content_sha256"]
        )
    assert directory.stat().st_mode & 0o777 == 0o700
    # The helper runs the writer's own image unless one is declared.
    job = next(iter(cluster.jobs.values()))
    assert job["spec"]["template"]["spec"]["containers"][0]["image"] == OLD


@needs_tools
def test_writers_start_again_when_the_release_will_not(tmp_path: Path) -> None:
    cluster, result = _setup(tmp_path)
    record = take(
        cluster,  # type: ignore[arg-type]
        result,
        RestorePoints(),
        tmp_path / "points",
        context={"namespace": "shop"},
    )
    assert cluster.calls[-1] == "scale StatefulSet/db 2"
    assert record["started"] == ["StatefulSet/db"]


@needs_tools
def test_a_failed_copy_starts_writers_again_and_leaves_no_partial_file(
    tmp_path: Path,
) -> None:
    cluster, result = _setup(tmp_path)
    cluster.fail["tar -czf"] = 2
    root = tmp_path / "points"
    with pytest.raises(RestorePointError) as caught:
        take(
            cluster,  # type: ignore[arg-type]
            result,
            RestorePoints(),
            root,
            context={"namespace": "shop"},
            keep_stopped={"StatefulSet/db"},
        )
    assert caught.value.code == "restore-point-copy-failed"
    assert cluster.calls[-1] == "scale StatefulSet/db 2"
    (point,) = list(root.glob("rp-*"))
    assert [path.name for path in point.iterdir()] == ["record.json"]
    assert json.loads((point / "record.json").read_text())["state"] == "failed"


@needs_tools
def test_quiesce_hooks_run_before_any_writer_stops(tmp_path: Path) -> None:
    cluster, result = _setup(tmp_path)
    result.writers[0]["quiesce"] = [{"type": "exec", "command": ["sync"]}]
    take(
        cluster,  # type: ignore[arg-type]
        result,
        RestorePoints(),
        tmp_path / "points",
        context={"namespace": "shop"},
    )
    assert cluster.calls[:2] == ["hook StatefulSet/db exec", "scale StatefulSet/db 0"]


def _tar(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_verification_refuses_changed_or_unsafe_archives(tmp_path: Path) -> None:
    path = tmp_path / "00-x.tar.gz"
    path.write_bytes(_tar([("./a", b"1")]))
    listing = inspect_archive(path)
    import hashlib

    entry = {
        "claim": "x",
        "archive": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "content_sha256": listing["content_sha256"],
    }
    verify_claim(tmp_path, entry)
    path.write_bytes(_tar([("./a", b"2")]))
    with pytest.raises(RestorePointError) as caught:
        verify_claim(tmp_path, entry)
    assert caught.value.code == "restore-point-checksum-mismatch"
    entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(RestorePointError) as caught:
        verify_claim(tmp_path, entry)
    assert caught.value.code == "restore-point-checksum-mismatch"
    path.write_bytes(_tar([("../escape", b"1")]))
    with pytest.raises(RestorePointError) as caught:
        inspect_archive(path)
    assert caught.value.code == "restore-point-archive-invalid"
    path.write_bytes(gzip.compress(b"not a tar" * 100))
    with pytest.raises(RestorePointError) as caught:
        inspect_archive(path)
    assert caught.value.code == "restore-point-archive-invalid"


class FakeSocket:
    def __init__(self, frames: list[bytes]) -> None:
        self.frames, self.sent, self.closed = list(frames), [], False

    def send_binary(self, data: bytes) -> None:
        self.sent.append(data)

    def settimeout(self, _seconds: float) -> None:
        pass

    def recv_data(self) -> tuple[int, bytes]:
        import websocket

        if not self.frames:
            return websocket.ABNF.OPCODE_CLOSE, b""
        return websocket.ABNF.OPCODE_BINARY, self.frames.pop(0)

    def close(self) -> None:
        self.closed = True


def test_stream_exec_passes_stdout_sends_stdin_and_reads_the_exit_code() -> None:
    failure = json.dumps(
        {
            "status": "Failure",
            "details": {"causes": [{"reason": "ExitCode", "message": "3"}]},
        }
    ).encode()
    socket_ = FakeSocket(
        [b"\x01abc", b"\x02secret-ish stderr", b"\x01def", b"\x03" + failure]
    )
    out = bytearray()
    code = stream_exec(socket_, stdout=out.extend, stdin=iter([b"xy"]), timeout=5)
    assert (code, bytes(out), socket_.sent, socket_.closed) == (
        3,
        b"abcdef",
        [b"\x00xy"],
        True,
    )
    success = FakeSocket([b'\x03{"status": "Success"}'])
    assert stream_exec(success, stdout=out.extend, timeout=5) == 0
    with pytest.raises(RestorePointError) as caught:
        stream_exec(FakeSocket([]), stdout=out.extend, timeout=5)
    assert caught.value.code == "restore-point-copy-failed"


def test_the_helper_job_mounts_the_claim_and_ends_by_itself() -> None:
    job = helper_job(
        "piceli-backup-abcdef-00",
        point="rp-20260101t000000z-abcdef",
        claim="data-db-0",
        image=OLD,
        read_only=True,
        seconds=2400,
        run_as_user=0,
        template={"spec": {"nodeSelector": {"disk": "ssd"}, "containers": []}},
    )
    pod = job["spec"]["template"]["spec"]
    assert job["spec"]["activeDeadlineSeconds"] == 2400
    assert job["spec"]["backoffLimit"] == 0
    assert pod["automountServiceAccountToken"] is False
    assert pod["nodeSelector"] == {"disk": "ssd"}
    assert pod["volumes"][0]["persistentVolumeClaim"] == {
        "claimName": "data-db-0",
        "readOnly": True,
    }
    container = pod["containers"][0]
    assert container["volumeMounts"][0]["readOnly"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["securityContext"]["allowPrivilegeEscalation"] is False


def _interrupted_point(root: Path, replicas: int = 2) -> Path:
    """What a controller killed mid-copy leaves: a ``running`` record, a partial."""
    from piceli.restore.store import write_record

    directory = root / "rp-20260101t000000z-abcdef"
    write_record(
        directory,
        {
            "schema": "piceli.restore-point.v1",
            "id": directory.name,
            "state": "running",
            "namespace": "shop",
            "writers": [
                {
                    "workload": "StatefulSet/db",
                    "kind": "StatefulSet",
                    "name": "db",
                    "replicas": replicas,
                }
            ],
            "claims": [],
        },
    )
    (directory / ".00-data-db-0.tar.gz.partial").write_bytes(b"half an archive")
    return directory


@needs_tools
def test_a_failed_copy_after_an_interrupted_one_starts_writers_at_their_replicas(
    tmp_path: Path,
) -> None:
    """The writers an interrupted attempt stopped are at 0 when the retry
    starts; a failing retry scales them back to the count before the first."""
    cluster, result = _setup(tmp_path)
    cluster.live["StatefulSet/db"]["spec"]["replicas"] = 0
    root = tmp_path / "points"
    _interrupted_point(root)
    cluster.fail["tar -czf"] = 2
    with pytest.raises(RestorePointError) as caught:
        take(
            cluster,  # type: ignore[arg-type]
            result,
            RestorePoints(),
            root,
            context={"namespace": "shop"},
            keep_stopped={"StatefulSet/db"},
        )
    assert caught.value.code == "restore-point-copy-failed"
    assert cluster.live["StatefulSet/db"]["spec"]["replicas"] == 2
    assert caught.value.details["writers_started"] == ["StatefulSet/db"]


@needs_tools
def test_a_retry_records_the_replicas_before_the_interrupted_attempt(
    tmp_path: Path,
) -> None:
    cluster, result = _setup(tmp_path)
    cluster.live["StatefulSet/db"]["spec"]["replicas"] = 0
    root = tmp_path / "points"
    _interrupted_point(root, replicas=3)
    record = take(
        cluster,  # type: ignore[arg-type]
        result,
        RestorePoints(),
        root,
        context={"namespace": "shop"},
    )
    assert record["writers"][0]["replicas"] == 3
    assert cluster.live["StatefulSet/db"]["spec"]["replicas"] == 3


@needs_tools
def test_the_replicas_are_recorded_before_any_writer_is_scaled(tmp_path: Path) -> None:
    """A controller killed right after a scale still knows the count."""
    cluster, result = _setup(tmp_path)
    root = tmp_path / "points"
    seen: list[Any] = []
    scale = cluster.scale

    def recording_scale(kind: str, name: str, replicas: int) -> None:
        if replicas == 0:
            (point,) = list(root.glob("rp-*"))
            seen.append(json.loads((point / "record.json").read_text())["writers"])
        scale(kind, name, replicas)

    cluster.scale = recording_scale  # type: ignore[method-assign]
    take(
        cluster,  # type: ignore[arg-type]
        result,
        RestorePoints(),
        root,
        context={"namespace": "shop"},
    )
    assert seen and seen[0][0]["replicas"] == 2
