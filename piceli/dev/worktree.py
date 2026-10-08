"""``--worktree``: a source as it is on disk (0.18.0).

The tracked files as they are now (committed or not) plus the untracked
files Git does not ignore (``git ls-files --cached --others
--exclude-standard``); deleted files are left out, ``target/`` and every
other ignored path never leave the machine. The source records its ``HEAD``
commit and whether the tree differs from it (``dirty``).

Importing this module is side-effect free.
"""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path
from typing import Any

from piceli.dev.model import DevError


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False
    )


def add_worktree(archive: tarfile.TarFile, request: Any) -> dict[str, Any]:
    """Add the working tree of ``request.path`` under ``request.name/``."""
    from piceli.dev.pack import toplevel

    repo = toplevel(request.path)
    listed = _run(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listed.returncode != 0:
        raise DevError("dev-source-invalid", f"cannot list the files of {request.name}")
    head = _run(repo, "rev-parse", "--verify", "--quiet", "HEAD")
    status = _run(repo, "status", "--porcelain", "--untracked-files=normal")
    count = 0
    for raw in sorted(set(listed.stdout.split(b"\0"))):
        if not raw:
            continue
        relative = raw.decode("utf-8", "surrogateescape")
        path = repo / relative
        if not path.is_symlink() and not path.is_file():
            continue  # deleted, or a submodule's directory
        archive.add(path, arcname=f"{request.name}/{relative}", recursive=False)
        count += 1
    return {
        "commit": head.stdout.decode().strip() or None,
        "ref": None,
        "dirty": bool(status.stdout.strip()),
        "files": count,
        "worktree": str(repo),
    }
