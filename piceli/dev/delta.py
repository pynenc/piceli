"""Git deltas for development runs (0.19.0): ship what the builder lacks.

The builder keeps one Git mirror per repository on the dev cache claim. A
run first asks which of a few commits the mirror has (the run pod's init
container fetches the repository's remote with the cluster's credentials
first, when it may); the client then uploads only:

- a ``git bundle`` of the commits from the nearest known base to ``head``
  (nothing when the mirror has ``head``; the whole history when it has no
  base at all, which seeds the mirror for every later run);
- for a working tree, the uncommitted diff (``git diff --binary HEAD``) and
  the untracked files Git does not ignore.

A typical agent run uploads kilobytes. The run's wrapper unbundles into the
mirror, exports ``head``, applies the diff and the untracked files, and
syncs the tree into its lineage as before (:mod:`piceli.dev.pod`).

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from piceli.dev.model import DevError
from piceli.dev.pack import Packed, SourceRequest, resolve, toplevel

UPLOAD_SCHEMA = "piceli.dev-upload.v1"
#: Candidate bases a run asks the mirror about (the upstream tip, the
#: merge base with the remote's default branch, ancestors at doubling
#: distances).
MAX_CANDIDATES = 16
_SCP = re.compile(r"^(?:[\w.-]+@)?([\w.-]+):(?!//)(.+)$")


def canonical_url(url: str | None) -> str | None:
    """``https://host/path`` (lowercase, no ``.git``, no user) of a remote URL.

    SSH forms (``git@host:org/repo.git``, ``ssh://git@host/org/repo``)
    map to the HTTPS URL the cluster fetches with its token; a local path
    gives ``None`` (nothing the cluster can fetch).
    """
    if not url:
        return None
    text = url.strip()
    found = _SCP.match(text)
    if found and "://" not in text:
        host, path = found.group(1), found.group(2)
    else:
        parts = urlsplit(text)
        if parts.scheme not in {"https", "http", "ssh", "git"} or not parts.hostname:
            return None
        host, path = parts.hostname, parts.path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path:
        return None
    return f"https://{host.lower()}/{path.lower()}"


def mirror_key(name: str, url: str | None) -> str:
    """The mirror's directory name: the source name and its remote's digest."""
    digest = hashlib.sha256((url or name).encode()).hexdigest()[:10]
    safe = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-") or "source"
    return f"{safe}-{digest}"


def _git(
    repo: Path, *args: str, binary: bool = False
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=not binary,
        check=False,
    )


def _out(repo: Path, *args: str) -> str | None:
    found = _git(repo, *args)
    return (
        found.stdout.strip() if found.returncode == 0 and found.stdout.strip() else None
    )


@dataclass
class SourcePlan:
    """What a source will ship: its commit, candidate bases, remote, changes."""

    name: str
    path: Path
    ref: str | None
    head: str
    candidates: list[str] = field(default_factory=list)
    url: str | None = None
    diff: bool = False
    dirty: bool = False

    def describe(self) -> dict[str, Any]:
        """The spec's part: what the init container fetches and checks."""
        return {
            "name": self.name,
            "key": mirror_key(self.name, self.url),
            "url": self.url,
            "head": self.head,
            "candidates": list(self.candidates),
        }


def _candidates(repo: Path, head: str) -> list[str]:
    found: list[str] = []
    upstream = _out(repo, "rev-parse", "--verify", "--quiet", "@{upstream}")
    default = _out(repo, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/HEAD")
    for tip in (upstream, default):
        if not tip:
            continue
        base = _out(repo, "merge-base", head, tip)
        for sha in (tip if base == tip else None, base):
            if sha and sha != head and sha not in found:
                found.append(sha)
    listed = (
        _out(repo, "rev-list", "--first-parent", "--max-count=1025", head) or ""
    ).split()
    distance = 1
    while distance < len(listed) and len(found) < MAX_CANDIDATES:
        if listed[distance] not in found:
            found.append(listed[distance])
        distance *= 2
    return found[:MAX_CANDIDATES]


def plan_source(request: SourceRequest) -> SourcePlan:
    """The commit a source runs at and the bases the mirror may already have.

    :raises DevError: ``dev-source-invalid`` (no Git repository, no commit),
        ``dev-ref-unknown``.
    """
    repo = toplevel(request.path)
    if request.ref is not None:
        head = resolve(repo, request.ref)
        diff = dirty = False
    else:
        found = _out(repo, "rev-parse", "--verify", "--quiet", "HEAD")
        if found is None:
            raise DevError(
                "dev-source-invalid", f"{request.name} has no commit yet: commit once"
            )
        head = found
        status = _git(repo, "status", "--porcelain", "--untracked-files=normal")
        dirty = bool(status.stdout.strip())
        diff = dirty
    remote = _out(repo, "remote", "get-url", "origin")
    return SourcePlan(
        name=request.name,
        path=repo,
        ref=request.ref,
        head=head,
        candidates=_candidates(repo, head),
        url=canonical_url(remote),
        diff=diff,
        dirty=dirty,
    )


def _bundle(plan: SourcePlan, bases: Iterable[str], into: Path) -> bool:
    """Write a bundle of ``head`` without ``bases``; ``False`` when empty."""
    ref = f"refs/piceli/dev-upload-{os.getpid()}"
    made = _git(plan.path, "update-ref", ref, plan.head)
    if made.returncode != 0:
        raise DevError("dev-source-invalid", f"cannot bundle {plan.name}")
    try:
        result = _git(
            plan.path,
            "bundle",
            "create",
            "--quiet",
            str(into),
            ref,
            *(f"^{base}" for base in bases),
        )
    finally:
        _git(plan.path, "update-ref", "-d", ref)
    if result.returncode != 0:
        if "empty bundle" in (result.stderr or ""):
            return False
        raise DevError("dev-source-invalid", f"git bundle of {plan.name} failed")
    return True


def _untracked(plan: SourcePlan) -> bytes:
    listed = _git(
        plan.path, "ls-files", "-z", "--others", "--exclude-standard", binary=True
    )
    names = [
        n.decode("utf-8", "surrogateescape") for n in listed.stdout.split(b"\0") if n
    ]
    if not names:
        return b""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name in sorted(names):
            path = plan.path / name
            if path.is_symlink() or path.is_file():
                tar.add(path, arcname=name, recursive=False)
    return buffer.getvalue()


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(data), 0o600
    tar.addfile(info, io.BytesIO(data))


def pack_delta(
    plans: list[SourcePlan], have: Mapping[str, set[str]], out: Path
) -> Packed:
    """Pack each source's delta against what the mirror has (``have``).

    :raises DevError: ``dev-source-invalid``.
    """
    out.mkdir(parents=True, exist_ok=True)
    path = out / "upload.tar.gz"
    manifest: dict[str, Any] = {"schema": UPLOAD_SCHEMA, "sources": {}}
    sources: dict[str, dict[str, Any]] = {}
    with tarfile.open(path, "w:gz", compresslevel=6) as archive:
        for plan in plans:
            known = have.get(plan.name) or set()
            entry: dict[str, Any] = {"mode": "delta", "head": plan.head}
            if plan.head in known:
                entry.update(base=[plan.head], bundle=False)
            else:
                bases = [sha for sha in plan.candidates if sha in known][:4]
                bundle = out / f"{plan.name}.bundle"
                entry["base"] = bases
                entry["bundle"] = _bundle(plan, bases, bundle)
                if entry["bundle"]:
                    archive.add(bundle, arcname=f"sources/{plan.name}/bundle")
                    entry["bundle_bytes"] = bundle.stat().st_size
                    bundle.unlink()
            if plan.diff:
                diff = _git(plan.path, "diff", "--binary", "HEAD", binary=True).stdout
                if diff:
                    _add(archive, f"sources/{plan.name}/diff", diff)
                entry["diff"] = bool(diff)
                untracked = _untracked(plan)
                if untracked:
                    _add(archive, f"sources/{plan.name}/untracked.tar", untracked)
                entry["untracked"] = bool(untracked)
            manifest["sources"][plan.name] = entry
            sources[plan.name] = {
                "commit": plan.head,
                "ref": plan.ref,
                "dirty": plan.dirty,
                "mode": "delta",
                "base": entry["base"][0] if entry["base"] else None,
                "bundle_bytes": entry.get("bundle_bytes", 0),
            }
        _add(archive, "manifest.json", json.dumps(manifest, sort_keys=True).encode())
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return Packed(path, digest.hexdigest(), path.stat().st_size, sources)
