"""The build Job's script keeps each source's checkout on the cache claim
(0.23.0): incremental fetches, unchanged files keep their times."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from piceli.infra.builders import _SCRIPT

needs_flock = pytest.mark.skipif(
    shutil.which("flock") is None, reason="needs flock (util-linux), as the image has"
)


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@x", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def _run(tmp: Path, sources: str) -> str:
    script = (
        _SCRIPT.replace("/cache", str(tmp / "cache"))
        .replace("/work", str(tmp / "work"))
        .replace("exec piceli build job-run", "exec sh -c env stub")
    )
    (tmp / "work").mkdir(exist_ok=True)
    shutil.rmtree(tmp / "work" / "src", ignore_errors=True)  # a new pod each time
    result = subprocess.run(
        ["sh", "-c", script, "sh"],
        env={**os.environ, "PICELI_BUILD_SOURCES": sources, "GIT_USERNAME": "",
             "GIT_PASSWORD": "", "PICELI_BUILD_REGISTRY": "r",
             "PICELI_BUILD_NODE_REGISTRY": "n", "PICELI_BUILD_TIMEOUT": "60"},
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    return result.stdout


@needs_flock
def test_sources_stay_checked_out_and_unchanged_files_keep_their_times(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(remote, "init", "-q", "-b", "main")
    (remote / "kept.rs").write_text("fn kept() {}\n")
    (remote / "changed.rs").write_text("fn v1() {}\n")
    _git(remote, "add", "-A")
    _git(remote, "commit", "-qm", "one")
    _git(remote, "config", "uploadpack.allowAnySHA1InWant", "true")
    first = _git(remote, "rev-parse", "HEAD")
    out = _run(tmp_path, f"app file://{remote} {first}\n")
    assert "PICELI_BUILD_FETCH_SECONDS=" in out
    checkout = tmp_path / "cache" / "sources" / "app"
    linked = tmp_path / "work" / "src" / "app"
    assert linked.resolve() == checkout.resolve()
    assert _git(checkout, "rev-parse", "HEAD") == first
    old = 1_000_000_000
    for name in ("kept.rs", "changed.rs"):
        os.utime(checkout / name, (old, old))
    (remote / "changed.rs").write_text("fn v2() {}\n")
    _git(remote, "commit", "-qam", "two")
    second = _git(remote, "rev-parse", "HEAD")
    _run(tmp_path, f"app file://{remote} {second}\n")
    assert _git(checkout, "rev-parse", "HEAD") == second
    assert (checkout / "changed.rs").read_text() == "fn v2() {}\n"
    assert os.stat(checkout / "kept.rs").st_mtime == old
    assert os.stat(checkout / "changed.rs").st_mtime > old


def test_a_jobs_time_outside_its_commands_is_broken_down() -> None:
    """Where a Job's time goes besides its commands (0.23.0)."""
    from types import SimpleNamespace

    from piceli.infra.builders import _job_times

    cluster = SimpleNamespace(container_started="2026-10-19T10:00:40Z")
    found = _job_times("2026-10-19T10:00:00Z", 171.2, cluster, 12.0)
    assert found == {"job_seconds": 171.2, "start_seconds": 40.0, "fetch_seconds": 12.0}
    assert _job_times("2026-10-19T10:00:00Z", 5, SimpleNamespace(), None) == {
        "job_seconds": 5
    }
