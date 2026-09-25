"""The refusal type of the shared-state package.

Importing this module is side-effect free.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class StateError(ValueError):
    """Shared deployment state or its lock refused or failed.

    ``code`` is registered in :mod:`piceli.errors` (``piceli explain <code>``);
    the message names objects, holders and sizes, never secret values or
    server messages. ``details`` holds printable structured data (the lock
    holder and when its lease expires).
    """

    def __init__(
        self, code: str, message: str, *, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})
