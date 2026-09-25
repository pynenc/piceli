"""Temporary directories are removed on success, error, interrupt and signals."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from piceli import tempfiles
from piceli.tempfiles import (
    cleanup_all,
    is_temporary,
    make_directory,
    temporary_directory,
    temporary_file,
    tracked,
)

REPO = Path(__file__).resolve().parents[2]


def test_names_are_recognised() -> None:
    for name in (
        "piceli-tls-abcd_123",
        "piceli-docker-archive-a1b2c3d4",
        ".piceli-oci-zz99yy88",
        "piceli-snapshot-a1b2c3d4.sqlite",
    ):
        assert is_temporary(name), name
    for name in (
        ".piceli-state.json",  # the shared-state marker is durable
        "piceli-deploy",
        "test_piceli0",
        "pytest-of-me",
    ):
        assert not is_temporary(name), name


def test_directory_is_private_and_removed_on_success_error_and_interrupt() -> None:
    with temporary_directory("tls") as path:
        assert is_temporary(path.name) and path.parent == Path(tempfile.gettempdir())
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert str(path) in tracked()
    assert not path.exists() and str(path) not in tracked()
    for error in (ValueError, KeyboardInterrupt):
        with pytest.raises(error), temporary_directory("build") as path:
            (path / "key").write_text("material")
            raise error()
        assert not path.exists() and str(path) not in tracked()


def test_hidden_directory_next_to_an_output(tmp_path: Path) -> None:
    with temporary_directory("oci", dir=tmp_path, hidden=True) as path:
        assert path.parent == tmp_path and path.name.startswith(".piceli-oci-")
    assert list(tmp_path.iterdir()) == []


def test_partial_file_is_removed_when_the_write_fails(tmp_path: Path) -> None:
    partial = tmp_path / ".receipt.json.partial"
    with pytest.raises(OSError), temporary_file(partial):
        partial.write_text("half")
        raise OSError("disk full")
    assert not partial.exists()
    with temporary_file(partial):
        partial.write_text("whole")
    assert partial.read_text() == "whole" and str(partial) not in tracked()


def test_cleanup_all_removes_what_is_still_live(tmp_path: Path) -> None:
    calls: list[str] = []
    path = make_directory("ref")
    tempfiles.track(path, lambda: calls.append("worktrees"))
    other = make_directory("runnable-inspect")
    cleanup_all()
    assert not path.exists() and not other.exists()
    assert calls == ["worktrees"] and tracked() == []


@pytest.mark.parametrize("name", ["SIGTERM", "SIGHUP"])
def test_a_terminating_signal_removes_live_directories(
    tmp_path: Path, name: str
) -> None:
    """A cancelled CI job (SIGTERM) or a closed terminal (SIGHUP) leaves nothing."""
    script = textwrap.dedent(
        f"""
        import os, signal, sys
        from piceli.tempfiles import install_signal_cleanup, temporary_directory
        install_signal_cleanup()
        with temporary_directory("tls") as path:
            (path / "tls.key").write_text("private")
            print(path, flush=True)
            os.kill(os.getpid(), signal.{name})
            sys.exit(0)  # not reached: the default action ends the process
        """
    )
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    process = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        timeout=60,
        check=False,
    )
    created = Path(process.stdout.strip())
    assert created.parent == tmp_path.resolve() or created.parent == tmp_path
    assert process.returncode == -getattr(signal, name)  # the exit status is unchanged
    assert not created.exists()
    assert list(tmp_path.iterdir()) == []


def test_an_ignored_signal_stays_ignored() -> None:
    script = textwrap.dedent(
        """
        import signal
        from piceli.tempfiles import install_signal_cleanup
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        install_signal_cleanup()
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
        assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
        """
    )
    subprocess.run([sys.executable, "-c", script], cwd=REPO, check=True, timeout=60)


def test_importing_installs_nothing() -> None:
    script = (
        "import atexit, signal, piceli.tempfiles\n"
        "assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL\n"
        "assert not piceli.tempfiles._hooked\n"
    )
    subprocess.run([sys.executable, "-c", script], cwd=REPO, check=True, timeout=60)


def test_ref_worktrees_directory_is_tracked(tmp_path: Path) -> None:
    """``--ref`` checkouts register their removal (worktrees included)."""
    from piceli.pipeline.refs import SourceCheckouts

    checkouts = SourceCheckouts({})
    checkouts.materialise()
    directory = checkouts._directory
    assert directory is not None and str(directory) in tracked()
    checkouts.close()
    assert not directory.exists() and tracked() == []


# ------------------------------------------- partial files of failed writes


def failing_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    def replace(_source: object, _target: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", replace)


def test_a_failed_receipt_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.artifacts.delivery import write_receipt

    failing_replace(monkeypatch)
    with pytest.raises(OSError):
        write_receipt(tmp_path / "receipt.json", {"state": "succeeded"})
    assert list(tmp_path.iterdir()) == []


def test_a_failed_build_output_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.artifacts.build_spec import _write_atomic

    failing_replace(monkeypatch)
    with pytest.raises(OSError):
        _write_atomic(tmp_path / "out" / "receipt.json", "{}")
    assert list((tmp_path / "out").iterdir()) == []


def test_a_failed_catalog_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.k8s.release import ReleaseCatalog

    catalog = ReleaseCatalog(tmp_path / "state" / "catalog.json")
    failing_replace(monkeypatch)
    with pytest.raises(OSError):
        catalog._write({"schema_version": 1, "releases": [], "selected": None})
    assert list((tmp_path / "state").iterdir()) == []


def test_a_sqlite_snapshot_copy_is_a_named_tracked_temporary(tmp_path: Path) -> None:
    import sqlite3

    from piceli.state.snapshot import _read

    database = tmp_path / "journal.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("create table t (x)")
    connection.commit()
    connection.close()
    seen: list[str] = []
    real = tempfiles.track

    def spy(path: Path | str, cleanup: object = None) -> None:
        seen.append(Path(path).name)
        real(path, cleanup)  # type: ignore[arg-type]

    import piceli.state.snapshot as snapshot

    original = snapshot.track
    snapshot.track = spy  # type: ignore[assignment]
    try:
        assert _read(database).startswith(b"SQLite format 3")
    finally:
        snapshot.track = original
    assert len(seen) == 1 and is_temporary(seen[0]) and tracked() == []
