"""0.71.0: a lineage whose files the filesystem cannot read (EUCLEAN, EIO)
is set aside in ``quarantine/`` and the run continues on a new lineage."""

from __future__ import annotations

import errno
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from piceli.dev import pod
from tests.unit.dev.test_pod import _archive, _run, _spec

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")

EUCLEAN = getattr(errno, "EUCLEAN", 117)
#: A file in a lineage's folder (not its tree) that marks it damaged here.
BAD = "bad-disk"


def _damaged_disk(monkeypatch: pytest.MonkeyPatch, code: int = EUCLEAN) -> None:
    """Copies into a lineage marked :data:`BAD` fail like a corrupt folder."""
    copy = shutil.copyfile

    def failing(source: Any, dest: Any, **kwargs: Any) -> Any:
        for parent in Path(dest).parents:
            if (parent / BAD).exists():
                raise OSError(code, "Structure needs cleaning", str(dest))
        return copy(source, dest, **kwargs)

    monkeypatch.setattr(pod.shutil, "copyfile", failing)


def _first(tmp_path: Path) -> tuple[Path, bytes]:
    data = _archive({"app/a.txt": "one\n"})
    result, _ = _run(tmp_path, data, _spec(data, ["true"]), "w1")
    assert result["state"] == "passed"
    return Path(result["cache"]["lineage_dir"]), data


def test_a_lineage_the_sync_cannot_write_is_set_aside_and_the_run_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lineage, _ = _first(tmp_path)
    (lineage / BAD).touch()
    _damaged_disk(monkeypatch)
    data = _archive({"app/a.txt": "two\n", "app/b.png": "png"})
    result, lines = _run(tmp_path, data, _spec(data, ["cat", "a.txt"]), "w2")
    assert result["state"] == "passed", result
    assert "two" in lines
    repaired = result["cache"]["repaired"]
    assert repaired["kind"] == "lineage-quarantined"
    assert repaired["lineage"] == "app-rust/0"
    assert repaired["error"].startswith("EUCLEAN: ")
    assert ".piceli-sync" in repaired["error"]
    assert any("set aside in quarantine/" in line for line in lines)
    moved = tmp_path / "cache" / repaired["moved_to"]
    assert (moved / BAD).is_file() and (moved / "tree").is_dir()
    # A fresh lineage, not seeded from the damaged one.
    fresh = Path(result["cache"]["lineage_dir"])
    assert not (fresh / BAD).exists() and "seed" not in result["cache"]
    assert result["cache"]["warm"] is False
    # The budget listing (piceli dev status) shows it, bytes unknown.
    shown = [i for i in result["cache"]["trim"]["lineages"] if i.get("quarantined")]
    assert shown == [
        {
            "name": repaired["moved_to"],
            "lineage": "app-rust/0",
            "bytes": None,
            "last_used": shown[0]["last_used"],
            "busy": False,
            "run": "r1",
            "quarantined": True,
            "error": repaired["error"],
            "path": repaired["moved_to"],
        }
    ]
    # The next run never takes it; the quarantine stays until a person acts.
    again, _ = _run(tmp_path, data, _spec(data, ["true"]), "w3")
    assert again["state"] == "passed" and "repaired" not in again["cache"]
    assert again["cache"]["lineage_dir"] == str(fresh)
    assert (moved / BAD).is_file()
    budget = pod.evict(tmp_path / "cache", 10**9)
    assert [i["name"] for i in budget["lineages"] if i.get("quarantined")] == [
        repaired["moved_to"]
    ]


def test_a_lineage_that_cannot_be_moved_is_marked_and_never_taken_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lineage, _ = _first(tmp_path)
    (lineage / BAD).touch()
    _damaged_disk(monkeypatch, errno.EIO)

    # No room to move it aside: a file where the quarantine folder goes.
    (tmp_path / "cache" / pod.QUARANTINE).write_text("")
    data = _archive({"app/a.txt": "two\n"})
    result, _ = _run(tmp_path, data, _spec(data, ["true"]), "w2")
    assert result["state"] == "passed", result
    repaired = result["cache"]["repaired"]
    assert repaired["kind"] == "lineage-quarantined"
    assert repaired["moved_to"] is None and repaired["marked"] is True
    assert result["cache"]["lineage"] == "app-rust/1"
    shown = [i for i in result["cache"]["trim"]["lineages"] if i.get("quarantined")]
    assert [i["name"] for i in shown] == ["app-rust/0"]
    assert shown[0]["bytes"] is None
    for name in ("w3", "w4"):
        again, _ = _run(tmp_path, data, _spec(data, ["true"], max_lineages=1), name)
        assert again["state"] == "passed"
        assert again["cache"]["lineage"] == "app-rust/1"
    cleared = pod.clear_cache(tmp_path / "cache")
    assert [c["lineage"] for c in cleared["cleared"]] == ["app-rust/1"]


def test_a_fresh_lineage_that_fails_the_same_way_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _first(tmp_path)
    copy = shutil.copyfile

    def failing(source: Any, dest: Any, **kwargs: Any) -> Any:
        if "/lineages/" in str(dest):
            raise OSError(EUCLEAN, "Structure needs cleaning", str(dest))
        return copy(source, dest, **kwargs)

    monkeypatch.setattr(pod.shutil, "copyfile", failing)
    data = _archive({"app/a.txt": "two\n"})
    result, _ = _run(tmp_path, data, _spec(data, ["true"]), "w2")
    assert result["state"] == "error"
    assert result["reason"] == "dev-lineage-damaged"
    assert "disk needs checking" in result["message"]
    assert len(result["quarantined"]) == 2
    assert len(list((tmp_path / "cache" / "quarantine").glob("app-rust-*.json"))) == 2


def test_only_filesystem_faults_inside_the_lineage_count(tmp_path: Path) -> None:
    lineage = tmp_path / "lineages" / "k" / "0"
    inside = str(lineage / "tree" / "a.png.piceli-sync")
    assert pod.lineage_fault(OSError(EUCLEAN, "x", inside), lineage) == (
        f"EUCLEAN: {inside}"
    )
    assert pod.lineage_fault(OSError(errno.ENOENT, "x", inside), lineage) is None
    assert pod.lineage_fault(OSError(errno.ENOSPC, "x", inside), lineage) is None
    elsewhere = str(tmp_path / "work" / "tree" / "a")
    assert pod.lineage_fault(OSError(errno.EIO, "x", elsewhere), lineage) is None
    assert pod.lineage_fault(OSError(errno.EIO, "x"), lineage) is None
