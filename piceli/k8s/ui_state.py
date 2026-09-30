"""The local UI's private state directory (owned forward records, launch token).

``$XDG_STATE_HOME/piceli/ui`` or ``~/.local/state/piceli/ui`` unless the
caller names one. The directory is created with mode ``0700`` and must belong
to the current user. Importing this module is side-effect free.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path


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


def launch_token_file(directory: Path, port: int, instance_id: str) -> Path:
    """A private token path for one server instance on ``port``."""
    return directory / f"launch-token-{port}-{instance_id}"


def write_launch_token(directory: Path, port: int, token: str) -> Path:
    """Write a server's launch token in a new ``0600`` file; return its path.

    Another process may already be serving the same port. Never remove or
    overwrite that process's token before the new process attempts to bind.
    """
    for _ in range(3):
        path = launch_token_file(directory, port, secrets.token_hex(12))
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            continue
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(token.encode() + b"\n")
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path
    raise FileExistsError("Could not create a unique UI launch token file")
