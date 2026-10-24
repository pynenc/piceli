"""The Git operations of a train: worktrees, merges, fast-forwards, pushes.

Every merge is a real ``git merge --no-ff`` in a temporary worktree,
without hooks or signing; a conflict is never resolved, only reported.
Branches only move by fast-forward: a ref is updated with its expected old
value (``git update-ref NEW OLD``), or, when it is checked out in a clean
worktree, by ``git merge --ff-only`` there.

Importing this module is side-effect free.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
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


#: The identity of the train's own merge commits and tags where Git has none
#: (a CI runner or a build host without ``user.name``/``user.email``).
FALLBACK_NAME = "Piceli train"
FALLBACK_EMAIL = "train@piceli.invalid"


@dataclass(frozen=True)
class Merged:
    """The outcome of merging candidates onto a head: the new commit, or the
    branch that failed with its conflicting files (none when Git failed for
    another reason, given in ``error``)."""

    head: str | None
    conflict: str | None = None
    files: tuple[str, ...] = ()
    error: str | None = None


class Git:
    """Git commands in one repository (``git -C repo``)."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        # Worktrees are added and removed one at a time (parallel trials).
        self._worktrees = threading.Lock()
        self._env: dict[str, str] | None = None

    def _environment(self) -> dict[str, str]:
        """The environment of Git commands: an identity for the train's own
        commits when Git has none, so a merge never fails for want of one."""
        if self._env is None:
            env = dict(os.environ)
            known = subprocess.run(
                ["git", "var", "GIT_COMMITTER_IDENT"],
                cwd=self.repo,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT,
                stdin=subprocess.DEVNULL,
                env=env,
            )
            if known.returncode != 0:
                for who in ("AUTHOR", "COMMITTER"):
                    env.setdefault(f"GIT_{who}_NAME", FALLBACK_NAME)
                    env.setdefault(f"GIT_{who}_EMAIL", FALLBACK_EMAIL)
            self._env = env
        return self._env

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
                env=self._environment(),
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
    def worktree(
        self, commit: str, purpose: str = "train", path: Path | None = None
    ) -> Iterator[Path]:
        """A detached worktree at ``commit`` in a temporary directory (or at
        ``path``, a stable place the caller holds), removed (with its Git
        administrative files) when the block exits."""
        if path is not None:
            with self._at(commit, path) as checkout:
                yield checkout
            return
        with temporary_directory(purpose) as scratch:
            checkout = scratch / "checkout"
            with self._worktrees:
                self.run(
                    "worktree", "add", "--detach", "--quiet", str(checkout), commit
                )
            try:
                yield checkout
            finally:
                with self._worktrees:
                    self.run(
                        "worktree", "remove", "--force", str(checkout), check=False
                    )
                    self.run("worktree", "prune", check=False)

    @contextmanager
    def _at(self, commit: str, checkout: Path) -> Iterator[Path]:
        checkout.parent.mkdir(parents=True, exist_ok=True)
        with self._worktrees:
            self._drop(checkout)
            self.run("worktree", "add", "--detach", "--quiet", str(checkout), commit)
        try:
            yield checkout
        finally:
            with self._worktrees:
                self._drop(checkout)

    def _drop(self, checkout: Path) -> None:
        """Remove a worktree at ``checkout`` (and what a crash left there)."""
        self.run("worktree", "remove", "--force", str(checkout), check=False)
        shutil.rmtree(checkout, ignore_errors=True)
        self.run("worktree", "prune", check=False)

    def merged(
        self, head: str, candidates: Sequence[tuple[str, str]], train: str
    ) -> Merged:
        """The commit of ``candidates`` merged onto ``head`` (made in a
        temporary worktree, which goes; the commit stays in the object store)."""
        with self.worktree(head) as checkout:
            return self.merge(checkout, candidates, train)

    def set_ref(self, ref: str, commit: str) -> None:
        """Point ``ref`` (a full name outside ``refs/heads``) at ``commit``."""
        self.run("update-ref", ref, commit)

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
                error = None if files else (done.stderr or done.stdout).strip()[-500:]
                return Merged(None, branch, tuple(files), error or None)
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

    def has_changes(self, checkout: Path) -> bool:
        """Any change in ``checkout``: staged, unstaged or untracked."""
        return bool(self.out("status", "--porcelain", cwd=checkout))

    def rename_branch(self, old: str, new: str) -> None:
        """Rename a local branch (a worktree that has it checked out follows)."""
        self.run("branch", "-m", old, new)

    def remote_tip(self, remote: str, branch: str) -> str | None:
        """The commit of ``branch`` on ``remote`` (``None``: not there).
        Raises :class:`TrainError` when the remote cannot be read."""
        out = self.out("ls-remote", "--heads", remote, f"refs/heads/{branch}")
        return out.split()[0] if out else None

    def delete_remote(self, remote: str, branch: str, expected: str) -> bool:
        """Delete ``branch`` on ``remote`` only while it is at ``expected``
        (``--force-with-lease``); whether it went."""
        done = self.run(
            "push",
            "--porcelain",
            f"--force-with-lease=refs/heads/{branch}:{expected}",
            remote,
            f":refs/heads/{branch}",
            check=False,
        )
        return done.returncode == 0

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
