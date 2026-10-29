"""The run pod's side of Git deltas: the init container's fetch and the
wrapper materializing a delta (stdlib wrapper, real git, local directories)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from piceli.dev import pod
from piceli.dev.delta import pack_delta, plan_source
from piceli.dev.pack import SourceRequest
from tests.unit.dev.test_delta import _clone
from tests.unit.dev.test_pack import commit, git, repo
from tests.unit.dev.test_pod import _spec

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _secret(tmp_path: Path, allowed: list[str], password: str = "s3cret") -> Path:
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "username").write_text("x-access-token")
    (secret / "password").write_text(password)
    (secret / "allowed").write_text("\n".join(allowed) + "\n")
    return secret


def _fetch(
    tmp_path: Path, sources: list[dict[str, Any]], secret: Path | None
) -> dict[str, Any]:
    work = tmp_path / "work"
    pod.fetch_main({"sources": sources}, work, tmp_path / "cache", secret)
    return json.loads((work / "have.json").read_text())


def test_the_init_container_fetches_a_declared_remote_and_reports_what_it_has(
    tmp_path: Path,
) -> None:
    origin = repo(tmp_path / "origin", {"a.rs": "1"})
    commit(origin, {"a.rs": "2"})
    work = _clone(origin, tmp_path / "clone")
    commit(work, {"a.rs": "local"})
    plan = plan_source(SourceRequest("shop", work, None))
    url = origin.as_uri()
    source = {**plan.describe(), "url": url}
    have = _fetch(tmp_path, [source], _secret(tmp_path, [url]))["sources"]["shop"]
    assert have["fetched"] is True and have["error"] is None
    assert plan.head not in have["present"]  # the local commit is not pushed
    assert plan.candidates[0] in have["present"]  # the upstream tip is


def test_an_undeclared_remote_is_never_fetched(tmp_path: Path) -> None:
    origin = repo(tmp_path / "origin", {"a.rs": "1"})
    source = {
        "name": "shop",
        "key": "shop-x",
        "url": origin.as_uri(),
        "head": git(origin, "rev-parse", "HEAD"),
        "candidates": [],
    }
    have = _fetch(
        tmp_path, [source], _secret(tmp_path, ["https://github.com/org/other"])
    )
    entry = have["sources"]["shop"]
    assert entry["fetched"] is False and "not a declared source" in entry["note"]
    assert _fetch(tmp_path, [source], None)["sources"]["shop"]["fetched"] is False


def test_a_failed_fetch_names_its_cause_with_the_token_masked(tmp_path: Path) -> None:
    url = "https://x-access-token:s3cret@invalid.invalid/org/repo"
    source = {
        "name": "shop",
        "key": "shop-x",
        "url": url,
        "head": "0" * 40,
        "candidates": [],
    }
    entry = _fetch(tmp_path, [source], _secret(tmp_path, [url]))["sources"]["shop"]
    assert entry["fetched"] is False and entry["error"]
    assert "s3cret" not in json.dumps(entry)


def _run_delta(
    tmp_path: Path, work_tree: Path, have: set[str], name: str = "w"
) -> tuple[dict[str, Any], list[str]]:
    plan = plan_source(SourceRequest("shop", work_tree, None))
    packed = pack_delta([plan], {"shop": have}, tmp_path / f"out-{name}")
    pod_work = tmp_path / name
    (pod_work / "upload").mkdir(parents=True)
    shutil.copyfile(packed.path, pod_work / "upload" / "tree.tar.gz")
    (pod_work / "upload" / "ready").touch()
    spec = _spec(
        packed.path.read_bytes(),
        ["sh", "-c", "cat a.rs; cat new.rs; ls"],
        cwd="shop",
        sources=[plan.describe()],
    )
    lines: list[str] = []
    result = pod.main(spec, pod_work, tmp_path / "cache", emit=lines.append)
    return result, lines


def test_the_wrapper_builds_the_working_tree_from_mirror_bundle_diff_and_untracked(
    tmp_path: Path,
) -> None:
    origin = repo(tmp_path / "origin", {"a.rs": "1", "gone.rs": "x"})
    url = origin.as_uri()
    work = _clone(origin, tmp_path / "clone")
    commit(work, {"a.rs": "2"})
    (work / "a.rs").write_text("3, not committed\n")
    (work / "gone.rs").unlink()
    (work / "new.rs").write_text("untracked\n")
    plan = plan_source(SourceRequest("shop", work, None))
    _fetch(tmp_path, [{**plan.describe(), "url": url}], _secret(tmp_path, [url]))
    result, lines = _run_delta(tmp_path, work, {plan.candidates[0]})
    assert result["state"] == "passed", result
    assert "3, not committed" in lines and "untracked" in lines
    assert "gone.rs" not in lines
    assert result["applied"]["shop"] == {"mode": "delta", "bundle": True, "diff": True}


def test_the_next_run_needs_no_bundle_for_commits_the_mirror_got(
    tmp_path: Path,
) -> None:
    work = repo(tmp_path / "work", {"a.rs": "1", "new.rs": "n"})
    first, _ = _run_delta(tmp_path, work, set(), "first")  # seeds the mirror
    assert first["state"] == "passed", first
    plan = plan_source(SourceRequest("shop", work, None))
    have = _fetch(tmp_path, [plan.describe()], None)["sources"]["shop"]
    assert plan.head in have["present"]  # kept from the first run's bundle
    again, _ = _run_delta(tmp_path, work, set(have["present"]), "again")
    assert again["applied"]["shop"]["bundle"] is False


def test_an_old_tree_upload_still_runs(tmp_path: Path) -> None:
    from tests.unit.dev.test_pod import _archive, _run

    data = _archive({"app/a.txt": "one\n"})
    result, lines = _run(tmp_path, data, _spec(data, ["cat", "a.txt"]))
    assert result["state"] == "passed" and "one" in lines


# 0.36.0: a sync into a reused lineage removes the folders it leaves empty
# (a cargo glob member such as ``crates/app/*`` fails on an empty folder).
def _lineage_tree(tmp_path: Path, result: dict[str, Any]) -> Path:
    return tmp_path / "cache" / "lineages" / result["cache"]["lineage"] / "tree"


def _crates(tmp_path: Path) -> Path:
    return repo(
        tmp_path / "work",
        {
            "a.rs": "1",
            "new.rs": "n",
            "crates/app/a/Cargo.toml": "a",
            "crates/app/b/Cargo.toml": "b",
        },
    )


def test_an_in_place_refresh_removes_the_folders_it_leaves_empty(
    tmp_path: Path,
) -> None:
    work = _crates(tmp_path)
    first, _ = _run_delta(tmp_path, work, set(), "first")
    assert first["state"] == "passed", first
    tree = _lineage_tree(tmp_path, first)
    assert (tree / "shop/crates/app/b/Cargo.toml").is_file()
    (tree / "shop/target/debug/deps").mkdir(parents=True)  # a build's output
    shutil.rmtree(work / "crates/app/b")  # uncommitted: the same head
    plan = plan_source(SourceRequest("shop", work, None))
    have = _fetch(tmp_path, [plan.describe()], None)["sources"]["shop"]
    again, _ = _run_delta(tmp_path, work, set(have["present"]), "again")
    assert again["state"] == "passed", again
    assert again["sync"]["mode"] == "in-place"
    assert _lineage_tree(tmp_path, again) == tree
    assert (tree / "shop/crates/app/a/Cargo.toml").is_file()
    assert not (tree / "shop/crates/app/b").exists()
    assert again["sync"]["removed_dirs"] >= 1
    assert (tree / "shop/target/debug/deps").is_dir()
    assert tree.is_dir() and (tree / "shop").is_dir()


def test_an_in_place_refresh_heals_folders_older_versions_left_empty(
    tmp_path: Path,
) -> None:
    work = _crates(tmp_path)
    first, _ = _run_delta(tmp_path, work, set(), "first")
    tree = _lineage_tree(tmp_path, first)
    (tree / "shop/crates/app/old/src").mkdir(parents=True)  # left by 0.35
    plan = plan_source(SourceRequest("shop", work, None))
    have = _fetch(tmp_path, [plan.describe()], None)["sources"]["shop"]
    again, _ = _run_delta(tmp_path, work, set(have["present"]), "again")
    assert again["sync"]["mode"] == "in-place" and again["sync"]["removed"] == 0
    assert not (tree / "shop/crates/app/old").exists()
    assert (tree / "shop/crates/app/b/Cargo.toml").is_file()


def test_a_full_sync_removes_emptied_and_already_empty_folders(
    tmp_path: Path,
) -> None:
    source, dest = tmp_path / "source", tmp_path / "dest"
    (source / "crates/app/a").mkdir(parents=True)
    (source / "crates/app/a/Cargo.toml").write_text("a")
    (dest / "crates/app/a").mkdir(parents=True)
    (dest / "crates/app/a/Cargo.toml").write_text("a")
    (dest / "crates/app/b").mkdir(parents=True)
    (dest / "crates/app/b/Cargo.toml").write_text("b")
    (dest / "crates/app/old/src").mkdir(parents=True)  # already empty
    counts = pod.sync_tree(source, dest)
    assert not (dest / "crates/app/b").exists()
    assert not (dest / "crates/app/old").exists()
    assert (dest / "crates/app/a/Cargo.toml").is_file() and dest.is_dir()
    assert counts["removed"] == 1 and counts["removed_dirs"] == 3
