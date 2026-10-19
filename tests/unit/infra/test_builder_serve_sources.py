"""``builder serve`` fetches private sources and checks them out with their Git
identity (0.21.1)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from piceli.infra.builder_serve import BuildFailed, export_source, fetch_env, fetch_urls

URL = "https://git.example.invalid/org/product.git"
SSH = "git@git.example.invalid:org/product.git"


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@x", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "product"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "kept.rs").write_text("fn kept() {}\n")
    (path / "changed.rs").write_text("fn v1() {}\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "one")
    return path


def _ssh_only(repo: Path, calls: list[list[str]]) -> Any:
    """``git`` where HTTPS has no credential and SSH reaches ``repo``."""

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        if "fetch" in argv and URL in argv:
            return subprocess.CompletedProcess(
                argv, 128, b"", b"fatal: could not read Username"
            )
        return subprocess.run([str(repo) if a == SSH else a for a in argv], **kwargs)

    return run


def test_an_https_url_falls_back_to_the_same_repository_over_ssh() -> None:
    assert fetch_urls(URL) == [URL, SSH]
    assert fetch_urls("https://token@github.com/org/x") == [
        "https://token@github.com/org/x",
        "git@github.com:org/x",
    ]
    assert fetch_urls("git@github.com:org/x.git") == ["git@github.com:org/x.git"]
    assert fetch_urls("/srv/git/x.git") == ["/srv/git/x.git"]


def test_fetches_never_prompt_and_keep_the_users_ssh_command() -> None:
    env = fetch_env({"HOME": "/h"})
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
    assert fetch_env({"GIT_SSH_COMMAND": "ssh -i k"})["GIT_SSH_COMMAND"] == "ssh -i k"


def test_a_private_source_is_checked_out_over_ssh_with_its_identity(
    repo: Path, tmp_path: Path
) -> None:
    commit = _git(repo, "rev-parse", "HEAD")
    calls: list[list[str]] = []
    into = tmp_path / "builder" / "checkouts" / "product"
    export_source(
        URL, commit, into, tmp_path / "builder" / "git", _ssh_only(repo, calls)
    )
    assert any(SSH in argv for argv in calls)
    assert _git(into, "rev-parse", "HEAD") == commit
    assert _git(into, "status", "--porcelain") == ""
    assert _git(into, "config", "--get", "remote.origin.url") == URL
    assert (into / "kept.rs").read_text() == "fn kept() {}\n"
    assert all(os.stat(p).st_mode & 0o004 for p in into.rglob("*"))  # others read


def test_the_next_commit_keeps_unchanged_files_times(
    repo: Path, tmp_path: Path
) -> None:
    """Incremental builds (cargo) compare file times: only what changed moves."""
    mirrors, into = tmp_path / "git", tmp_path / "checkout"
    run = _ssh_only(repo, [])
    export_source(URL, _git(repo, "rev-parse", "HEAD"), into, mirrors, run)
    old = 1_000_000_000
    for name in ("kept.rs", "changed.rs"):
        os.utime(into / name, (old, old))
    (repo / "changed.rs").write_text("fn v2() {}\n")
    _git(repo, "commit", "-qam", "two")
    second = _git(repo, "rev-parse", "HEAD")
    export_source(URL, second, into, mirrors, run)
    assert _git(into, "rev-parse", "HEAD") == second
    assert (into / "changed.rs").read_text() == "fn v2() {}\n"
    assert os.stat(into / "kept.rs").st_mtime == old
    assert os.stat(into / "changed.rs").st_mtime > old
    # One commit in the checkout: earlier objects are gone.
    assert _git(into, "rev-list", "--count", "HEAD") == "1"


def test_an_unreachable_source_names_both_transports(
    repo: Path, tmp_path: Path
) -> None:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if "fetch" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"Permission denied")
        return subprocess.run(argv, **kwargs)

    with pytest.raises(BuildFailed, match=r"over HTTPS or SSH.*Permission denied"):
        export_source(URL, "a" * 40, tmp_path / "c", tmp_path / "git", run)
