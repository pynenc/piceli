"""Every source of a composition, through one set of Git credentials.

:class:`SourceSet` keeps one bare mirror per :class:`~piceli.infra.Source`
(``<state_dir>/sources/<name>``) and answers, without a working tree:

- the remote's branch heads and tags (``git ls-remote``);
- the ``piceli.toml`` of a commit (``git show <commit>:piceli.toml``);
- the Git object id of a path at a commit (``git rev-parse <commit>:<path>``),
  which is what a component's source digest is made of: a commit that leaves
  the paths a build reads untouched leaves the digest unchanged;
- the files below a directory at a commit with their blob ids
  (``git ls-tree -r``): what a pipeline image's change key is made of.

Git runs as :class:`piceli.gitops.repo.GitRemote` runs it: no prompt, the
credentials only from the mounted Secret directory (one for every source),
and never Git's output in a message.

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from piceli.gitops import GitOpsError
from piceli.gitops.repo import GitRemote, RemoteRefs
from piceli.infra import CompositionError, Source
from piceli.infra.contract import (
    CONTRACT_FILE,
    ComponentContract,
    contract_digest,
    parse_contracts,
)

_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_OBJECT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


class SourceSet:
    """The mirrors of every source.

    :param sources: The composition's sources.
    :param mirror_root: Where the mirrors live (the controller's volume).
    :param credentials_dir: The one mounted Git Secret (or ``None``).
    :param runner: ``subprocess.run`` (tests pass a fake).
    """

    def __init__(
        self,
        sources: Sequence[Source],
        mirror_root: Path,
        *,
        credentials_dir: Path | None = None,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        options: dict[str, Any] = {"credentials_dir": credentials_dir}
        if runner is not None:
            options["runner"] = runner
        self._root = mirror_root
        self._options = options
        self.sources = {source.key: source for source in sources}
        self.remotes = {
            key: GitRemote(source.url, mirror_root / key, **options)
            for key, source in self.sources.items()
        }
        self._fetched: set[str] = set()

    def add(self, source: Source) -> None:
        """Follow one more source (a composition imported later names it)."""
        known = self.sources.get(source.key)
        if known is not None and known.url == source.url:
            return
        self.sources[source.key] = source
        self.remotes[source.key] = GitRemote(
            source.url, self._root / source.key, **self._options
        )
        self._fetched.discard(source.key)

    def ls_remote(self, key: str) -> RemoteRefs:
        return self.remotes[key].ls_remote()

    def fetch(self, key: str) -> None:
        """Update one mirror (once per poll: :meth:`new_poll` resets it)."""
        if key in self._fetched:
            return
        self.remotes[key].fetch()
        self._fetched.add(key)

    def new_poll(self) -> None:
        self._fetched.clear()

    def _git(self, key: str, *args: str, what: str) -> bytes:
        remote = self.remotes[key]
        return remote._git(*args, what=what, cwd=remote.mirror_dir)

    def object_id(self, key: str, commit: str, path: str) -> str | None:
        """The Git object id of ``path`` at ``commit`` (``None``: absent)."""
        if not _COMMIT.fullmatch(commit):
            raise GitOpsError("gitops-git-failed", "invalid commit id")
        spec = f"{commit}:{path}" if path not in ("", ".") else f"{commit}^{{tree}}"
        try:
            out = self._git(
                key, "rev-parse", "--verify", "--quiet", spec, what="rev-parse"
            )
        except GitOpsError:
            return None
        found = out.decode().strip()
        return found if _OBJECT.fullmatch(found) else None

    def tree(self, key: str, commit: str, prefix: str) -> list[tuple[str, str, str]]:
        """``(path relative to prefix, mode, object id)`` of every file below ``prefix``.

        Submodules are skipped; symbolic links are listed (mode ``120000``).
        An absent ``prefix`` lists nothing.
        """
        if not _COMMIT.fullmatch(commit):
            raise GitOpsError("gitops-git-failed", "invalid commit id")
        prefix = prefix.strip("/")
        if prefix in ("", "."):
            prefix = ""
        args = ["ls-tree", "-r", "-z", "--full-tree", commit]
        if prefix:
            args += ["--", prefix + "/"]
        out = self._git(key, *args, what="ls-tree")
        found: list[tuple[str, str, str]] = []
        for record in out.split(b"\0"):
            if not record:
                continue
            meta, _, raw = record.partition(b"\t")
            mode, kind, oid = meta.decode().split(" ")
            if kind != "blob":
                continue
            path = raw.decode("utf-8", "surrogateescape")
            if prefix:
                if not path.startswith(prefix + "/"):
                    continue
                path = path[len(prefix) + 1 :]
            found.append((path, mode, oid))
        return found

    def read(self, key: str, commit: str, path: str) -> bytes | None:
        """The bytes of ``path`` at ``commit`` (``None``: absent)."""
        if self.object_id(key, commit, path) is None:
            return None
        return self._git(key, "show", f"{commit}:{path}", what="show")

    def contracts(self, key: str, commit: str) -> dict[str, ComponentContract]:
        """The ``piceli.toml`` contracts of ``commit`` (none: an empty mapping)."""
        text = self.read(key, commit, CONTRACT_FILE)
        if text is None:
            return {}
        return parse_contracts(text)

    def contract(self, key: str, commit: str, name: str) -> ComponentContract:
        """One component's contract at ``commit``.

        :raises CompositionError: ``component-contract-missing`` when the
            commit has no ``[component.<name>]``; ``component-contract-invalid``.
        """
        found = self.contracts(key, commit).get(name)
        if found is None:
            raise CompositionError(
                "component-contract-missing",
                f"source {key!r} at {commit[:12]} has no [component.{name}] in "
                f"{CONTRACT_FILE}",
            )
        return found

    def digest(self, key: str, commit: str, contract: ComponentContract) -> str:
        """The component's source digest at ``commit`` (its build cache key)."""
        assert contract.build is not None
        trees = {
            path: self.object_id(key, commit, path) or "absent"
            for path in contract.build.paths
        }
        return contract_digest(contract, trees)

    @contextmanager
    def checkout(self, key: str, commit: str, parent: Path) -> Iterator[Path]:
        """A clean working tree of ``commit`` of one source (removed afterwards)."""
        with self.remotes[key].checkout(commit, parent) as tree:
            yield tree


def describe_refs(
    refs: RemoteRefs, wanted: Collection[str] | None = None
) -> dict[str, str]:
    """``{ref: sha}`` with full ref names (``refs/heads/main``, ``refs/tags/v1``)."""
    found = {f"refs/heads/{name}": sha for name, sha in refs.branches.items()}
    found.update({f"refs/tags/{name}": sha for name, sha in refs.tags.items()})
    if wanted is not None:
        found = {key: value for key, value in found.items() if key in wanted}
    return dict(sorted(found.items()))
