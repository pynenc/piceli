"""Piceli's temporary files and directories: named, tracked, always removed.

Every temporary directory Piceli creates outside a state directory is named
``piceli-<purpose>-<random>`` in the system temporary directory
(:func:`tempfile.gettempdir`, so ``TMPDIR`` moves it), is owner-only
(``0700``) and is removed when its ``with`` block exits: on success, on an
error and on ``Ctrl-C``.

Two exits skip ``finally`` blocks. A process killed by a signal whose
default action terminates it (``SIGTERM`` from a cancelled CI job,
``SIGHUP`` from a closed terminal) never unwinds, and a thread that is still
inside its ``with`` block when the interpreter exits never leaves it. So
every live path is also registered here: :func:`install_signal_cleanup`
(called by the ``piceli`` entry point) removes them before the signal's
default action, and an :mod:`atexit` hook removes whatever is left at exit.
``SIGKILL`` cannot be caught; ``piceli cache prune`` removes ``piceli-*``
temporary directories older than :data:`STALE_SECONDS`.

Importing this module is side-effect free: the ``atexit`` hook is registered
on first use and signal handlers only by :func:`install_signal_cleanup`.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import signal
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

#: Name prefix of every temporary directory Piceli creates in the system
#: temporary directory (``piceli-<purpose>-<random>``).
PREFIX = "piceli-"
#: The name of a temporary entry Piceli creates: ``piceli-<purpose>-<8
#: random characters>`` (an optional suffix such as ``.sqlite``), with a
#: leading dot next to an output it renames into place (``.piceli-oci-…``).
#: The test suite fails on any left behind.
TEMPORARY_NAME = re.compile(r"\.?piceli-[a-z0-9-]+-[a-z0-9_]{8}(\.[a-z]+)?")
#: A ``piceli-*`` temporary directory older than this belongs to a process
#: that was killed (a build or deploy never runs this long); ``piceli cache
#: prune`` may remove it.
STALE_SECONDS = 6 * 3600

_lock = threading.RLock()  # re-entered by a signal handler
_live: dict[str, Callable[[], None] | None] = {}
_hooked = False


def _remove(path: str) -> None:
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.unlink(path)
    except OSError:
        pass


def is_temporary(name: str) -> bool:
    """Whether ``name`` (a file name, no directory) is one of Piceli's
    temporary entries (:data:`TEMPORARY_NAME`)."""
    return TEMPORARY_NAME.fullmatch(name) is not None


def track(path: Path | str, cleanup: Callable[[], None] | None = None) -> None:
    """Remove ``path`` at interpreter exit or on a terminating signal, unless
    :func:`untrack` is called first. ``cleanup`` replaces the default removal
    (for example to also drop a git worktree registration)."""
    global _hooked
    with _lock:
        _live[os.fspath(path)] = cleanup
        if not _hooked:
            atexit.register(cleanup_all)
            _hooked = True


def untrack(path: Path | str) -> None:
    """Forget ``path`` (it was removed, or published under its final name)."""
    with _lock:
        _live.pop(os.fspath(path), None)


def tracked() -> list[str]:
    """The paths that are live now (for tests and diagnostics)."""
    with _lock:
        return sorted(_live)


def cleanup_all() -> None:
    """Remove every live path now (idempotent; errors are ignored)."""
    with _lock:
        items = list(_live.items())
        _live.clear()
    for path, cleanup in items:
        try:
            if cleanup is not None:
                cleanup()
            _remove(path)
        except Exception:  # cleanup is best effort at exit
            pass


def make_directory(
    purpose: str, *, dir: Path | None = None, hidden: bool = False
) -> Path:
    """Create and :func:`track` an owner-only ``piceli-<purpose>-*`` directory.

    The caller removes it (``shutil.rmtree`` then :func:`untrack`), or
    publishes it with ``os.rename`` and untracks it. Prefer
    :func:`temporary_directory`, which does both.
    """
    prefix = f"{'.' if hidden else ''}{PREFIX}{purpose}-"
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=dir))
    track(path)
    return path


def discard(path: Path) -> None:
    """Remove a directory from :func:`make_directory` and untrack it."""
    shutil.rmtree(path, ignore_errors=True)
    untrack(path)


@contextmanager
def temporary_directory(
    purpose: str, *, dir: Path | None = None, hidden: bool = False
) -> Iterator[Path]:
    """An owner-only ``piceli-<purpose>-*`` directory, removed on exit.

    ``dir`` defaults to the system temporary directory; ``hidden`` adds a
    leading dot (for a directory next to an output the caller renames into
    place).
    """
    path = make_directory(purpose, dir=dir, hidden=hidden)
    try:
        yield path
    finally:
        discard(path)


@contextmanager
def temporary_file(path: Path) -> Iterator[Path]:
    """``path`` (a partial file the caller creates and then renames into place)
    is removed when the block fails or is interrupted."""
    track(path)
    try:
        yield path
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        untrack(path)


_SIGNALS = ("SIGTERM", "SIGHUP")


def install_signal_cleanup() -> None:
    """Remove live temporary paths before ``SIGTERM``/``SIGHUP`` terminate us.

    Only replaces a signal's *default* handler (an ignored or custom handler
    stays), and only from the main thread. The handler removes every live
    path, restores the default action and re-sends the signal, so the exit
    status is unchanged. Commands that turn ``SIGTERM`` into
    ``KeyboardInterrupt`` (``piceli deploy``) install their own handler over
    this one while they run.
    """
    if threading.current_thread() is not threading.main_thread():
        return
    for name in _SIGNALS:
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            if signal.getsignal(number) is signal.SIG_DFL:
                signal.signal(number, _terminate)
        except (OSError, ValueError):
            continue


def _terminate(signum: int, _frame: FrameType | None) -> None:
    cleanup_all()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
