"""The controller's view of the Git remote: ``git ls-remote``, a mirror, checkouts.

Git runs as a subprocess with no terminal prompt. Credentials come only from
a directory of files (the mounted Secret): ``username`` + ``password`` for
HTTPS (handed to Git by a credential helper that reads the files, so the
values never appear in a command line or the environment), or
``ssh-privatekey`` (+ ``known_hosts``) for SSH. Git's own output is never
printed or recorded: a failure is ``gitops-git-failed`` with the exit code.

Importing this module is side-effect free.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from piceli.gitops import GitOpsError

Runner = Callable[..., subprocess.CompletedProcess[bytes]]
GIT_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class RemoteRefs:
    """Branch heads and tag commits of the remote (annotated tags peeled)."""

    branches: Mapping[str, str] = field(default_factory=dict)
    tags: Mapping[str, str] = field(default_factory=dict)


def parse_ls_remote(text: str) -> RemoteRefs:
    """Parse ``git ls-remote --heads --tags`` output."""
    branches: dict[str, str] = {}
    tags: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in text.splitlines():
        sha, _, ref = line.partition("\t")
        sha, ref = sha.strip(), ref.strip()
        if len(sha) < 40:
            continue
        if ref.startswith("refs/heads/"):
            branches[ref[len("refs/heads/") :]] = sha
        elif ref.startswith("refs/tags/"):
            name = ref[len("refs/tags/") :]
            if name.endswith("^{}"):
                peeled[name[:-3]] = sha
            else:
                tags[name] = sha
    tags.update(peeled)  # an annotated tag deploys the commit it points at
    return RemoteRefs(branches, tags)


class GitRemote:
    """One remote, mirrored under ``mirror_dir``.

    :param url: The repository URL (validated: no credentials in it).
    :param mirror_dir: Where the bare mirror lives (the controller's volume).
    :param credentials_dir: The mounted Secret, or ``None`` for a public or
        local repository.
    :param runner: ``subprocess.run`` (replaced in tests).
    """

    def __init__(
        self,
        url: str,
        mirror_dir: Path,
        *,
        credentials_dir: Path | None = None,
        runner: Runner = subprocess.run,
    ) -> None:
        self.url = url
        self.mirror_dir = mirror_dir
        self.credentials_dir = credentials_dir
        self.runner = runner

    # ------------------------------------------------------------ plumbing
    @contextmanager
    def _auth(self) -> Iterator[tuple[list[str], dict[str, str]]]:
        """Git ``-c`` options and environment for the credentials, if any."""
        options: list[str] = []
        env = {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "true",
            "SSH_ASKPASS": "true",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        directory = self.credentials_dir
        if directory is None or not directory.is_dir():
            env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
            yield options, env
            return
        if (directory / "password").is_file():
            user = shlex.quote(str(directory / "username"))
            password = shlex.quote(str(directory / "password"))
            helper = (
                '!f() { test "$1" = get || exit 0; '
                f"test -f {user} && printf 'username=%s\\n' \"$(cat {user})\"; "
                f"printf 'password=%s\\n' \"$(cat {password})\"; }}; f"
            )
            options += ["-c", "credential.helper=", "-c", f"credential.helper={helper}"]
        with tempfile.TemporaryDirectory(prefix="piceli-git-") as private:
            key = directory / "ssh-privatekey"
            if key.is_file():
                # ssh refuses a key readable by others; a Secret volume with
                # fsGroup is group-readable, so use a private copy.
                copy = Path(private) / "id"
                shutil.copyfile(key, copy)
                copy.chmod(0o600)
                known = directory / "known_hosts"
                ssh = ["ssh", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes"]
                ssh += ["-i", str(copy)]
                if known.is_file():
                    ssh += ["-o", f"UserKnownHostsFile={known}"]
                    ssh += ["-o", "StrictHostKeyChecking=yes"]
                env["GIT_SSH_COMMAND"] = shlex.join(ssh)
            else:
                env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
            yield options, env

    def _git(self, *args: str, what: str, cwd: Path | None = None) -> bytes:
        with self._auth() as (options, env):
            try:
                result = self.runner(
                    ["git", *options, *args],
                    cwd=cwd,
                    env={**os.environ, **env},
                    capture_output=True,
                    timeout=GIT_TIMEOUT_SECONDS,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise GitOpsError(
                    "gitops-git-failed", f"git {what} could not run or timed out"
                ) from None
        if result.returncode != 0:
            # Never Git's stderr: it can echo URLs and server messages.
            raise GitOpsError(
                "gitops-git-failed", f"git {what} failed (exit {result.returncode})"
            )
        return result.stdout

    # ------------------------------------------------------------ commands
    def ls_remote(self) -> RemoteRefs:
        """Branch heads and tags of the remote, without fetching objects."""
        out = self._git(
            "ls-remote", "--heads", "--tags", "--", self.url, what="ls-remote"
        )
        return parse_ls_remote(out.decode("utf-8", "replace"))

    def fetch(self) -> None:
        """Create or update the bare mirror (all branches and tags)."""
        if not (self.mirror_dir / "HEAD").is_file():
            self.mirror_dir.parent.mkdir(parents=True, exist_ok=True)
            if self.mirror_dir.exists():
                shutil.rmtree(self.mirror_dir)
            self._git(
                "clone",
                "--mirror",
                "--quiet",
                "--",
                self.url,
                str(self.mirror_dir),
                what="clone",
            )
            return
        self._git(
            "remote", "set-url", "origin", self.url, what="remote", cwd=self.mirror_dir
        )
        self._git(
            "fetch", "--prune", "--quiet", "origin", what="fetch", cwd=self.mirror_dir
        )

    @contextmanager
    def checkout(self, commit: str, parent: Path) -> Iterator[Path]:
        """A clean working tree of ``commit``, removed when the block exits."""
        if not all(ch in "0123456789abcdef" for ch in commit) or len(commit) < 7:
            raise GitOpsError("gitops-git-failed", "invalid commit id")
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="checkout-", dir=parent) as work:
            tree = Path(work) / "src"
            self._git(
                "clone", "--quiet", "--shared", "--no-checkout",
                str(self.mirror_dir), str(tree), what="clone",
            )  # fmt: skip
            self._git(
                "checkout", "--quiet", "--detach", commit, what="checkout", cwd=tree
            )
            yield tree


def describe(refs: RemoteRefs) -> dict[str, Any]:
    """A JSON-friendly summary (counts only)."""
    return {"branches": len(refs.branches), "tags": len(refs.tags)}
