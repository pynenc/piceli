"""The development-build wrapper that runs inside the run's pod (stdlib only).

It runs here on plain directories: ``work`` is the pod's scratch, ``cache``
the shared cache claim.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from piceli.dev import pod

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")


def _archive(files: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in sorted(files.items()):
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 1_000_000
            if name.endswith(".sh"):
                info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _spec(data: bytes, command: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "run": "r1",
        "command": command,
        "cwd": "app",
        "archive_sha256": hashlib.sha256(data).hexdigest(),
        "lineage_key": "app-rust",
        "lineage_hint": "wp-a",
        "env": {},
        "timeout_seconds": 60,
        "upload_timeout_seconds": 5,
        "collect_timeout_seconds": 5,
        "poll_seconds": 0.01,
        "max_lineages": 2,
        "cache_max_bytes": 10**9,
        "log_tail_lines": 5,
        "prefetch": None,
        "tools": [],
        "artifacts": [],
        **extra,
    }


def _run(
    tmp_path: Path, data: bytes, spec: dict[str, Any], name: str = "w"
) -> tuple[dict[str, Any], list[str]]:
    work = tmp_path / name
    (work / "upload").mkdir(parents=True)
    (work / "upload" / "tree.tar.gz").write_bytes(data)
    (work / "upload" / "ready").touch()
    lines: list[str] = []
    result = pod.main(spec, work, tmp_path / "cache", emit=lines.append)
    assert lines[-1].startswith(pod.RESULT_MARKER)
    assert json.loads(lines[-1][len(pod.RESULT_MARKER) :]) == result
    return result, lines


def test_a_run_syncs_the_tree_into_a_lineage_and_runs_the_command(
    tmp_path: Path,
) -> None:
    data = _archive({"app/a.txt": "one\n", "app/b.txt": "two\n"})
    command = ["sh", "-c", 'cat a.txt b.txt; echo "target=$CARGO_TARGET_DIR"']
    result, lines = _run(tmp_path, data, _spec(data, command))
    assert result["state"] == "passed" and result["exit_code"] == 0
    assert "one" in lines and "two" in lines
    lineage = Path(result["cache"]["lineage_dir"])
    assert lineage.parent == tmp_path / "cache" / "lineages" / "app-rust"
    assert f"target={lineage / 'target'}" in lines
    assert result["cache"]["warm"] is False
    assert set(result["durations"]) >= {"sync", "command", "total"}
    # The scratch is the pod's emptyDir: the extracted tree is gone after.
    assert not (tmp_path / "w" / "tree").exists()


def test_a_second_run_keeps_the_mtime_of_unchanged_files(tmp_path: Path) -> None:
    first = _archive({"app/a.txt": "one\n", "app/b.txt": "two\n", "app/gone.txt": "x"})
    result, _ = _run(tmp_path, first, _spec(first, ["true"]), "w1")
    tree = Path(result["cache"]["lineage_dir"]) / "tree" / "app"
    before = (tree / "a.txt").stat().st_mtime_ns
    time.sleep(0.02)
    second = _archive({"app/a.txt": "one\n", "app/b.txt": "TWO\n"})
    again, _ = _run(tmp_path, second, _spec(second, ["true"]), "w2")
    assert again["cache"]["lineage_dir"] == result["cache"]["lineage_dir"]
    assert again["cache"]["warm"] is True
    assert (tree / "a.txt").stat().st_mtime_ns == before  # cargo sees it fresh
    assert (tree / "b.txt").read_text() == "TWO\n"
    assert (tree / "b.txt").stat().st_mtime_ns > before
    assert not (tree / "gone.txt").exists()
    assert again["sync"] == {"written": 1, "removed": 1, "kept": 1}


def test_a_failing_command_reports_its_exit_code_and_log_tail(tmp_path: Path) -> None:
    data = _archive({"app/a.txt": "x"})
    command = ["sh", "-c", "for i in 1 2 3 4 5 6 7; do echo line $i; done; exit 3"]
    result, _ = _run(tmp_path, data, _spec(data, command))
    assert result["state"] == "failed" and result["exit_code"] == 3
    assert result["reason"] == "dev-command-failed"
    assert result["log_tail"] == [f"line {i}" for i in range(3, 8)]


def test_cargo_test_output_gives_phases_tests_and_cache_use(tmp_path: Path) -> None:
    script = "\n".join(
        [
            "echo '   Compiling poet v0.1.0 (/x)'",
            "echo '   Compiling model v0.1.0 (/x)'",
            "echo '    Finished `test` profile [unoptimized] target(s) in 4.2s'",
            "echo '     Running unittests src/lib.rs'",
            "echo 'test result: ok. 12 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out'",
            "echo 'test result: FAILED. 3 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out'",
            "exit 101",
        ]
    )
    lock = "".join(f'[[package]]\nname = "p{i}"\n\n' for i in range(10))
    data = _archive({"app/Cargo.lock": lock, "app/t.sh": script})
    result, _ = _run(tmp_path, data, _spec(data, ["sh", "t.sh"]))
    assert result["tests"] == {"passed": 15, "failed": 2, "ignored": 1, "suites": 2}
    assert result["cache"]["crates_compiled"] == 2
    assert result["cache"]["crates_locked"] == 10
    assert result["cache"]["hit_ratio"] == 0.8
    assert {"build", "test"} <= set(result["durations"])
    assert result["exit_code"] == 101


def test_a_command_over_its_time_is_killed(tmp_path: Path) -> None:
    data = _archive({"app/a.txt": "x"})
    spec = _spec(data, ["sh", "-c", "echo start; sleep 30"], timeout_seconds=1)
    began = time.monotonic()
    result, _ = _run(tmp_path, data, spec)
    assert time.monotonic() - began < 10
    assert result["state"] == "timed-out" and result["reason"] == "dev-run-timed-out"


def test_an_archive_that_does_not_match_its_digest_runs_nothing(tmp_path: Path) -> None:
    data = _archive({"app/a.txt": "x"})
    spec = _spec(data, ["sh", "-c", "touch ran"], archive_sha256="0" * 64)
    result, _ = _run(tmp_path, data, spec)
    assert result["state"] == "error" and result["reason"] == "dev-upload-failed"
    assert not list((tmp_path / "cache").rglob("ran"))


def test_no_upload_in_time_ends_the_run(tmp_path: Path) -> None:
    work = tmp_path / "w"
    lines: list[str] = []
    spec = _spec(b"", ["true"], upload_timeout_seconds=0.2)
    result = pod.main(spec, work, tmp_path / "cache", emit=lines.append)
    assert result["reason"] == "dev-upload-failed"


def test_unsafe_archive_members_are_refused(tmp_path: Path) -> None:
    data = _archive({"../escape.txt": "x"})
    result, _ = _run(tmp_path, data, _spec(data, ["true"]))
    assert result["reason"] == "dev-upload-failed"
    assert not (tmp_path / "escape.txt").exists()


def test_a_missing_tool_fails_before_the_command(tmp_path: Path) -> None:
    data = _archive({"app/a.txt": "x"})
    spec = _spec(data, ["sh", "-c", "touch ran"], tools=["sh", "no-such-tool-xyz"])
    result, _ = _run(tmp_path, data, spec)
    assert result["reason"] == "dev-tool-missing"
    assert result["missing_tools"] == ["no-such-tool-xyz"]


def test_a_busy_lineage_is_never_shared(tmp_path: Path) -> None:
    """Two runs at once take two lineages: never two writers on one target."""
    data = _archive({"app/a.txt": "x"})
    seen: list[str] = []

    def slow() -> None:
        result, _ = _run(
            tmp_path,
            data,
            _spec(data, ["sh", "-c", "touch $CARGO_TARGET_DIR/../busy; sleep 1"]),
            "slow",
        )
        seen.append(result["cache"]["lineage_dir"])

    thread = threading.Thread(target=slow)
    thread.start()
    deadline = time.monotonic() + 5
    while not list((tmp_path / "cache").rglob("busy")) and time.monotonic() < deadline:
        time.sleep(0.02)
    other, _ = _run(tmp_path, data, _spec(data, ["true"]), "fast")
    thread.join()
    assert other["cache"]["lineage_dir"] != seen[0]


def test_artifacts_are_collected_for_the_client(tmp_path: Path) -> None:
    data = _archive({"app/a.txt": "x"})
    command = [
        "sh",
        "-c",
        "mkdir -p $CARGO_TARGET_DIR/rep && echo '<x/>' > $CARGO_TARGET_DIR/rep/junit.xml",
    ]
    spec = _spec(data, command, artifacts=["target/rep/*.xml", "a.txt"])
    work = tmp_path / "w"
    (work / "upload").mkdir(parents=True)
    (work / "upload" / "tree.tar.gz").write_bytes(data)
    (work / "upload" / "ready").touch()
    lines: list[str] = []
    done: dict[str, Any] = {}

    def run() -> None:
        done["result"] = pod.main(spec, work, tmp_path / "cache", emit=lines.append)

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 10
    while (
        not any(line.startswith(pod.RESULT_MARKER) for line in lines)
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    archive = work / "out" / "artifacts.tar.gz"
    with tarfile.open(archive) as tar:
        assert sorted(tar.getnames()) == ["a.txt", "target/rep/junit.xml"]
    assert thread.is_alive()  # waits until the client took them
    (work / "out" / "collected").touch()
    thread.join(5)
    assert not thread.is_alive()
    assert done["result"]["artifacts"] == ["a.txt", "target/rep/junit.xml"]


def test_old_free_lineages_are_evicted_over_the_cache_size(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    for key, used in (("old-key", 1), ("new-key", 2)):
        lineage = cache / "lineages" / key / "0"
        (lineage / "target").mkdir(parents=True)
        (lineage / "target" / "blob").write_bytes(b"x" * 4096)
        (lineage / "meta.json").write_text(
            json.dumps({"last_used": used, "bytes": 4096})
        )
    data = _archive({"app/a.txt": "x"})
    result, _ = _run(tmp_path, data, _spec(data, ["true"], cache_max_bytes=6000))
    assert not (cache / "lineages" / "old-key" / "0").exists()
    assert (cache / "lineages" / "new-key" / "0").exists()
    assert result["cache"]["evicted"] == ["old-key/0"]


def test_the_wrapper_imports_nothing_but_the_standard_library() -> None:
    source = Path(pod.__file__).read_text()
    assert "import piceli" not in source and "from piceli" not in source
    assert os.path.getsize(pod.__file__) < 60_000
