"""Where deployment state lives, and the lock that serializes deployers.

One interface, two backends:

``local`` (the default)
    The state directory on this machine is the state. A lock file in it
    serializes runs that share the directory (``fcntl.flock``). Two machines
    have two independent states: use it for development and for one
    persistent runner.
``cluster``
    The state lives in the release namespace (:mod:`piceli.state.cluster`)
    and the state directory is a working copy of it. A session takes the
    release-scoped Lease lock (namespace + release name), replaces the
    working copy with the shared snapshot, and writes the snapshot back at
    every checkpoint (every journaled stage change, every apply step, at most
    every few seconds) and when it ends, fenced by the lease. Any runner
    with the kubeconfig can then plan, apply, resume or roll back; two
    deployers of one release never interleave.

Every consumer of state (the ``piceli deploy`` run journal, the release
catalog, the execution journal, the secret version store, receipts) keeps
reading and writing files in the state directory; only the session decides
where they come from and go to. The choice is reversible: ``piceli state
export`` and ``piceli state import`` move a state between backends.

Importing this module is side-effect free.
"""

from __future__ import annotations

import fcntl
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from piceli.state.errors import StateError

BACKENDS = ("local", "cluster")
#: Minimum seconds between two non-forced checkpoint writes.
CHECKPOINT_SECONDS = 5.0


@dataclass(frozen=True)
class StateSettings:
    """``state`` and ``state_lease_seconds`` of a Pipeline or ``[release]``."""

    backend: str = "local"
    lease_seconds: int = 60

    def __post_init__(self) -> None:
        if self.backend not in BACKENDS:
            raise ValueError(f"state must be one of {', '.join(BACKENDS)}")
        if not isinstance(self.lease_seconds, int) or not (
            5 <= self.lease_seconds <= 3600
        ):
            raise ValueError("state_lease_seconds must be an integer in [5, 3600]")


@dataclass(frozen=True)
class StateScope:
    """What one shared state belongs to.

    :param directory: The local state directory (the working copy).
    :param name: The release name: the lock and state objects derive from it.
    :param namespace: The release namespace.
    :param layout: ``pipeline`` (``piceli deploy``) or ``release``
        (``release.toml``).
    :param target: The explicit ``KubeconfigTarget`` (cluster backend).
    :param lock_code: Error code when another deployer holds the lock.
    """

    directory: Path
    name: str
    namespace: str
    layout: str
    settings: StateSettings
    target: Any = None
    lock_code: str = "pipeline-locked"


@contextmanager
def directory_lock(directory: Path, code: str = "pipeline-locked") -> Iterator[None]:
    """Hold ``<directory>/deploy.lock`` (refused when another process holds it)."""
    from piceli.state.snapshot import private_dir

    private_dir(directory)
    handle: IO[str] = open(directory / "deploy.lock", "a+")  # noqa: SIM115
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StateError(
                code, "another piceli run is using this state directory"
            ) from None
        yield
    finally:
        handle.close()


class Session:
    """An open state session: checkpoints write the working copy back."""

    backend = "local"

    def checkpoint(self, *, force: bool = False) -> None:
        """Write the working copy to the shared state (no-op for ``local``)."""

    def check(self) -> None:
        """Raise when the lock was lost (no request)."""

    def describe(self) -> dict[str, Any]:
        return {"backend": self.backend}


class ClusterSession(Session):
    backend = "cluster"

    def __init__(
        self,
        store: Any,
        directory: Path,
        *,
        write: bool,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.directory = directory
        self.write = write
        self.clock = clock
        self.manifest: dict[str, Any] | None = None
        self._last = 0.0

    def checkpoint(self, *, force: bool = False) -> None:
        if not self.write:
            return
        self.store.check()
        if not force and self.clock() - self._last < CHECKPOINT_SECONDS:
            return
        import hashlib

        from piceli.state.snapshot import pack

        data, names = pack(self.directory)
        digest = hashlib.sha256(data).hexdigest()
        if digest != (self.manifest or {}).get("sha256"):
            self.manifest = self.store.push(data, files=len(names))
            _mark(self.directory, self.manifest)
        else:
            self.store.renew()  # nothing new to write; still prove the lock
        self._last = self.clock()

    def check(self) -> None:
        if self.store is not None:
            self.store.check()

    def describe(self) -> dict[str, Any]:
        manifest = self.manifest or {}
        return {
            "backend": "cluster",
            "generation": manifest.get("generation"),
            "holder": self.store.holder if self.write else None,
        }


#: The generation and digest this working copy last synced (a dotfile, never
#: part of a snapshot).
MARKER = ".piceli-state.json"


def _mark(directory: Path, manifest: dict[str, Any] | None) -> None:
    import json

    from piceli.state.snapshot import _write

    if manifest is None:
        return
    value = {key: manifest.get(key) for key in ("name", "generation", "sha256")}
    _write(directory / MARKER, json.dumps(value, sort_keys=True).encode())


def _unsynced(directory: Path, manifest: dict[str, Any] | None) -> bool:
    """The working copy changed on top of the shared generation it last synced."""
    import hashlib
    import json

    from piceli.state.snapshot import pack

    try:
        marker = json.loads((directory / MARKER).read_text())
    except (OSError, ValueError):
        return False
    if (
        manifest is None
        or not isinstance(marker, dict)
        or marker.get("name") != manifest.get("name")
        or marker.get("generation") != manifest.get("generation")
        or marker.get("sha256") != manifest.get("sha256")
    ):
        return False
    data, _names = pack(directory)
    return hashlib.sha256(data).hexdigest() != manifest.get("sha256")


def open_cluster_store(scope: StateScope) -> Any:
    from piceli.state.cluster import open_store

    return open_store(
        scope.target,
        name=scope.name,
        layout=scope.layout,
        lease_seconds=scope.settings.lease_seconds,
        lock_code=scope.lock_code,
    )


#: Opens the cluster store of a scope; tests replace it.
STORE_FACTORY: list[Callable[[StateScope], Any]] = [open_cluster_store]


@contextmanager
def session(
    scope: StateScope,
    *,
    write: bool = True,
    say: Callable[[str], None] | None = None,
) -> Iterator[Session]:
    """Open the state of ``scope`` for one command.

    ``write``: the command changes state (plan, apply, resume, rollback …) and
    takes the release lock; otherwise the working copy is refreshed without
    the lock (and left as is when a run on this machine holds the directory).
    """
    say = say or (lambda _line: None)
    if scope.settings.backend == "local":
        if write:
            with directory_lock(scope.directory, scope.lock_code):
                yield Session()
        else:
            yield Session()
        return
    from piceli.state.snapshot import members, pack, unpack

    try:
        guard = directory_lock(scope.directory, scope.lock_code)
        guard.__enter__()
    except StateError:
        if write:
            raise
        # A run on this machine holds the working copy: it is the freshest.
        yield ClusterSession(None, scope.directory, write=False)
        return
    try:
        store = STORE_FACTORY[0](scope)
        try:
            if write:
                fence = store.acquire()
                if store.took_over:
                    say(
                        f"state: took over the expired lock of {scope.name!r} from "
                        f"{store.took_over['holder']}"
                    )
                store.start_heartbeat()
            data, manifest = store.pull()
            current = ClusterSession(store, scope.directory, write=write)
            current.manifest = manifest
            ahead = data is not None and _unsynced(scope.directory, manifest)
            if ahead and write:
                # This runner wrote after the last generation it synced, and
                # nobody wrote since (a write-back that failed): keep it.
                packed, names = pack(scope.directory)
                current.manifest = store.push(packed, files=len(names))
                say(f"state: wrote this runner's unsynced state of {scope.name!r}")
            elif data is not None and not ahead:
                unpack(data, scope.directory)
            elif data is None and write and members(scope.directory):
                # First use of the cluster backend: the local state becomes
                # the shared state (nothing is dropped).
                packed, names = pack(scope.directory)
                current.manifest = store.push(packed, files=len(names))
                say(
                    f"state: moved the local state of {scope.name!r} "
                    f"({len(names)} files) to the cluster"
                )
            if write:
                say(
                    f"state: holding the lock of {scope.name!r} "
                    f"(lease transition {fence.transitions})"
                )
            _mark(scope.directory, current.manifest)
            failed = False
            try:
                yield current
            except BaseException:
                failed = True
                raise
            finally:
                if write:
                    try:
                        current.checkpoint(force=True)
                    except StateError as error:
                        if not failed:
                            raise
                        say(f"state: the final state write failed ({error.code})")
        finally:
            store.close()
    finally:
        guard.__exit__(None, None, None)
