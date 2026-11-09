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
    assert again["sync"] == {"written": 1, "removed": 1, "kept": 1, "removed_dirs": 0}


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
            "echo '   Compiling app v0.1.0 (/x)'",
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
    deadline = time.monotonic() + 60
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
    deadline = time.monotonic() + 60
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
    assert result["cache"]["evicted"] == ["old-key/0"]
    # 0.38.1: still over that tiny budget after the run, the run's end trims
    # the least recently used lineage next.
    assert result["cache"]["trim"]["evicted"][0] == "new-key/0"


def test_the_wrapper_imports_nothing_but_the_standard_library() -> None:
    source = Path(pod.__file__).read_text()
    # 0.22: one exception, the shared toolchain module the wrapper bundles.
    bundled = (
        "try:  # in a pod: the wrapper bundles it under this name (jobs.wrapper_source)\n"
        "    import piceli_dev_toolchains as toolchains  # type: ignore[import-not-found]\n"
        "except ImportError:\n"
        "    from piceli.dev import toolchains\n"
    )
    assert source.count(bundled) == 1
    source = source.replace(bundled, "")
    assert "import piceli" not in source and "from piceli" not in source
    shared = Path(pod.__file__).with_name("toolchains.py").read_text()
    assert "import piceli" not in shared and "from piceli" not in shared
    # Shipped as one compressed `python3 -c` argument (Linux caps one
    # argument at 128 KiB); the source itself may grow.
    from piceli.dev.jobs import wrapper_source

    assert len(wrapper_source()) < 60_000


def test_no_code_runs_while_the_api_server_is_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CNI programs a new pod's NetworkPolicy a moment after it starts, or
    never: the command waits for isolation and runs nothing without it."""
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", str(listener.getsockname()[1]))
    data = _archive({"app/a.txt": "x"})
    spec = _spec(
        data, ["sh", "-c", "touch $CARGO_TARGET_DIR/ran"],
        isolation_check=True, isolation_timeout_seconds=0.5,
    )  # fmt: skip
    try:
        result, _ = _run(tmp_path, data, spec)
    finally:
        listener.close()
    assert result["reason"] == "dev-isolation-not-enforced"
    assert not list((tmp_path / "cache").rglob("ran"))


def test_the_command_runs_once_the_api_server_is_cut_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", str(listener.getsockname()[1]))
    threading.Timer(0.4, listener.close).start()  # the policy takes effect
    data = _archive({"app/a.txt": "x"})
    spec = _spec(data, ["true"], isolation_check=True, isolation_timeout_seconds=10)
    result, lines = _run(tmp_path, data, spec)
    assert result["state"] == "passed"
    assert any("waiting for the network policy" in line for line in lines)


def test_a_corrupted_cached_crate_is_cleared_and_the_command_runs_once_more(
    tmp_path: Path,
) -> None:
    """0.19: the first run blames a truncated artifact of ``geometry``
    in the lineage's target; it is removed and the second run passes."""
    script = "\n".join(
        [
            'obj="$CARGO_TARGET_DIR/debug/incremental/geometry-3k2j1/s-1/x.o"',
            'if [ -f "$obj" ]; then',
            "  echo '   Compiling geometry v0.4.0 (/x)'",
            "  echo 'error: linking with `cc` failed: exit status: 1'",
            "  echo '  = note: rust-lld: error: undefined hidden symbol: geometry::f'",
            '  echo "          >>> referenced by $obj"',
            "  echo 'error: could not compile `geometry` (bin \"geometry\") due to 1 previous error'",
            "  exit 101",
            "fi",
            "echo '    Finished `dev` profile [unoptimized] target(s) in 1.0s'",
        ]
    )
    data = _archive({"app/build.sh": script})
    first, _ = _run(tmp_path, data, _spec(data, ["true"]), "seed")
    poisoned = Path(first["cache"]["lineage_dir"]) / "target"
    (poisoned / "debug/incremental/geometry-3k2j1/s-1").mkdir(parents=True)
    (poisoned / "debug/incremental/geometry-3k2j1/s-1/x.o").write_bytes(b"\0" * 7)
    (poisoned / "debug/deps").mkdir(parents=True)
    (poisoned / "debug/deps/libapp-1.rlib").write_text("kept")
    result, lines = _run(tmp_path, data, _spec(data, ["sh", "build.sh"]), "repair")
    assert result["state"] == "passed", result
    repaired = result["cache"]["repaired"]
    assert repaired["kind"] == "link" and repaired["crates"] == ["geometry"]
    assert repaired["entries"] == 1
    assert repaired["first_error"][0].startswith("error: linking with")
    assert any("corrupted cache (link): cleared geometry" in line for line in lines)
    assert (poisoned / "debug/deps/libapp-1.rlib").exists()


def test_an_ordinary_failure_is_not_retried_and_keeps_its_first_error(
    tmp_path: Path,
) -> None:
    script = "\n".join(
        [
            "echo 'error[E0308]: mismatched types'",
            "echo ' --> src/lib.rs:3:5'",
            *[f"echo 'note {i}'" for i in range(10)],
            "exit 101",
        ]
    )
    data = _archive({"app/build.sh": script})
    result, lines = _run(tmp_path, data, _spec(data, ["sh", "build.sh"]))
    assert result["state"] == "failed" and "repaired" not in result["cache"]
    assert result["first_error"][:2] == [
        "error[E0308]: mismatched types",
        " --> src/lib.rs:3:5",
    ]
    assert "error[E0308]: mismatched types" not in result["log_tail"]  # 5 lines
    ran = [line for line in lines if not line.startswith(pod.RESULT_MARKER)]
    assert sum("mismatched types" in line for line in ran) == 1  # ran once


def test_failed_tests_are_named_in_the_result(tmp_path: Path) -> None:
    """0.20: not only counts."""
    script = "\n".join(
        [
            "echo '    Finished `test` profile [unoptimized] target(s) in 1s'",
            "echo 'test db::slabs::merges ... ok'",
            "echo 'test db::slabs::splits ... FAILED'",
            "echo 'test api::login_rejects_bad_tokens ... FAILED'",
            "echo 'test result: FAILED. 1 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out'",
            "exit 101",
        ]
    )
    data = _archive({"app/t.sh": script})
    result, _ = _run(tmp_path, data, _spec(data, ["sh", "t.sh"]))
    assert result["tests"]["failed"] == 2
    assert result["tests"]["failed_names"] == [
        "db::slabs::splits",
        "api::login_rejects_bad_tokens",
    ]


def test_a_link_out_of_the_tree_is_named(tmp_path: Path) -> None:
    """0.20: the message names the member and its target."""
    import io

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("app/sdk")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../../sdk"
        tar.addfile(info)
    data = buffer.getvalue()
    result, _ = _run(tmp_path, data, _spec(data, ["true"]))
    assert result["reason"] == "dev-upload-failed"
    assert "app/sdk -> ../../../sdk" in result["message"]
    assert result["path"] == "app/sdk"


def test_an_unchanged_cargo_lock_is_not_fetched_again(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """0.20: a warm run skips ``cargo fetch --locked`` (2 s each) when its
    lineage fetched this exact lock into this CARGO_HOME."""
    import stat

    log = tmp_path / "cargo.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "cargo"
    fake.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nmkdir -p "$CARGO_HOME"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    lock = '[[package]]\nname = "a"\n'
    data = _archive({"app/Cargo.lock": lock, "app/t.sh": "true"})
    first, _ = _run(tmp_path, data, _spec(data, ["sh", "t.sh"], prefetch="cargo"), "w1")
    again, _ = _run(tmp_path, data, _spec(data, ["sh", "t.sh"], prefetch="cargo"), "w2")
    assert first["prefetch"] == "ok" and again["prefetch"] == "cached"
    assert log.read_text().splitlines() == ["fetch --locked"]
    changed = _archive({"app/Cargo.lock": lock + 'version = "2"\n', "app/t.sh": "true"})
    third, _ = _run(
        tmp_path, changed, _spec(changed, ["sh", "t.sh"], prefetch="cargo"), "w3"
    )
    assert third["prefetch"] == "ok" and len(log.read_text().splitlines()) == 2


def test_a_rustc_197_ice_drops_incremental_and_runs_once_more(tmp_path: Path) -> None:
    """0.64.0: rustc 1.97 words its panic without "internal compiler error";
    the run still drops the lineage's incremental/ and passes on the retry."""
    script = "\n".join(
        [
            'state="$CARGO_TARGET_DIR/debug/incremental/shop-1/s-1/query-cache.bin"',
            'if [ -f "$state" ]; then',
            "  echo '    Checking shop-query v0.1.0 (/x)'",
            "  echo \"thread 'rustc' (1169) panicked at /rustc-dev/2d81/compiler/"
            'rustc_middle/src/dep_graph/graph.rs:1301:32:"',
            "  echo 'error: the compiler unexpectedly panicked. This is a bug'",
            "  echo 'error: could not compile `shop-query` (test \"suite\")'",
            "  exit 101",
            "fi",
            "echo '    Finished `dev` profile [unoptimized] target(s) in 1.0s'",
        ]
    )
    data = _archive({"app/check.sh": script})
    first, _ = _run(tmp_path, data, _spec(data, ["true"]), "seed")
    target = Path(first["cache"]["lineage_dir"]) / "target"
    (target / "debug/incremental/shop-1/s-1").mkdir(parents=True)
    (target / "debug/incremental/shop-1/s-1/query-cache.bin").write_bytes(b"x")
    (target / "debug/deps").mkdir(parents=True)
    (target / "debug/deps/libshop-1.rmeta").write_text("kept")
    result, lines = _run(tmp_path, data, _spec(data, ["sh", "check.sh"]), "ice")
    assert result["state"] == "passed", result
    assert result["cache"]["repaired"]["kind"] == "incremental"
    assert any("corrupted cache (incremental)" in line for line in lines)
    assert not (target / "debug/incremental").exists()
    assert (target / "debug/deps/libshop-1.rmeta").exists()


# ------------------------------------------------- damaged lineages (0.69.0)
#: A dependency's artifact the damaged lineage holds (its presence is the damage).
_DAMAGED = "$CARGO_TARGET_DIR/debug/deps/libarrow_ord-05e6.rlib"
_ICE_LINES = [
    "  echo '   Compiling shop-query v0.1.0 (/x)'",
    "  echo \"thread 'rustc' (1169) panicked at compiler/rustc_middle/src/"
    'query/mod.rs:10:1:"',
    "  echo 'error: the compiler unexpectedly panicked. This is a bug'",
    "  echo 'error: could not compile `shop-query` (lib)'",
]
_LINK_LINES = [
    "  echo 'error: linking with `cc` failed: exit status: 1'",
    f'  echo "  = note: rust-lld: error: {_DAMAGED}(arrow_ord-05e6.o): '
    'unknown file type"',
    "  echo 'error: could not compile `shop` (bin \"shop\")'",
]
_SEGV_LINES = [
    "  echo '   Compiling syn v2.0.87'",
    "  echo 'error: rustc interrupted by SIGSEGV, printing backtrace'",
    "  echo 'error: could not compile `syn` (lib)'",
    "  echo 'Caused by:'",
    "  echo \"  process didn't exit successfully: \\`/opt/rust/bin/rustc "
    "--crate-name syn --edition=2021\\` (signal: 11, SIGSEGV: invalid memory "
    'reference)"',
]
_FINISHED = "echo '    Finished `dev` profile [unoptimized] target(s) in 1.0s'"


def _damaging(lines: list[str]) -> str:
    """A build that fails with ``lines`` while the damaged artifact is there."""
    return "\n".join(
        [f'if [ -f "{_DAMAGED}" ]; then', *lines, "  exit 101", "fi", _FINISHED]
    )


def _warm(tmp_path: Path, data: bytes) -> Path:
    """A warm lineage holding the damaged artifact and some incremental state."""
    first, _ = _run(tmp_path, data, _spec(data, ["true"]), "seed")
    lineage = Path(first["cache"]["lineage_dir"])
    for name in ("debug/deps/libarrow_ord-05e6.rlib", "debug/incremental/q-1/s/x"):
        (lineage / "target" / name).parent.mkdir(parents=True, exist_ok=True)
        (lineage / "target" / name).write_text("x")
    return lineage


@pytest.mark.parametrize(
    ("lines", "after"),
    [(_ICE_LINES, "incremental"), (_LINK_LINES, None), (_SEGV_LINES, None)],
    ids=["ice-persists", "link-into-deps", "rustc-sigsegv"],
)
def test_a_damaged_lineage_loses_its_target_and_runs_once_more_cold(
    tmp_path: Path, lines: list[str], after: str | None
) -> None:
    """0.69.0: an ICE the incremental repair does not cure, a linker refusing
    a cached rlib, rustc killed by SIGSEGV: target/ goes, the run retries."""
    data = _archive({"app/check.sh": _damaging(lines)})
    lineage = _warm(tmp_path, data)
    result, out = _run(tmp_path, data, _spec(data, ["sh", "check.sh"]), "run")
    assert result["state"] == "passed", result
    repaired = result["cache"]["repaired"]
    assert repaired["kind"] == "lineage" and repaired.get("after") == after
    assert repaired["reason"] and repaired["first_error"]
    assert any("damaged lineage" in line and "cold" in line for line in out)
    assert not (lineage / "target/debug/deps/libarrow_ord-05e6.rlib").exists()
    assert (lineage / "tree" / "app" / "check.sh").is_file()  # the sources stay
    assert "damaged" not in json.loads((lineage / "meta.json").read_text())
    assert "damaged" not in result["cache"]
    assert result["health"]["repaired"] is True


def test_code_errors_never_drop_the_lineage_target(tmp_path: Path) -> None:
    """0.69.0: the code's own undefined symbol, a link error on a cold
    lineage, a test binary segfaulting while the tests run: no lineage repair."""
    target = "$CARGO_TARGET_DIR/debug/deps"
    undefined = _archive(
        {
            "app/check.sh": "\n".join(
                [
                    "echo '  = note: rust-lld: error: undefined symbol: shop::gone'",
                    f'echo "          >>> referenced by {target}/shop-1a.shop.o"',
                    "echo 'error: could not compile `shop` (bin \"shop\")'",
                    "exit 101",
                ]
            )
        }
    )
    lineage = _warm(tmp_path, undefined)
    result, _ = _run(tmp_path, undefined, _spec(undefined, ["sh", "check.sh"]), "u")
    assert result["state"] == "failed"
    assert result["cache"]["repaired"]["kind"] == "link"  # the 0.19 repair only
    assert (lineage / "target/debug/deps/libarrow_ord-05e6.rlib").exists()

    segfault = _archive(
        {
            "app/check.sh": "\n".join(
                [
                    _FINISHED,
                    f'echo "     Running unittests src/lib.rs ({target}/shop-1a)"',
                    "echo 'error: test failed, to rerun pass `--lib`'",
                    "echo 'Caused by:'",
                    f"echo \"  process didn't exit successfully: \\`{target}/shop-1a\\`"
                    ' (signal: 11, SIGSEGV: invalid memory reference)"',
                    "exit 101",
                ]
            )
        }
    )
    result, _ = _run(tmp_path, segfault, _spec(segfault, ["sh", "check.sh"]), "s")
    assert result["state"] == "failed" and "repaired" not in result["cache"]
    assert (lineage / "target/debug/deps/libarrow_ord-05e6.rlib").exists()

    cold = _archive({"app/check.sh": "\n".join([*_LINK_LINES, "exit 101"])})
    spec = _spec(cold, ["sh", "check.sh"], lineage_key="cold-rust")
    result, _ = _run(tmp_path, cold, spec, "c")
    assert result["cache"]["warm"] is False
    assert result["state"] == "failed" and "repaired" not in result["cache"]


def test_a_lineage_still_damaged_after_the_cold_retry_is_removed(
    tmp_path: Path,
) -> None:
    """0.69.0: the cold retry fails the same way: the lineage is removed at
    release, and one a killed run marked damaged is never taken again."""
    always = _archive({"app/check.sh": "\n".join([*_SEGV_LINES, "exit 101"])})
    lineage = _warm(tmp_path, always)
    result, out = _run(tmp_path, always, _spec(always, ["sh", "check.sh"]), "run")
    assert result["state"] == "failed"
    assert result["cache"]["repaired"]["kind"] == "lineage"
    assert "rustc interrupted by SIGSEGV" in result["cache"]["damaged"]
    assert any("still damaged" in line for line in out)
    assert not lineage.exists()
    again, _ = _run(tmp_path, always, _spec(always, ["true"]), "next")
    assert again["state"] == "passed" and again["cache"]["warm"] is False

    # A run killed after marking its lineage damaged: the next run skips it.
    held = Path(again["cache"]["lineage_dir"])
    meta = json.loads((held / "meta.json").read_text())
    (held / "meta.json").write_text(json.dumps({**meta, "damaged": "rustc crash"}))
    (held / "target" / "marker").write_text("x")
    last, _ = _run(tmp_path, always, _spec(always, ["true"]), "last")
    assert last["cache"]["warm"] is False and "seed" not in last["cache"]
    assert not (Path(last["cache"]["lineage_dir"]) / "target" / "marker").exists()
