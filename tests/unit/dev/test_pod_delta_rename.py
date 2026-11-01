"""0.50.0: a delta upload with staged renames, and a second run on the same
head after a rename (a lineage refreshed in place)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from piceli.dev.delta import plan_source
from piceli.dev.pack import SourceRequest
from tests.unit.dev.test_pack import git, repo
from tests.unit.dev.test_pod_delta import _fetch, _run_delta

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _have(tmp_path: Path, work: Path) -> set[str]:
    plan = plan_source(SourceRequest("shop", work, None))
    return set(_fetch(tmp_path, [plan.describe()], None)["sources"]["shop"]["present"])


def test_a_staged_rename_uploads_and_runs(tmp_path: Path) -> None:
    work = repo(tmp_path / "work", {"a.rs": "1", "old.rs": "line\n" * 20})
    first, _ = _run_delta(tmp_path, work, set(), "first")
    assert first["state"] == "passed", first
    git(work, "mv", "old.rs", "new.rs")
    again, lines = _run_delta(tmp_path, work, _have(tmp_path, work), "again")
    assert again["state"] == "passed", again
    assert "line" in lines and "old.rs" not in lines


def test_a_staged_rename_with_edits_on_both_sides(tmp_path: Path) -> None:
    work = repo(tmp_path / "work", {"a.rs": "1", "old.rs": "line\n" * 20})
    first, _ = _run_delta(tmp_path, work, set(), "first")
    assert first["state"] == "passed", first
    git(work, "mv", "old.rs", "new.rs")
    (work / "new.rs").write_text("line\n" * 19 + "edited\n")
    (work / "a.rs").write_text("2\n")
    git(work, "add", "a.rs")
    (work / "a.rs").write_text("3, unstaged\n")
    again, lines = _run_delta(tmp_path, work, _have(tmp_path, work), "again")
    assert again["state"] == "passed", again
    assert "edited" in lines and "3, unstaged" in lines


def test_each_run_on_the_same_head_carries_the_latest_edits(tmp_path: Path) -> None:
    work = repo(tmp_path / "work", {"a.rs": "1", "new.rs": "n"})
    first, _ = _run_delta(tmp_path, work, set(), "first")
    assert first["state"] == "passed", first
    for step in range(3):
        (work / "a.rs").write_text(f"edit {step}\n")
        run, lines = _run_delta(tmp_path, work, _have(tmp_path, work), f"r{step}")
        assert run["state"] == "passed", run
        assert f"edit {step}" in lines, (step, lines)
    git(work, "mv", "new.rs", "renamed.rs")
    run, lines = _run_delta(tmp_path, work, _have(tmp_path, work), "renamed")
    assert run["state"] == "passed", run
    (work / "a.rs").write_text("after rename\n")
    run, lines = _run_delta(tmp_path, work, _have(tmp_path, work), "after")
    assert run["state"] == "passed", run
    assert "after rename" in lines, lines


def test_an_older_clients_rename_diff_names_both_paths(tmp_path: Path) -> None:
    from piceli.dev import pod

    work = repo(tmp_path / "work", {"old name.rs": "line\n" * 20, "b.rs": "b"})
    git(work, "mv", "old name.rs", "new.rs")
    git(work, "mv", "b.rs", "cañón.rs")  # C-quoted in the diff headers
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "diff").write_bytes(
        git(
            work, "-c", "core.quotePath=true", "diff", "--binary", "-M", "HEAD"
        ).encode()
        + b"\n"
    )
    source = pod.DeltaSource("shop", tmp_path / "mirror", "0" * 40, incoming)
    assert source.overlay() == ["b.rs", "cañón.rs", "new.rs", "old name.rs"]
