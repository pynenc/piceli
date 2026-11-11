"""0.71.0: a tree sync into a reused lineage leaves no emptied folder behind
(a cargo glob member such as ``crates/x/*`` fails on one)."""

from __future__ import annotations

from pathlib import Path

from piceli.dev import pod


def _tree(root: Path, files: dict[str, str], empty: tuple[str, ...] = ()) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    for name in empty:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def test_a_folder_the_next_tree_lacks_is_gone_after_the_sync(tmp_path: Path) -> None:
    dest = tmp_path / "lineage" / "tree"
    with_x = _tree(
        tmp_path / "a",
        {"Cargo.toml": "ws", "crates/x/foo/Cargo.toml": "foo", "x/a.rs": "a"},
    )
    pod.sync_tree(with_x, dest)
    assert (dest / "crates/x/foo/Cargo.toml").is_file()
    without_x = _tree(tmp_path / "b", {"Cargo.toml": "ws"})
    counts = pod.sync_tree(without_x, dest)
    assert not (dest / "x").exists()
    assert not (dest / "crates").exists()
    assert (dest / "Cargo.toml").is_file()
    assert counts["removed"] == 2


def test_a_folder_the_source_holds_empty_is_not_kept(tmp_path: Path) -> None:
    # Git tracks no empty folder: one in an upload is a branch switch's leftover.
    dest = tmp_path / "lineage" / "tree"
    pod.sync_tree(_tree(tmp_path / "a", {"crates/x/foo/Cargo.toml": "foo"}), dest)
    source = _tree(tmp_path / "b", {"crates/x/bar/Cargo.toml": "b"}, ("crates/x/foo",))
    pod.sync_tree(source, dest)
    assert not (dest / "crates/x/foo").exists()
    assert (dest / "crates/x/bar/Cargo.toml").is_file()


def test_nested_emptied_folders_go_bottom_up_and_the_root_stays(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "lineage" / "tree"
    pod.sync_tree(_tree(tmp_path / "a", {"a/b/c/file.rs": "x"}), dest)
    counts = pod.sync_tree(_tree(tmp_path / "b", {}, ("a/b/c",)), dest)
    assert not (dest / "a").exists()
    assert dest.is_dir()
    assert counts["removed"] == 1 and counts["removed_dirs"] >= 3


def test_empty_target_folders_are_never_touched(tmp_path: Path) -> None:
    dest = tmp_path / "lineage" / "tree"
    source = _tree(
        tmp_path / "a", {"crates/x/foo/Cargo.toml": "foo"}, ("target", "app/target/d")
    )
    pod.sync_tree(source, dest)
    assert (dest / "target").is_dir()
    assert (dest / "app/target/d").is_dir()
    assert (dest / "crates/x/foo/Cargo.toml").is_file()
