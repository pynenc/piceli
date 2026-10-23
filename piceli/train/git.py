"""The Git operations of a train: worktrees, merges, fast-forwards, pushes.

Every merge is a real ``git merge --no-ff`` in a temporary worktree,
without hooks or signing; a conflict is never resolved, only reported.
Branches only move by fast-forward: a ref is updated with its expected old
value (``git update-ref NEW OLD``), or, when it is checked out in a clean
worktree, by ``git merge --ff-only`` there.

Importing this module is side-effect free.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from piceli.tempfiles import temporary_directory
from piceli.train.model import TrainError

#: Seconds a Git command may take (a hang guard; merges of large trees fit).
GIT_TIMEOUT = 600.0
_ZERO = "0" * 40

#: Options of every Git command that commits: no hooks, no signing, no editor.
_QUIET = (
    "-c",
    "commit.gpgSign=false",
    "-c",
    "tag.gpgSign=false",
    "-c",
    "core.editor=true",
    "-c",
    "core.hooksPath=/dev/null",
)


@dataclass(frozen=True)
class Merged:
    """The outcome of merging candidates onto a head: the new commit, or the
    branch that conflicted and its conflicting files."""

    head: str | None
    conflict: str | None = None
    files: tuple[str, ...] = ()


class Git:
    """Git commands in one repository (``git -C repo``)."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def run(
        self, *args: str, cwd: Path | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        try:
            done = subprocess.run(
                ["git", *_QUIET, *args],
                cwd=cwd or self.repo,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise TrainError("train-git-failed", f"git {args[0]} timed out") from None
        if check and done.returncode != 0:
            raise TrainError(
                "train-git-failed",
                f"git {' '.join(args[:2])} failed: {done.stderr.strip()[-300:]}",
            )
        return done

    def out(self, *args: str, cwd: Path | None = None) -> str:
        return self.run(*args, cwd=cwd).stdout.strip()

    def commit(self, rev: str) -> str | None:
        """The commit ``rev`` names, or ``None``."""
        done = self.run("rev-parse", "-q", "--verify", f"{rev}^{{commit}}", check=False)
        if done.returncode != 0:
            return None
        return done.stdout.strip() or None

    def ref(self, name: str) -> str | None:
        """The commit of ``refs/heads/name`` (``refs/tags/…`` with a full name)."""
        full = name if name.startswith("refs/") else f"refs/heads/{name}"
        return self.commit(full)

    def is_ancestor(self, older: str, newer: str) -> bool:
        done = self.run("merge-base", "--is-ancestor", older, newer, check=False)
        return done.returncode == 0

    def toplevel(self) -> Path:
        return Path(self.out("rev-parse", "--show-toplevel")).resolve()

    def tags(self, pattern: str) -> list[str]:
        return self.out("tag", "--list", pattern).splitlines()

    @contextmanager
    def worktree(self, commit: str, purpose: str = "train") -> Iterator[Path]:
        """A detached worktree at ``commit`` in a temporary directory, removed
        (with its Git administrative files) when the block exits."""
        with temporary_directory(purpose) as scratch:
            checkout = scratch / "checkout"
            self.run("worktree", "add", "--detach", "--quiet", str(checkout), commit)
            try:
                yield checkout
            finally:
                self.run("worktree", "remove", "--force", str(checkout), check=False)
                self.run("worktree", "prune", check=False)

    def merge(
        self, checkout: Path, candidates: Sequence[tuple[str, str]], train: str
    ) -> Merged:
        """Merge each ``(branch, commit)`` in order (``--no-ff``) in ``checkout``."""
        for branch, commit in candidates:
            done = self.run(
                "merge",
                "--no-ff",
                "--no-verify",
                "--no-edit",
                "-q",
                "-m",
                f"Merge branch '{branch}' (train {train})",
                commit,
                cwd=checkout,
                check=False,
            )
            if done.returncode != 0:
                files = self.out(
                    "diff", "--name-only", "--diff-filter=U", cwd=checkout
                ).splitlines()
                self.run("merge", "--abort", cwd=checkout, check=False)
                return Merged(None, branch, tuple(files))
        return Merged(self.out("rev-parse", "HEAD", cwd=checkout))

    def changed(self, base: str, head: str) -> list[str]:
        """Paths that differ between two commits."""
        return [
            line
            for line in self.out(
                "diff", "--name-only", "--no-renames", base, head
            ).splitlines()
            if line
        ]

    def checked_out(self, branch: str) -> Path | None:
        """The worktree that has ``branch`` checked out, if any."""
        current: Path | None = None
        for line in self.out("worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                current = Path(line[len("worktree ") :])
            elif line == f"branch refs/heads/{branch}":
                return current
        return None

    def dirty(self, checkout: Path) -> bool:
        return bool(
            self.out("status", "--porcelain", "--untracked-files=no", cwd=checkout)
        )

    def fast_forward(self, branch: str, old: str | None, new: str) -> None:
        """Move ``branch`` from ``old`` (``None``: create it) to ``new``."""
        checkout = self.checked_out(branch)
        if checkout is not None:
            self.run("merge", "--ff-only", "-q", new, cwd=checkout)
            return
        self.run("update-ref", f"refs/heads/{branch}", new, old or _ZERO)

    def tag(self, name: str, commit: str, message: str) -> None:
        self.run("tag", "-a", name, "-m", message, commit)

    def push(
        self, remote: str, refs: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        return self.run("push", "--atomic", "--porcelain", remote, *refs, check=False)
