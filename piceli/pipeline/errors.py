"""The refusal type of the pipeline package.

Importing this module is side-effect free.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class PipelineError(ValueError):
    """A pipeline declaration or run was refused or failed.

    ``code`` is a fixed error code registered in :mod:`piceli.errors`
    (``piceli explain <code>``); the message is safe to print (no secret
    values). ``failed`` distinguishes "ran but did not succeed" (exit 1) from
    "rejected before the stage changed anything" (exit 2). ``details`` holds
    printable structured data, such as the objects blocking a release plan.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        failed: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.failed = failed
        self.details = dict(details or {})
