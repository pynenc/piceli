"""The local UI's private state directory (owned forward records, launch token).

``$XDG_STATE_HOME/piceli/ui`` or ``~/.local/state/piceli/ui`` unless the
caller names one. The directory is created with mode ``0700`` and must belong
to the current user. Importing this module is side-effect free.
"""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path
from typing import IO

_launch_handles: dict[Path, IO[bytes]] = {}


def default_ui_state_dir() -> Path:
    """Where ``piceli ui serve`` keeps its private state by default."""
    root = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(root) / "piceli" / "ui"


def private_ui_state_dir(explicit: Path | None = None) -> Path:
    """Create (``0700``) and return the private UI state directory.

    Raises :class:`ValueError` when the directory is a symlink or belongs to
    another user.
    """
    directory = (explicit or default_ui_state_dir()).absolute()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or (
        hasattr(os, "getuid") and info.st_uid != os.getuid()
    ):
        raise ValueError("UI state directory is not a private directory")
    os.chmod(directory, 0o700)
    return directory


def launch_token_file(directory: Path, port: int) -> Path:
    """The single private token path for a local server port."""
    return directory / f"launch-token-{port}"


def write_launch_token(directory: Path, port: int, token: str) -> Path:
    """Lock the port's file for this process; recover it after a crashed owner."""
    path = launch_token_file(directory, port)
    if path in _launch_handles:
        raise FileExistsError("UI launch token port is already owned")
    descriptor = os.open(
        path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    handle = os.fdopen(descriptor, "r+b")
    try:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        ):
            raise ValueError("UI launch token file is not private")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FileExistsError("UI launch token port is already owned") from None
        handle.seek(0)
        handle.truncate()
        handle.write(token.encode() + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
        _launch_handles[path] = handle
        return path
    except BaseException:
        handle.close()
        raise


def remove_launch_token(path: Path) -> None:
    """Remove only this process's still-owned token file and release its lock."""
    handle = _launch_handles.pop(path, None)
    if handle is None:
        return
    try:
        if path.exists() and path.stat().st_ino == os.fstat(handle.fileno()).st_ino:
            path.unlink()
    finally:
        handle.close()
