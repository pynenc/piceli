"""Local Git source repositories for composition tests (bare remotes in tmp_path)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ENV = {
    **os.environ,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


class Repo:
    """A bare remote and a working clone that pushes to it."""

    def __init__(self, root: Path, name: str, files: dict[str, str]) -> None:
        self.remote = root / f"{name}.git"
        self.work = root / f"{name}-work"
        self.remote.mkdir(parents=True)
        git(self.remote, "init", "-q", "--bare", "-b", "main")
        self.work.mkdir()
        git(self.work, "init", "-q", "-b", "main")
        git(self.work, "remote", "add", "origin", str(self.remote))
        self.commit(files, "initial")

    @property
    def url(self) -> str:
        return str(self.remote)

    def commit(
        self, files: dict[str, str], message: str = "change", branch: str = "main"
    ) -> str:
        current = git(self.work, "symbolic-ref", "--short", "HEAD")
        if current != branch:
            exists = git(self.work, "branch", "--list", branch)
            git(self.work, "checkout", "-q", *([] if exists else ["-b"]), branch)
        for path, text in files.items():
            target = self.work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", message)
        git(self.work, "push", "-q", "origin", branch)
        return git(self.work, "rev-parse", "HEAD")

    def tag(self, name: str, branch: str = "main") -> str:
        git(self.work, "tag", name, branch)
        git(self.work, "push", "-q", "origin", name)
        return git(self.work, "rev-parse", branch)

    def delete_branch(self, branch: str) -> None:
        git(self.work, "checkout", "-q", "main")
        git(self.work, "push", "-q", "origin", "--delete", branch)
        git(self.work, "branch", "-q", "-D", branch)


def example_files(directory: Path) -> dict[str, str]:
    """Every file under ``directory`` (an example source repository), as text."""
    return {
        str(path.relative_to(directory)): path.read_text()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def copy_tree(source: Path, target: Path) -> None:
    shutil.copytree(source, target)
