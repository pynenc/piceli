"""Packing what a development run builds: a commit, plus sibling sources."""

from __future__ import annotations

import hashlib
import subprocess
import tarfile
from pathlib import Path

import pytest

from piceli.dev.model import DevError
from piceli.dev.pack import SourceRequest, pack, parse_source


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "dev@example.com")
    git(path, "config", "user.name", "Dev")
    commit(path, files)
    return path


def commit(path: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "change")
    return git(path, "rev-parse", "HEAD")


def names(path: Path) -> dict[str, bytes]:
    with tarfile.open(path) as tar:
        return {
            m.name: tar.extractfile(m).read()  # type: ignore[union-attr]
            for m in tar.getmembers()
            if m.isfile()
        }


def test_a_ref_packs_that_commit_under_the_source_name(tmp_path: Path) -> None:
    app = repo(tmp_path / "shop", {"src/a.rs": "one", "Cargo.toml": "[package]"})
    first = git(app, "rev-parse", "HEAD")
    commit(app, {"src/a.rs": "two"})
    (app / "src" / "a.rs").write_text("uncommitted")  # a --ref ignores the tree
    packed = pack(SourceRequest("shop", app, first[:9]), [], tmp_path / "out")
    files = names(packed.path)
    assert files["shop/src/a.rs"] == b"one" and "shop/Cargo.toml" in files
    assert packed.sources["shop"] == {"commit": first, "ref": first[:9], "dirty": False}
    assert packed.sha256 == hashlib.sha256(packed.path.read_bytes()).hexdigest()


def test_sibling_sources_sit_next_to_the_root(tmp_path: Path) -> None:
    app = repo(tmp_path / "product", {"Cargo.toml": 'muse = { path = "../ih-lib" }'})
    lib = repo(tmp_path / "lib", {"lib.rs": "v1"})
    pinned = git(lib, "rev-parse", "HEAD")
    commit(lib, {"lib.rs": "v2"})
    packed = pack(
        SourceRequest("product", app, "HEAD"),
        [parse_source(f"ih-lib={lib}@{pinned}")],
        tmp_path / "out",
    )
    files = names(packed.path)
    assert files["ih-lib/lib.rs"] == b"v1"
    assert packed.sources["ih-lib"]["commit"] == pinned


def test_an_unknown_ref_is_refused(tmp_path: Path) -> None:
    app = repo(tmp_path / "shop", {"a": "x"})
    with pytest.raises(DevError) as raised:
        pack(SourceRequest("shop", app, "no-such-branch"), [], tmp_path / "out")
    assert raised.value.code == "dev-ref-unknown"


@pytest.mark.parametrize(
    "text", ["=x", "../x=y", "name", "Bad Name=/tmp", "a=b@", "two words=/x@HEAD"]
)
def test_bad_source_requests_are_refused(text: str) -> None:
    with pytest.raises(DevError) as raised:
        parse_source(text)
    assert raised.value.code == "dev-source-invalid"


def test_a_path_that_is_no_repository_is_refused(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    with pytest.raises(DevError) as raised:
        pack(SourceRequest("plain", tmp_path / "plain", "HEAD"), [], tmp_path / "out")
    assert raised.value.code == "dev-source-invalid"
