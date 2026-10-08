"""What a development run builds, packed on the client into one tar.gz (0.18.0).

The root source and every sibling source sit side by side under their
names (``product/``, ``ih-muse/``), so path dependencies like
``../ih-muse`` resolve in the run as on the developer's disk. A source at a
ref is ``git archive`` of that commit; nothing in the cluster needs Git
credentials, a registry or a container engine.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from piceli.dev.model import DevError

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")


@dataclass(frozen=True)
class SourceRequest:
    """One source of a run: ``name`` (its directory), a local repository, a ref.

    ``ref=None`` ships the working tree (``--worktree``).
    """

    name: str
    path: Path
    ref: str | None = None


@dataclass
class Packed:
    """The archive a run uploads, its digest and what it holds."""

    path: Path
    sha256: str
    bytes: int
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)


def _bad_source(message: str) -> DevError:
    return DevError("dev-source-invalid", message)


def parse_source(text: str) -> SourceRequest:
    """``name=path[@ref]`` (``--source``); without ``@ref``, the working tree."""
    name, sep, rest = text.partition("=")
    if not sep or not _NAME.fullmatch(name) or not rest:
        raise _bad_source(
            f"--source {text!r}: use NAME=PATH[@REF] (NAME a directory name)"
        )
    path, at, ref = rest.rpartition("@") if "@" in rest else (rest, "", "")
    if at and not ref:
        raise _bad_source(f"--source {text!r}: the ref after @ is empty")
    return SourceRequest(name, Path(path).expanduser(), ref or None)


def _git(path: Path, *args: str, binary: bool = False) -> Any:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=not binary,
        check=False,
    )


def toplevel(path: Path) -> Path:
    """The repository holding ``path``.

    :raises DevError: ``dev-source-invalid``.
    """
    found = _git(path, "rev-parse", "--show-toplevel") if path.is_dir() else None
    if found is None or found.returncode != 0:
        raise _bad_source(f"{path} is not in a Git repository")
    return Path(found.stdout.strip())


def resolve(path: Path, ref: str) -> str:
    """The commit ``ref`` names in the repository at ``path``.

    :raises DevError: ``dev-ref-unknown`` (fetch it first).
    """
    found = _git(path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if found.returncode != 0 or not found.stdout.strip():
        raise DevError(
            "dev-ref-unknown",
            f"{ref!r} is no commit of {path.name}; fetch it first (git fetch)",
        )
    return str(found.stdout.strip())


def _add_commit(out: tarfile.TarFile, request: SourceRequest) -> dict[str, Any]:
    repo = toplevel(request.path)
    commit = resolve(repo, str(request.ref))
    process = subprocess.Popen(
        ["git", "-C", str(repo), "archive", "--format=tar", commit],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
        for member in archive:
            if member.type in (tarfile.XGLTYPE, tarfile.XHDTYPE):
                continue
            data = archive.extractfile(member) if member.isfile() else None
            member.name = f"{request.name}/{member.name}"
            out.addfile(member, data)
    if process.wait() != 0:
        raise _bad_source(f"git archive of {request.name} failed")
    return {"commit": commit, "ref": request.ref, "dirty": False}


def pack(root: SourceRequest, extras: list[SourceRequest], out: Path) -> Packed:
    """Pack ``root`` and ``extras`` into ``out/tree.tar.gz``.

    :raises DevError: ``dev-source-invalid``, ``dev-ref-unknown``.
    """
    names = [root.name, *(item.name for item in extras)]
    if len(set(names)) != len(names):
        raise _bad_source("two sources have the same name")
    out.mkdir(parents=True, exist_ok=True)
    path = out / "tree.tar.gz"
    sources: dict[str, dict[str, Any]] = {}
    with tarfile.open(path, "w:gz", compresslevel=3) as archive:
        for request in (root, *extras):
            if request.ref is None:
                raise _bad_source(f"source {request.name} needs a ref (NAME=PATH@REF)")
            sources[request.name] = _add_commit(archive, request)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return Packed(path, digest.hexdigest(), path.stat().st_size, sources)
