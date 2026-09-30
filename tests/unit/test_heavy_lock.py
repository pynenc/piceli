"""The machine-wide heavy-work lock: two real processes, signals, receipts."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from piceli.maintenance import heavy

PY = sys.executable


def piceli(
    state: Path, *args: str, **kwargs: Any
) -> subprocess.Popen[str]:  # pragma: no cover - helper
    env = {**os.environ, heavy.DIR_ENV: str(state)}
    env.pop(heavy.HELD_ENV, None)
    return subprocess.Popen(
        [PY, "-m", "piceli", *args],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )


def stamp_script(out: Path, seconds: float) -> list[str]:
    code = (
        "import time,sys;"
        f"f=open({str(out)!r},'w');f.write(str(time.time())+'\\n');f.flush();"
        f"time.sleep({seconds});f.write(str(time.time())+'\\n');f.close()"
    )
    return [PY, "-c", code]


def wait_for(path: Path, timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while not path.exists() or not path.read_text().strip():
        assert time.monotonic() < end, f"{path} never written"
        time.sleep(0.05)


def last_json(stdout: str) -> dict[str, Any]:
    return json.loads(stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_second_process_waits_for_the_first(tmp_path: Path) -> None:
    state = tmp_path / "state"
    a_out, b_out = tmp_path / "a", tmp_path / "b"
    first = piceli(state, "heavy", "run", "--", *stamp_script(a_out, 1.5))
    wait_for(a_out)
    second = piceli(state, "heavy", "run", "--", *stamp_script(b_out, 0.1))
    out_a, _ = first.communicate(timeout=60)
    out_b, err_b = second.communicate(timeout=60)
    assert first.returncode == 0 and second.returncode == 0
    a_start, a_end = map(float, a_out.read_text().split())
    b_start, b_end = map(float, b_out.read_text().split())
    assert b_start >= a_end
    assert "waiting for the heavy-work lock held by" in err_b
    receipt_b = last_json(out_b)
    assert receipt_b["waited_seconds"] > 0.5
    assert len(heavy.HeavyLock(state).recent_receipts()) == 2


def test_killed_holder_releases_the_lock(tmp_path: Path) -> None:
    state = tmp_path / "state"
    marker = tmp_path / "child-pid"
    child = [
        PY,
        "-c",
        f"import os,time;open({str(marker)!r},'w').write(str(os.getpid()));time.sleep(60)",
    ]
    holder = piceli(state, "heavy", "run", "--", *child)
    wait_for(marker)
    child_pid = int(marker.read_text())
    try:
        assert heavy.HeavyLock(state).holder() is not None
        holder.kill()
        holder.wait(timeout=30)  # the orphaned child is still running
        lock = heavy.HeavyLock(state)
        lock.acquire(["next"], wait=5)  # would time out if the lock leaked
        lock.release()
    finally:
        try:
            os.killpg(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        holder.communicate(timeout=30)


def test_exit_code_and_signal_are_preserved(tmp_path: Path) -> None:
    state = tmp_path / "state"
    failing = piceli(
        state, "heavy", "run", "--", PY, "-c", "import sys; sys.exit(7)"
    )
    out, _ = failing.communicate(timeout=60)
    assert failing.returncode == 7
    assert last_json(out)["exit_code"] == 7

    marker = tmp_path / "started"
    sleeper = piceli(
        state,
        "heavy",
        "run",
        "--",
        PY,
        "-c",
        f"import time;open({str(marker)!r},'w').write('x');time.sleep(60)",
    )
    wait_for(marker)
    sleeper.send_signal(signal.SIGTERM)  # forwarded to the child's group
    out, _ = sleeper.communicate(timeout=30)
    assert sleeper.returncode == 128 + signal.SIGTERM
    assert last_json(out)["exit_code"] == 128 + signal.SIGTERM
    assert heavy.HeavyLock(state).holder() is None


def test_receipt_has_duration_peak_memory_and_no_environment(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    secret = "s3cr3t-value-xyz"
    env = {**os.environ, heavy.DIR_ENV: str(state), "MY_API_TOKEN": secret}
    done = subprocess.run(
        [
            PY,
            "-m",
            "piceli",
            "heavy",
            "run",
            "--name",
            "alloc",
            "--",
            PY,
            "-c",
            "b=bytearray(150*1024*1024);b[::4096]=b'x'*len(b[::4096]);"
            "import time;time.sleep(0.3)",
            "--api-token=" + secret,
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[2],
        check=False,
    )
    # the extra argument makes python treat it as a script arg: still exit 0
    receipt = last_json(done.stdout)
    assert receipt["schema"] == heavy.RECEIPT_SCHEMA
    assert receipt["name"] == "alloc"
    assert receipt["duration_seconds"] >= 0.3
    assert receipt["peak_memory_bytes"] >= 100 * 1024 * 1024
    assert receipt["sources"]["commit"]  # run inside the repository
    assert secret not in done.stdout + done.stderr
    stored = (next((state / "receipts").iterdir())).read_text()
    assert secret not in stored and "MY_API_TOKEN" not in stored
    assert "--api-token=***" in receipt["command"]


def test_wait_timeout_is_a_registered_rejection(tmp_path: Path) -> None:
    state = tmp_path / "state"
    marker = tmp_path / "started"
    holder = piceli(
        state,
        "heavy",
        "run",
        "--",
        PY,
        "-c",
        f"import time;open({str(marker)!r},'w').write('x');time.sleep(5)",
    )
    try:
        wait_for(marker)
        late = piceli(state, "heavy", "run", "--wait", "0.5", "--", PY, "-c", "pass")
        out, _ = late.communicate(timeout=30)
        assert late.returncode == 2
        assert last_json(out)["reason"] == "heavy-lock-timeout"
    finally:
        holder.terminate()  # forwarded to the child
        holder.communicate(timeout=30)


def test_status_shows_the_holder_and_receipts(tmp_path: Path) -> None:
    state = tmp_path / "state"
    marker = tmp_path / "started"
    holder = piceli(
        state,
        "heavy",
        "run",
        "--name",
        "gate",
        "--",
        PY,
        "-c",
        f"import time;open({str(marker)!r},'w').write('x');time.sleep(3)",
    )
    wait_for(marker)
    during = piceli(state, "heavy", "status", "--json")
    out, _ = during.communicate(timeout=30)
    document = last_json(out)
    assert document["state"] == "held"
    assert document["holder"]["pid"] == holder.pid
    assert document["holder"]["name"] == "gate"
    holder.communicate(timeout=30)
    after = piceli(state, "heavy", "status", "--json")
    document = last_json(after.communicate(timeout=30)[0])
    assert document["state"] == "free" and document["holder"] is None
    assert len(document["receipts"]) == 1


def test_empty_command_is_rejected(tmp_path: Path) -> None:
    done = piceli(tmp_path / "state", "heavy", "run")
    out, _ = done.communicate(timeout=30)
    assert done.returncode == 2
    assert last_json(out)["reason"] == "heavy-command-empty"
    missing = piceli(tmp_path / "state", "heavy", "run", "--", "no-such-program-xyz")
    out, _ = missing.communicate(timeout=30)
    assert last_json(out)["reason"] == "heavy-command-missing"
    assert heavy.HeavyLock(tmp_path / "state").holder() is None


def test_receipts_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heavy, "RECEIPT_KEEP", 3)
    lock = heavy.HeavyLock(tmp_path)
    for index in range(6):
        lock.write_receipt({"n": index})
    assert [r["n"] for r in lock.recent_receipts(10)] == [5, 4, 3]


def test_section_is_opt_in_and_reentrant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(heavy.DIR_ENV, str(tmp_path))
    monkeypatch.delenv(heavy.ENABLE_ENV, raising=False)
    monkeypatch.delenv(heavy.HELD_ENV, raising=False)
    with heavy.section("build x"):
        assert heavy.HeavyLock(tmp_path).holder() is None  # disabled: no lock
    monkeypatch.setenv(heavy.ENABLE_ENV, "1")
    with heavy.section("build x"):
        assert heavy.HeavyLock(tmp_path).holder() is not None
    assert heavy.HeavyLock(tmp_path).holder() is None
    (receipt,) = heavy.HeavyLock(tmp_path).recent_receipts()
    assert receipt["kind"] == "section" and receipt["exit_code"] == 0
    monkeypatch.setenv(heavy.HELD_ENV, "1")  # a child of `heavy run`
    with heavy.section("build x"):
        assert heavy.HeavyLock(tmp_path).holder() is None


def test_section_serializes_with_heavy_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(heavy.DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setenv(heavy.ENABLE_ENV, "1")
    monkeypatch.delenv(heavy.HELD_ENV, raising=False)
    marker = tmp_path / "started"
    holder = piceli(
        tmp_path / "state",
        "heavy",
        "run",
        "--",
        PY,
        "-c",
        f"import time;open({str(marker)!r},'w').write('x');time.sleep(1.5)",
    )
    wait_for(marker)
    began = time.monotonic()
    with heavy.section("build y"):
        waited = time.monotonic() - began
    holder.communicate(timeout=30)
    assert waited > 0.5


def test_redact_hides_secret_options() -> None:
    assert heavy.redact(["tool", "--token", "abc", "--name=x", "--password=p"]) == [
        "tool",
        "--token",
        "***",
        "--name=x",
        "--password=***",
    ]
