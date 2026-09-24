"""Minimal-by-construction build contexts: selection, budgets and staging."""

import os
from pathlib import Path

import pytest

from piceli.artifacts.build_context import (
    BuildContextError,
    ContextSelection,
    Glob,
    stage_context,
    verify_staged,
)


def tree(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def workspace(tmp_path: Path) -> Path:
    return tree(
        tmp_path / "ws",
        {
            "Cargo.toml": "[package]\n",
            "Cargo.lock": "version = 4\n",
            "src/main.rs": "fn main() {}\n",
            "src/net/mod.rs": "// net\n",
            "src/notes.md": "not rust\n",
            "target/release/app": "binary",
            "target/debug/deps/x.rs": "// generated\n",
            ".venv/lib/site.py": "venv\n",
            "node_modules/pkg/index.rs": "// vendored\n",
            ".git/config": "[core]\n",
            ".env": "TOKEN=secret\n",
            "src/server.key": "private\n",
            "README.md": "readme\n",
        },
    )


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("src/**/*.rs", "src/main.rs", True),
        ("src/**/*.rs", "src/net/mod.rs", True),
        ("src/**/*.rs", "src/notes.md", False),
        ("*.toml", "Cargo.toml", True),
        ("*.toml", "crates/a/Cargo.toml", False),
        ("**/Cargo.toml", "crates/a/Cargo.toml", True),
        ("**/Cargo.toml", "Cargo.toml", True),
        ("src/**", "src/net/mod.rs", True),
        ("src/?ain.rs", "src/main.rs", True),
    ],
)
def test_glob_semantics(pattern: str, path: str, expected: bool) -> None:
    assert Glob(pattern).matches(path) is expected


@pytest.mark.parametrize(
    "pattern", ["/abs", "../up", "a//b", "./a", "a/**b", "a[0]", "", "a\\b"]
)
def test_invalid_patterns_rejected(pattern: str) -> None:
    with pytest.raises(BuildContextError):
        ContextSelection((pattern,))


def test_scan_selects_only_declared_files_and_prunes_heavy_dirs(
    tmp_path: Path,
) -> None:
    root = workspace(tmp_path)
    manifest = ContextSelection(
        ("Cargo.toml", "Cargo.lock", "**/*.rs", "**/*.key", ".env")
    ).scan(root)
    paths = [item.path for item in manifest.files]
    # target/, node_modules/, .venv/ and .git/ are never entered via "**".
    assert paths == ["Cargo.lock", "Cargo.toml", "src/main.rs", "src/net/mod.rs"]
    assert manifest.pruned_directories >= 3
    # Private files are never staged, even when a pattern matches them.
    assert manifest.skipped_private == 2
    assert manifest.summary()["bytes"] == manifest.total_bytes


def test_heavy_dir_entered_only_when_named_literally(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    manifest = ContextSelection(("target/release/app",)).scan(root)
    assert [item.path for item in manifest.files] == ["target/release/app"]


def test_manifest_digest_is_content_bound(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    selection = ContextSelection(("src/**/*.rs",))
    first = selection.scan(root)
    assert selection.scan(root).sha256 == first.sha256
    (root / "src/main.rs").write_text("fn main() { println!(); }\n")
    assert selection.scan(root).sha256 != first.sha256


def test_budgets_enforced(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    with pytest.raises(BuildContextError) as files:
        ContextSelection(("src/**/*.rs",), max_files=1).scan(root)
    assert files.value.code == "context-budget-exceeded"
    with pytest.raises(BuildContextError) as size:
        ContextSelection(("src/**/*.rs",), max_bytes=5).scan(root)
    assert size.value.code == "context-budget-exceeded"


def test_empty_and_missing_contexts(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    with pytest.raises(BuildContextError) as empty:
        ContextSelection(("nothing/*.x",)).scan(root)
    assert empty.value.code == "context-empty"
    with pytest.raises(BuildContextError) as missing:
        ContextSelection(("*",)).scan(tmp_path / "absent")
    assert missing.value.code == "context-missing"


def test_matching_symlink_is_an_error_other_links_are_skipped(
    tmp_path: Path,
) -> None:
    root = workspace(tmp_path)
    (root / "src/link.rs").symlink_to(root / "src/main.rs")
    with pytest.raises(BuildContextError) as error:
        ContextSelection(("src/*.rs",)).scan(root)
    assert error.value.code == "context-symlink"
    os.remove(root / "src/link.rs")
    (root / "linked").symlink_to(root / "src", target_is_directory=True)
    manifest = ContextSelection(("**/*.rs",)).scan(root)
    assert manifest.skipped_symlinks == 1
    assert all(not item.path.startswith("linked") for item in manifest.files)


def test_exclude_patterns(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    manifest = ContextSelection(("src/**/*.rs",), ("src/net/**",)).scan(root)
    assert [item.path for item in manifest.files] == ["src/main.rs"]


def test_stage_copies_exactly_the_manifest_with_fixed_times(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    (root / "src/main.rs").chmod(0o755)
    manifest = ContextSelection(("Cargo.toml", "src/**/*.rs")).scan(root)
    staged = tmp_path / "staged"
    stage_context(root, manifest, staged, mtime=7)
    found = sorted(
        path.relative_to(staged).as_posix()
        for path in staged.rglob("*")
        if path.is_file()
    )
    assert found == ["Cargo.toml", "src/main.rs", "src/net/mod.rs"]
    assert os.stat(staged / "src/main.rs").st_mode & 0o777 == 0o755
    assert os.stat(staged / "Cargo.toml").st_mode & 0o777 == 0o644
    assert os.stat(staged / "src/net/mod.rs").st_mtime == 7
    assert os.stat(staged / "src/net").st_mtime == 7


def test_stage_detects_change_after_scan(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    manifest = ContextSelection(("src/**/*.rs",)).scan(root)
    (root / "src/main.rs").write_text("changed\n")
    with pytest.raises(BuildContextError) as error:
        stage_context(root, manifest, tmp_path / "staged")
    assert error.value.code == "context-changed"


def test_stage_refuses_symlink_swapped_in(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    manifest = ContextSelection(("src/**/*.rs",)).scan(root)
    os.remove(root / "src/main.rs")
    (root / "src/main.rs").symlink_to(root / "README.md")
    with pytest.raises(BuildContextError) as error:
        stage_context(root, manifest, tmp_path / "staged")
    assert error.value.code == "context-changed"


def test_verify_staged_checks_only_the_staged_files(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    manifest = ContextSelection(("src/**/*.rs",)).scan(root)
    # Unstaged files (even new ones matching the patterns) are not drift.
    (root / "README.md").write_text("edited\n")
    (root / "src/notes.md").write_text("edited\n")
    (root / "src/new.rs").write_text("// added after the scan\n")
    verify_staged(root, manifest)
    changes = {
        "content": lambda base: (base / "src/net/mod.rs").write_text("// changed\n"),
        "mode": lambda base: (base / "src/main.rs").chmod(0o755),
        "deleted": lambda base: (base / "src/main.rs").unlink(),
    }
    for name, change in changes.items():
        fresh = workspace(tmp_path / name)
        before = ContextSelection(("src/**/*.rs",)).scan(fresh)
        change(fresh)
        with pytest.raises(BuildContextError) as error:
            verify_staged(fresh, before)
        assert error.value.code == "context-changed"
