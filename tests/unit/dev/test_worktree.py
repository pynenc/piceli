"""``--worktree``: the tree as it is on disk, without Git's ignored files."""

from __future__ import annotations

import os
from pathlib import Path

from piceli.dev.pack import SourceRequest, pack, parse_source
from tests.unit.dev.test_pack import git, names, repo


def test_the_worktree_ships_committed_changed_and_new_files(tmp_path: Path) -> None:
    app = repo(
        tmp_path / "shop",
        {".gitignore": "target/\n*.log\n", "a.rs": "committed", "b.rs": "gone soon"},
    )
    (app / "a.rs").write_text("changed, not committed")
    (app / "new.rs").write_text("untracked")
    (app / "b.rs").unlink()  # deleted, not committed
    (app / "target").mkdir()
    (app / "target" / "big.o").write_text("ignored")
    (app / "debug.log").write_text("ignored")
    os.symlink("a.rs", app / "link.rs")
    packed = pack(SourceRequest("shop", app, None), [], tmp_path / "out")
    files = names(packed.path)
    assert files["shop/a.rs"] == b"changed, not committed"
    assert files["shop/new.rs"] == b"untracked"
    assert "shop/b.rs" not in files
    assert not any("target" in name or name.endswith(".log") for name in files)
    source = packed.sources["shop"]
    assert source["dirty"] is True and source["ref"] is None
    assert source["commit"] == git(app, "rev-parse", "HEAD")
    assert source["files"] == 4  # .gitignore, a.rs, new.rs, link.rs
    import tarfile

    with tarfile.open(packed.path) as tar:
        assert tar.getmember("shop/link.rs").issym()


def test_a_clean_worktree_is_not_dirty(tmp_path: Path) -> None:
    app = repo(tmp_path / "shop", {"a.rs": "x"})
    packed = pack(SourceRequest("shop", app, None), [], tmp_path / "out")
    assert packed.sources["shop"]["dirty"] is False


def test_a_sibling_without_ref_ships_its_worktree(tmp_path: Path) -> None:
    app = repo(tmp_path / "product", {"a": "x"})
    lib = repo(tmp_path / "lib", {"lib.rs": "v1"})
    (lib / "lib.rs").write_text("work in progress")
    packed = pack(
        SourceRequest("product", app, "HEAD"),
        [parse_source(f"lib={lib}")],
        tmp_path / "out",
    )
    assert names(packed.path)["lib/lib.rs"] == b"work in progress"
