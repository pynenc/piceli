"""Git deltas: what a run uploads when the builder's mirror has a base."""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from piceli.dev.delta import canonical_url, pack_delta, plan_source
from piceli.dev.pack import SourceRequest
from tests.unit.dev.test_pack import commit, git, repo


def _members(path: Path) -> dict[str, bytes]:
    with tarfile.open(path) as tar:
        return {
            m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()
        }  # type: ignore[union-attr]


def _clone(origin: Path, at: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(origin), str(at)], check=True)
    git(at, "config", "user.email", "dev@example.com")
    git(at, "config", "user.name", "Dev")
    return at


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:Org/Repo.git", "https://github.com/org/repo"),
        ("https://github.com/org/repo.git", "https://github.com/org/repo"),
        ("https://user@GitHub.com/org/repo/", "https://github.com/org/repo"),
        ("ssh://git@github.com/org/repo.git", "https://github.com/org/repo"),
        ("/some/local/path", None),
    ],
)
def test_remote_urls_compare_by_host_and_path(url: str, expected: str | None) -> None:
    assert canonical_url(url) == expected


def test_a_worktree_plan_names_head_candidates_and_the_remote(tmp_path: Path) -> None:
    origin = repo(tmp_path / "origin", {"a.rs": "1"})
    for n in range(5):
        commit(origin, {"a.rs": str(n + 2)})
    work = _clone(origin, tmp_path / "work")
    pushed = git(work, "rev-parse", "HEAD")
    commit(work, {"b.rs": "local"})  # not pushed
    plan = plan_source(SourceRequest("shop", work, None))
    assert plan.head == git(work, "rev-parse", "HEAD")
    assert plan.candidates[0] == pushed  # the upstream tip comes first
    assert len(plan.candidates) <= 16 and plan.head not in plan.candidates
    assert plan.url is None  # a local path remote: nothing the cluster may fetch


def test_with_a_base_only_the_new_commits_and_the_diff_travel(tmp_path: Path) -> None:
    origin = repo(tmp_path / "origin", {"big.bin": "x" * 200_000, "a.rs": "1"})
    work = _clone(origin, tmp_path / "work")
    base = git(work, "rev-parse", "HEAD")
    commit(work, {"a.rs": "2"})
    (work / "a.rs").write_text("3, not committed")
    (work / "new.rs").write_text("untracked")
    plan = plan_source(SourceRequest("shop", work, None))
    packed = pack_delta([plan], {"shop": {base}}, tmp_path / "out")
    files = _members(packed.path)
    manifest = json.loads(files["manifest.json"])
    entry = manifest["sources"]["shop"]
    assert (
        entry["mode"] == "delta"
        and entry["base"] == [base]
        and entry["head"] == plan.head
    )
    assert (
        "sources/shop/bundle" in files
        and b"3, not committed" in files["sources/shop/diff"]
    )
    with tarfile.open(fileobj=io.BytesIO(files["sources/shop/untracked.tar"])) as tar:
        assert tar.getnames() == ["new.rs"]
    assert packed.bytes < 20_000  # not the 200 KB file again
    assert packed.sources["shop"]["mode"] == "delta"


def test_a_clean_tree_at_a_known_commit_uploads_almost_nothing(tmp_path: Path) -> None:
    origin = repo(tmp_path / "origin", {"big.bin": "x" * 200_000})
    work = _clone(origin, tmp_path / "work")
    head = git(work, "rev-parse", "HEAD")
    packed = pack_delta(
        [plan_source(SourceRequest("shop", work, None))],
        {"shop": {head}},
        tmp_path / "out",
    )
    files = _members(packed.path)
    assert "sources/shop/bundle" not in files  # the mirror has the head itself
    assert packed.bytes < 2_000


def test_without_a_known_base_the_whole_history_seeds_the_mirror(
    tmp_path: Path,
) -> None:
    work = repo(tmp_path / "work", {"a.rs": "1"})
    commit(work, {"a.rs": "2"})
    packed = pack_delta(
        [plan_source(SourceRequest("shop", work, None))], {}, tmp_path / "out"
    )
    entry = json.loads(_members(packed.path)["manifest.json"])["sources"]["shop"]
    assert entry["mode"] == "delta" and entry["base"] == []
    subprocess.run(
        ["git", "bundle", "verify", "-q", "-"],
        input=_members(packed.path)["sources/shop/bundle"],
        check=False,
    )


def test_a_ref_plan_has_no_diff(tmp_path: Path) -> None:
    work = repo(tmp_path / "work", {"a.rs": "1"})
    first = git(work, "rev-parse", "HEAD")
    commit(work, {"a.rs": "2"})
    (work / "a.rs").write_text("dirty")
    plan = plan_source(SourceRequest("shop", work, first))
    assert plan.head == first and plan.diff is False
    files = _members(pack_delta([plan], {"shop": {first}}, tmp_path / "out").path)
    assert "sources/shop/diff" not in files
