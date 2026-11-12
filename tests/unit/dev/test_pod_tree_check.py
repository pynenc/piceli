"""0.72.0: a lineage's tree is checked against the uploaded commit. What a
run's command did to tracked files (a test deleting or rewriting a fixture)
is restored by the next in-place refresh, and a tree that still lacks a
tracked file fails the run with ``dev-tree-mismatch``."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from piceli.dev import pod
from piceli.dev.delta import pack_delta, plan_source
from piceli.dev.pack import SourceRequest
from tests.unit.dev.test_pack import commit, git, repo
from tests.unit.dev.test_pod import _spec

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

FIXTURE = "examples/fixture.json"
CONTENT = '{"name": "web"}\n'


def _run(
    tmp_path: Path, work: Path, name: str, command: str
) -> tuple[dict[str, Any], list[str]]:
    plan = plan_source(SourceRequest("web", work, None))
    packed = pack_delta([plan], {"web": set()}, tmp_path / f"out-{name}")
    pod_work = tmp_path / name
    (pod_work / "upload").mkdir(parents=True)
    shutil.copyfile(packed.path, pod_work / "upload" / "tree.tar.gz")
    (pod_work / "upload" / "ready").touch()
    spec = _spec(
        packed.path.read_bytes(),
        ["sh", "-c", command],
        cwd="web",
        sources=[plan.describe()],
    )
    lines: list[str] = []
    result = pod.main(spec, pod_work, tmp_path / "cache", emit=lines.append)
    return result, lines


def _tree(result: dict[str, Any]) -> Path:
    return Path(result["cache"]["lineage_dir"]) / "tree" / "web"


def _work(tmp_path: Path) -> Path:
    return repo(
        tmp_path / "work",
        {"a.rs": "1\n", FIXTURE: CONTENT, "run.sh": "echo run\n"},
    )


def test_a_file_removed_and_back_again_is_present_through_full_syncs(
    tmp_path: Path,
) -> None:
    work = _work(tmp_path)
    with_file = git(work, "rev-parse", "HEAD")
    (work / FIXTURE).unlink()
    without = commit(work, {})
    git(work, "reset", "-q", "--hard", with_file)
    first, _ = _run(tmp_path, work, "a", "true")  # tree A
    tree = _tree(first)
    assert (tree / FIXTURE).read_text() == CONTENT
    git(work, "reset", "-q", "--hard", without)
    second, _ = _run(tmp_path, work, "b", "true")  # tree B: without the file
    assert second["state"] == "passed", second
    assert _tree(second) == tree and not (tree / FIXTURE).exists()
    git(work, "reset", "-q", "--hard", with_file)
    third, _ = _run(tmp_path, work, "a2", f"cat {FIXTURE}")  # tree A again
    assert third["state"] == "passed", third
    assert third["sync"].get("mode") != "in-place"
    assert (tree / FIXTURE).read_text() == CONTENT


def test_a_file_removed_and_back_again_is_present_through_in_place_refreshes(
    tmp_path: Path,
) -> None:
    work = _work(tmp_path)
    first, _ = _run(tmp_path, work, "a", "true")
    tree = _tree(first)
    (work / FIXTURE).unlink()  # tree B: the same head, the file deleted
    second, _ = _run(tmp_path, work, "b", "true")
    assert second["sync"]["mode"] == "in-place"
    assert not (tree / FIXTURE).exists()
    git(work, "checkout", "-q", "--", FIXTURE)  # tree A again
    third, lines = _run(tmp_path, work, "a2", f"cat {FIXTURE}")
    assert third["state"] == "passed", third
    assert third["sync"]["mode"] == "in-place"
    assert (tree / FIXTURE).read_text() == CONTENT
    assert '{"name": "web"}' in lines


def test_a_tracked_file_the_command_deleted_is_restored_by_the_next_refresh(
    tmp_path: Path,
) -> None:
    work = _work(tmp_path)
    first, _ = _run(tmp_path, work, "first", f"rm {FIXTURE}")
    assert first["state"] == "passed", first
    tree = _tree(first)
    assert not (tree / FIXTURE).exists()
    again, lines = _run(tmp_path, work, "again", f"cat {FIXTURE}")
    assert again["state"] == "passed", again
    assert again["sync"]["mode"] == "in-place"
    assert again["sync"]["restored"] == 1
    assert again["sync"]["restored_sample"] == [f"web/{FIXTURE}"]
    assert (tree / FIXTURE).read_text() == CONTENT
    assert any("restored 1 tracked file(s)" in line for line in lines)
    third, _ = _run(tmp_path, work, "third", "true")
    assert third["sync"]["restored"] == 0
    assert "restored_sample" not in third["sync"]


@pytest.mark.parametrize(
    "change",
    [
        f": > {FIXTURE}",
        f"printf x >> {FIXTURE}",
        "chmod +x a.rs",
        "rm a.rs; mkdir a.rs",
    ],
    ids=["truncated", "grown", "made-executable", "file-to-folder"],
)
def test_a_tracked_file_the_command_changed_is_restored(
    tmp_path: Path, change: str
) -> None:
    work = _work(tmp_path)
    first, _ = _run(tmp_path, work, "first", change)
    assert first["state"] == "passed", first
    again, _ = _run(tmp_path, work, "again", "true")
    assert again["state"] == "passed", again
    assert again["sync"]["restored"] == 1
    tree = _tree(again)
    assert (tree / FIXTURE).read_text() == CONTENT
    assert (tree / "a.rs").is_file() and (tree / "a.rs").read_text() == "1\n"
    assert not (tree / "a.rs").stat().st_mode & 0o111


def test_a_folder_replaced_by_a_file_is_restored(tmp_path: Path) -> None:
    work = _work(tmp_path)
    first, _ = _run(tmp_path, work, "first", "rm -r examples; echo x > examples")
    assert first["state"] == "passed", first
    again, _ = _run(tmp_path, work, "again", f"cat {FIXTURE}")
    assert again["state"] == "passed", again
    assert again["sync"]["restored"] == 1
    assert (_tree(again) / FIXTURE).read_text() == CONTENT


def test_a_file_the_runs_own_diff_deletes_is_not_restored(tmp_path: Path) -> None:
    work = _work(tmp_path)
    (work / FIXTURE).unlink()  # uncommitted: in the run's overlay
    first, _ = _run(tmp_path, work, "first", "true")
    assert first["state"] == "passed", first
    again, _ = _run(tmp_path, work, "again", f"test ! -e {FIXTURE}")
    assert again["state"] == "passed", again
    assert again["sync"]["mode"] == "in-place"
    assert again["sync"]["restored"] == 0
    assert not (_tree(again) / FIXTURE).exists()


def test_a_tree_still_missing_a_file_fails_the_run_and_the_next_syncs_fully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _work(tmp_path)
    first, _ = _run(tmp_path, work, "first", f"rm {FIXTURE}")
    assert first["state"] == "passed", first
    with monkeypatch.context() as patched:
        patched.setattr(pod, "restore", lambda *args: None)  # a broken repair
        failed, _ = _run(tmp_path, work, "failed", "echo ran")
    assert failed["state"] == "error", failed
    assert failed["reason"] == "dev-tree-mismatch"
    assert FIXTURE in failed["message"]
    again, _ = _run(tmp_path, work, "again", f"cat {FIXTURE}")
    assert again["state"] == "passed", again
    assert again["sync"].get("mode") != "in-place"
    assert (_tree(again) / FIXTURE).read_text() == CONTENT


def test_the_check_skips_submodules_and_sees_kind_and_size(tmp_path: Path) -> None:
    work = _work(tmp_path)
    (work / "link").symlink_to("a.rs")
    commit(work, {})
    first, _ = _run(tmp_path, work, "first", "true")
    tree = _tree(first)
    plan = plan_source(SourceRequest("web", work, None))
    mirror = next((tmp_path / "cache" / "git").glob("*.git"))
    source = pod.DeltaSource("web", mirror, plan.head, tmp_path / "none")
    kinds = {path: kind for kind, _, path in source.tracked()}
    assert kinds["link"] == "link" and kinds["a.rs"] == "file"
    assert pod.differing(source, tree, set()) == []
    (tree / "link").unlink()
    (tree / "link").symlink_to("other.rs")  # another target, another size
    assert pod.differing(source, tree, set()) == ["link"]
    assert pod.differing(source, tree, {"link"}) == []
