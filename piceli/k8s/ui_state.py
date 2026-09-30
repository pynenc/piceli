"""The local UI's private state directory (owned forward records, launch token).

``$XDG_STATE_HOME/piceli/ui`` or ``~/.local/state/piceli/ui`` unless the
caller names one. The directory is created with mode ``0700`` and must belong
to the current user. Importing this module is side-effect free.
"""

from __future__ import annotations

import os
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
