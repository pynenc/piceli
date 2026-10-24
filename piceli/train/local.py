"""This machine as a train host: its slots, warm cargo targets and sccache.

Every trial runs in a fresh worktree, so a cargo build there starts cold.
Local cargo commands share a target directory instead, per repository and
train, under Piceli's cache (``$XDG_CACHE_HOME/piceli/train``, else
``~/.cache/piceli/train``)::

    slots/local-<n>.lock                 this machine's slots (every train)
    checkouts/<train>/<repo>/<n>/        a trial's worktree (removed after it)
    checkouts/<train>/<repo>/lock-<n>.lock   held while a trial uses it
    checkouts/<train>/<repo>/<n>.times.json  its files' blobs and mtimes
    targets/<train>/<repo>/<n>/          the target directory of checkout n
    targets/<train>/<repo>/slot-<n>/     of a local cargo command outside one
    store/                               the content store (piceli.train.reuse)

``<repo>`` is the repository's directory name and a hash of its path.
Everything here is bounded and pruned at the end of each run
(:mod:`piceli.train.prune`).

Cargo fingerprints a workspace member by its absolute path and by the
modification time of its sources, so a warm target directory only helps a
checkout at the same path whose unchanged files keep their times. Each
trial's worktree is made at one of a few stable paths (a pool, locked while
a trial uses it), builds into that path's own target directory, and every
file whose content (Git blob) is the same as at that path's previous trial
gets that trial's modification time back; a changed file keeps the new time
of its checkout, so cargo rebuilds exactly what changed. (Correct by
induction: a time is only kept while the content at that path never
changed since, so every build after it saw this content.)

A command takes one of the machine's ``local_slots`` (a file lock, so the
limit holds across every train and process on the machine, and a crashed
run releases it) and, when it runs cargo, the target directory of that slot:
two commands never share a target directory at once, and with one slot every
trial builds in the same, warm directory. ``sccache`` is used as the
compiler wrapper when it is on ``PATH`` (``Train(sccache="auto")``).

Importing this module is side-effect free.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

#: Seconds between looks for a free slot.
POLL = 0.2


def cache_root(configured: str | None = None) -> Path:
    """Piceli's train cache: ``configured``, else ``$XDG_CACHE_HOME/piceli/train``,
    else ``~/.cache/piceli/train``."""
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "piceli" / "train"


class Slots:
    """``count`` slots of this machine, shared by every train on it.

    A slot is a lock file (``flock``): taking it is atomic, the operating
    system releases it when the process ends, and a second train on the
    machine waits for the same slots.
    """

    def __init__(self, directory: Path, count: int, name: str = "local") -> None:
        self.directory = directory
        self.count = count
        self.name = name

    def _path(self, slot: int) -> Path:
        return self.directory / f"{self.name}-{slot}.lock"

    def _try(self, slot: int) -> IO[str] | None:
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = self._path(slot).open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        return handle

    def free(self) -> int:
        """How many slots are free now."""
        count = 0
        for slot in range(self.count):
            handle = self._try(slot)
            if handle is not None:
                count += 1
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()
        return count

    @contextmanager
    def take(self, deadline: float | None = None) -> Iterator[int]:
        """Hold the first free slot (waiting for one); yields its number."""
        while True:
            for slot in range(self.count):
                handle = self._try(slot)
                if handle is not None:
                    try:
                        yield slot
                    finally:
                        fcntl.flock(handle, fcntl.LOCK_UN)
                        handle.close()
                    return
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("no free slot")
            time.sleep(POLL)


class Checkouts:
    """Stable worktree paths for trials (see the module doc): ``take()``
    yields a free path, held until the block exits; :meth:`keep_times`
    gives unchanged files their earlier modification times."""

    def __init__(self, root: Path, train: str, repo: Path, count: int = 4) -> None:
        self.directory = root / "checkouts" / train / _repo_key(repo)
        self.slots = Slots(self.directory, count, name="lock")

    @contextmanager
    def take(self) -> Iterator[Path]:
        with self.slots.take() as slot:
            yield self.directory / str(slot)

    def keep_times(self, checkout: Path) -> int:
        """Restore the times of files unchanged since this path's previous
        trial and record this one's; how many kept their time."""
        from piceli.train.reuse import tracked

        record = checkout.with_name(f"{checkout.name}.times.json")
        try:
            before = json.loads(record.read_text())
        except (OSError, ValueError):
            before = {}
        now: dict[str, list[Any]] = {}
        kept = 0
        for path, blob in tracked(checkout).items():
            file = checkout / path
            old = before.get(path)
            try:
                if isinstance(old, list) and len(old) == 2 and old[0] == blob:
                    os.utime(file, ns=(old[1], old[1]), follow_symlinks=False)
                    kept += 1
                    now[path] = old
                else:
                    now[path] = [blob, file.lstat().st_mtime_ns]
            except OSError:
                continue
        partial = record.with_suffix(f".partial-{os.getpid()}")
        partial.write_text(json.dumps(now, separators=(",", ":")))
        os.replace(partial, record)
        return kept


#: A file in a target directory touched whenever a command uses it (its
#: last use, for pruning).
USED_MARKER = ".piceli-used"


def touch_used(directory: Path) -> None:
    marker = directory / USED_MARKER
    marker.touch(exist_ok=True)
    os.utime(marker)


def _repo_key(repo: Path) -> str:
    digest = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:10]
    return f"{repo.resolve().name}-{digest}"


class CargoCache:
    """The cargo environment of a local command in a slot.

    :param target: ``"auto"`` (a target directory per slot under ``root``,
        unless the caller's environment sets ``CARGO_TARGET_DIR``), a path
        (shared by every slot: cargo's own lock serialises its users), or
        ``None`` (the checkout's own ``target/``).
    :param sccache: ``"auto"``, ``True`` or ``False`` (see the module doc).
    """

    def __init__(
        self,
        root: Path,
        train: str,
        repo: Path,
        *,
        target: str | None = "auto",
        sccache: bool | str = "auto",
    ) -> None:
        self.root = root
        self.train = train
        self.repo = repo
        self.target = target
        self.sccache = sccache

    def target_dir(
        self, slot: int, env: Mapping[str, str], cwd: Path | None = None
    ) -> Path | None:
        """The target directory of a command: the one of its checkout path
        (a trial's stable worktree), else of its slot."""
        if self.target is None:
            return None
        if self.target == "auto":
            if env.get("CARGO_TARGET_DIR"):
                return None  # the caller chose one
            base = self.root / "targets" / self.train / _repo_key(self.repo)
            pool = self.root / "checkouts" / self.train / _repo_key(self.repo)
            if cwd is not None and cwd.parent == pool:
                return base / cwd.name
            return base / f"slot-{slot}"
        return Path(self.target).expanduser()

    def environment(
        self, slot: int, env: Mapping[str, str], cwd: Path | None = None
    ) -> tuple[dict[str, str], Path | None]:
        """``(variables to add, the target directory set or None)``."""
        extra: dict[str, str] = {}
        target = self.target_dir(slot, env, cwd)
        if target is not None:
            target.mkdir(parents=True, exist_ok=True)
            touch_used(target)
            extra["CARGO_TARGET_DIR"] = str(target)
        wrapper = self._sccache(env)
        if wrapper:
            extra["RUSTC_WRAPPER"] = wrapper
        return extra, target

    def _sccache(self, env: Mapping[str, str]) -> str | None:
        if self.sccache is False or env.get("RUSTC_WRAPPER"):
            return None
        found = shutil.which("sccache", path=env.get("PATH"))
        if found is None and self.sccache is True:
            return "sccache"  # asked for: cargo says it is missing
        return found


def bring_outputs(target: Path, cwd: Path, outputs: Sequence[str]) -> list[str]:
    """Copy the ``target/...`` outputs a command made in the shared target
    directory ``target`` to ``cwd/target/...``, where the step expects them."""
    copied: list[str] = []
    for output in outputs:
        if output != "target" and not output.startswith("target/"):
            continue
        rest = output[len("target/") :] if output != "target" else ""
        source = target / rest if rest else target
        dest = cwd / output
        if source.is_dir():
            shutil.copytree(source, dest, dirs_exist_ok=True, symlinks=True)
        elif source.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
        else:
            continue
        copied.append(output)
    return copied
