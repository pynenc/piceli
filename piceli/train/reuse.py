"""Content-hash reuse: a step that passed on the same inputs is not run again.

A step that declares ``inputs=`` (globs of files in the tree) has a **key**:
the SHA-256 of

- its definition (kind, argv or cargo arguments, its own ``env``, the
  impact selection of a cargo step), never where it runs or its timeout,
  and never the per-trial ``PICELI_TRAIN_*`` variables;
- the Git blob id of every tracked file its inputs match (``fnmatch``
  globs, relative to the repository root; ``*`` crosses ``/``);
- the toolchain pins (``rust-toolchain``, ``rust-toolchain.toml``), always.

When a step passes, its key is recorded in the **content store** with the
run that made it and its ``outputs`` (each file's SHA-256 and mode; the
bytes are kept once, by digest). A later step with the same key, in the
same run (a bisection trial), a later run or another train of the same
machine, is ``reused``: its outputs are restored from the store and the
result names the run, trial and host that made them. Failures are never
recorded: a failing or flaky step runs again.

Layout under ``<cache>/store`` (``piceli.train.local.cache_root``)::

    keys/<key>.json          the record of a passed step (its mtime: last use)
    blobs/<aa>/<sha256>      output bytes, by digest

The store is bounded (``Train(retention=)``, :mod:`piceli.train.prune`):
records unused for ``store_days`` go, then the least recently used until
the bytes they keep fit ``store_size``; unreferenced bytes go with them.

Importing this module is side-effect free.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

#: Files whose content always keys a step (the toolchain the tree pins).
TOOLCHAIN_FILES = ("rust-toolchain", "rust-toolchain.toml")
#: The record format of ``keys/<key>.json``.
SCHEMA = "piceli.train-reuse.v1"
_CHUNK = 1 << 20


def tracked(checkout: Path) -> dict[str, str]:
    """``{path: blob id}`` of every tracked file of ``checkout``."""
    done = subprocess.run(
        ["git", "ls-files", "-s", "-z"],
        cwd=checkout,
        capture_output=True,
        timeout=600,
        check=True,
    )
    found: dict[str, str] = {}
    for entry in done.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) >= 2:
            found[path] = parts[1]
    return found


def matching(files: Mapping[str, str], patterns: Sequence[str]) -> list[list[str]]:
    """``[[path, blob], ...]`` of the files matching ``patterns``, sorted."""
    return [
        [path, blob]
        for path, blob in sorted(files.items())
        if any(
            fnmatch.fnmatchcase(path, pattern)
            or fnmatch.fnmatchcase(path, pattern.rstrip("/") + "/*")
            for pattern in patterns
        )
    ]


def step_key(
    definition: Mapping[str, Any],
    inputs: Sequence[str],
    checkout: Path,
    extra: Mapping[str, Any] | None = None,
) -> tuple[str, int]:
    """``(key, number of input files)`` of a step (see the module doc)."""
    files = tracked(checkout)
    matched = matching(files, inputs)
    body = {
        "schema": SCHEMA,
        "definition": {
            key: value
            for key, value in definition.items()
            if key not in {"timeout", "hosts"}
        },
        "inputs": matched,
        "toolchain": {name: files.get(name) for name in TOOLCHAIN_FILES},
        "extra": dict(extra or {}),
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest(), len(matched)


def _files(checkout: Path, output: str) -> list[tuple[str, Path]] | None:
    """``[(relative path, file)]`` of one declared output (``None``: missing)."""
    path = checkout / output
    if path.is_file():
        return [(output, path)]
    if path.is_dir():
        return [
            (item.relative_to(checkout).as_posix(), item)
            for item in sorted(path.rglob("*"))
            if item.is_file()
        ]
    return None


class Store:
    """The content store (see the module doc)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _record(self, key: str) -> Path:
        return self.root / "keys" / f"{key.split(':', 1)[-1]}.json"

    def _blob(self, digest: str) -> Path:
        hexa = digest.split(":", 1)[-1]
        return self.root / "blobs" / hexa[:2] / hexa

    def lookup(self, key: str) -> dict[str, Any] | None:
        """The record of ``key`` when every output's bytes are still here."""
        try:
            record = json.loads(self._record(key).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict) or record.get("key") != key:
            return None
        for item in record.get("outputs") or ():
            if not self._blob(str(item.get("sha256"))).is_file():
                return None
        try:  # its last use, for the least-recently-used prune
            os.utime(self._record(key))
        except OSError:
            pass
        return record

    def _put(self, path: Path) -> str:
        """Keep ``path``'s bytes; their digest."""
        blobs = self.root / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        fd, partial = tempfile.mkstemp(dir=blobs, prefix=".partial-")
        try:
            with os.fdopen(fd, "wb") as out, path.open("rb") as source:
                while chunk := source.read(_CHUNK):
                    digest.update(chunk)
                    out.write(chunk)
            name = "sha256:" + digest.hexdigest()
            final = self._blob(name)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(partial, final)
            return name
        finally:
            Path(partial).unlink(missing_ok=True)

    def save(
        self,
        key: str,
        checkout: Path,
        outputs: Sequence[str],
        meta: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Record a passed step and keep its outputs; ``None`` when a declared
        output is missing (nothing is recorded: the step runs next time)."""
        items: list[dict[str, Any]] = []
        for output in outputs:
            found = _files(checkout, output)
            if found is None:
                return None
            for relative, path in found:
                mode = path.stat().st_mode
                items.append(
                    {
                        "path": relative,
                        "sha256": self._put(path),
                        "size": path.stat().st_size,
                        "executable": bool(mode & stat.S_IXUSR),
                    }
                )
        record = {"schema": SCHEMA, "key": key, **dict(meta), "outputs": items}
        target = self._record(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(f".partial-{os.getpid()}")
        partial.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        os.replace(partial, target)
        return record

    def restore(self, record: Mapping[str, Any], checkout: Path) -> list[str]:
        """Write a record's outputs into ``checkout``; their paths."""
        written = []
        for item in record.get("outputs") or ():
            relative = str(item["path"])
            if relative.startswith("/") or ".." in relative.split("/"):
                continue
            dest = checkout / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            partial = dest.with_name(f".{dest.name}.partial")
            shutil.copyfile(self._blob(str(item["sha256"])), partial)
            os.chmod(partial, 0o755 if item.get("executable") else 0o644)
            os.replace(partial, dest)
            written.append(relative)
        return written
